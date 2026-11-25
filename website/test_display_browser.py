"""Run the shipped display template/bundle against controlled API snapshots.

These regressions depend on an actual browser: source-string checks cannot
detect old rows surviving deletion or panels cropped by a CSS transform.
No school/device/payment records or external services are used.
"""

from __future__ import annotations

import asyncio
import copy
import json
import mimetypes
import os
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase

from core import occasions

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional on a developer's fresh clone
    sync_playwright = None


def display_snapshot():
    """A complete school day with enough rows to exercise both scrollers."""
    blocks = [
        {"kind": "period", "index": i, "label": f"الحصة {i}",
         "from": f"{6 + i:02}:00", "to": f"{6 + i:02}:45"}
        for i in range(1, 8)
    ]
    rows = [
        {"class_name": f"الثالث المتوسط / {i}", "subject_name": "الدراسات الاجتماعية",
         "teacher_name": f"عبد الرحمن محمد العبد الله {i}", "period_index": 2}
        for i in range(1, 13)
    ]
    return {
        "now": "2026-10-11T08:20:00+03:00",
        "settings": {"name": "مدرسة الاختبار المتوسطة والثانوية", "school_type": "boys",
                     "theme": "indigo", "featured_panel": "excellence", "refresh_interval_sec": 30},
        "meta": {"schedule_revision": 1, "date": "2026-10-11", "weekday": 7,
                 "is_school_day": True, "is_active_window": True, "is_period_day_window": True,
                 "active_window": {"start": "2026-10-11T06:30:00+03:00", "end": "2026-10-11T14:00:00+03:00"},
                 "next_wake_at": None, "ws_enabled": False},
        "state": {"type": "period", "index": 2, "label": "الحصة الثانية",
                  "from": "08:00", "to": "08:45", "remaining_seconds": 1500},
        "current_period": blocks[1], "next_period": blocks[2], "day_path": blocks,
        "period_classes": rows, "period_classes_map": {"2": rows},
        "standby": [{"class_name": f"الثاني المتوسط / {i}", "teacher_name": f"محمد عبد العزيز القحطاني {i}",
                     "period_index": 2 + i % 3} for i in range(1, 9)],
        "excellence": [{"name": "عبد الله عبد الرحمن محمد العتيبي", "reason": "التميز العلمي والانضباط المدرسي"}],
        "duty": {"items": [{"teacher_name": f"عبد الرحمن العبد الله {i}", "location": "الساحة الداخلية",
                            "duty_type": "supervision"} for i in range(1, 8)]},
        "announcements": [{"id": 1, "title": "إعلان مدرسي", "body": "نرحب بطلابنا ونتمنى لهم يوماً دراسياً متميزاً.", "level": "info"}],
        "emergency_alerts": [],
    }


def display_html():
    context = {
        "school_name": "مدرسة الاختبار المتوسطة والثانوية", "school_type": "boys", "theme": "indigo",
        "api_token": "a" * 64, "snapshot_url": "/api/display/snapshot/" + "a" * 64 + "/",
        "screen_id": 1, "school_id": 1, "screen_occasion_theme": "auto",
        "display_use_minified_js": True, "ws_live_status_check_sec": 3600,
        "occasion_themes": occasions.all_occasions(),
        "occasion_theme_meta_json": json.dumps(occasions.theme_map(), ensure_ascii=False),
    }
    for panel in ("announcements", "period_classes", "standby", "duty", "excellence"):
        context["screen_show_" + panel] = True
    return render_to_string("website/display.html", context).encode("utf-8")


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class DisplayBrowserTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        html = display_html()
        root = Path(settings.BASE_DIR)
        worker = render_to_string("sw.js").encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if getattr(self.server, "simulate_outage", False):
                    self.close_connection = True
                    return
                path = unquote(urlparse(self.path).path)
                if path in {"/", "/s/TEST01", "/s/TEST01/"}:
                    data, content_type = html, "text/html; charset=utf-8"
                elif path == "/sw.js":
                    data, content_type = worker, "application/javascript; charset=utf-8"
                elif path.startswith("/static/"):
                    target = (root / path.lstrip("/")).resolve()
                    if not target.is_relative_to((root / "static").resolve()) or not target.is_file():
                        self.send_error(404)
                        return
                    data = target.read_bytes()
                    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                elif path == "/favicon.png":
                    data = (root / "static/img/UntiTtled-1.png").read_bytes()
                    content_type = "image/png"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                if path.startswith("/s/"):
                    self.send_header("X-School-Display", "board")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        try:
            self.browser = self.playwright.chromium.launch(headless=True)
        except Exception as exc:
            if os.environ.get("CI"):
                raise
            raise unittest.SkipTest(f"Install Chromium with python -m playwright install chromium: {exc}") from exc
        self.addCleanup(self.browser.close)
        self.snapshot = display_snapshot()
        self.offline = False
        self.snapshot_requests = 0
        self.errors = []

    def open_board(self, width=1920, height=1080, dpr=1, screen=None, query="", *, worker=False, init_script=None):
        self.context = self.browser.new_context(
            viewport={"width": width, "height": height}, device_scale_factor=dpr,
            screen=screen or {"width": width, "height": height},
            locale="ar-SA", timezone_id="Asia/Riyadh", service_workers="allow" if worker else "block",
        )
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        if init_script:
            self.page.add_init_script(init_script)
        self.page.route("**/api/**", self.api_response)
        self.page.clock.install(time=datetime.fromisoformat(self.snapshot["now"]))
        self.page.goto(self.url + ("/s/TEST01/" if worker else "/") + query, wait_until="networkidle")
        self.page.wait_for_function("document.body.dataset.displayState !== 'loading'")
        self.page.evaluate("document.fonts.ready")
        self.page.wait_for_timeout(200)

    def api_response(self, route):
        if self.offline:
            route.abort()
        elif "/snapshot/" in route.request.url:
            self.snapshot_requests += 1
            route.fulfill(json=copy.deepcopy(self.snapshot))
        else:
            route.fulfill(json={"fetch_required": True, "revision": self.snapshot["meta"]["schedule_revision"]})

    def receive_update(self):
        self.snapshot["meta"]["schedule_revision"] += 1
        with self.page.expect_response(lambda r: "/snapshot/" in r.url):
            self.page.evaluate("window.dispatchEvent(new Event('online'))")
        self.page.wait_for_timeout(250)

    def test_deleting_assignments_removes_old_names(self):
        self.open_board()
        self.assertEqual(self.page.locator("#pcCount").inner_text(), "12")
        self.snapshot["period_classes_map"] = {}
        self.snapshot["period_classes"] = []
        self.receive_update()
        self.assertEqual(self.page.locator("#pcCount").inner_text(), "0")
        self.assertNotIn("عبد الرحمن", self.page.locator("#periodClassesTrack").inner_text())
        self.assertEqual(self.errors, [])

    def test_cancelling_schedule_stops_the_old_period(self):
        self.open_board()
        self.snapshot.update(state={"type": "NO_SCHEDULE_TODAY", "reason": "holiday"}, day_path=[],
                             current_period=None, next_period=None, period_classes_map={}, period_classes=[])
        self.snapshot["meta"].update(is_school_day=False, is_active_window=False, is_period_day_window=False)
        self.receive_update()
        self.page.clock.fast_forward(5000)
        self.assertEqual(self.page.locator("body").get_attribute("data-display-state"), "holiday")
        self.assertEqual(self.page.locator("#pcCount").inner_text(), "0")
        self.assertEqual(self.page.locator("body").get_attribute("data-countdown-active"), "0")
        self.assertEqual(self.errors, [])

    def test_same_timetable_on_next_day_rebases_period_times(self):
        self.open_board()
        self.snapshot["now"] = "2026-10-12T08:20:00+03:00"
        self.snapshot["meta"].update(date="2026-10-12", weekday=1, schedule_revision=2)
        self.snapshot["meta"]["active_window"] = {
            "start": "2026-10-12T06:30:00+03:00", "end": "2026-10-12T14:00:00+03:00",
        }
        with self.page.expect_response(lambda r: "/snapshot/" in r.url):
            self.page.clock.fast_forward(24 * 60 * 60 * 1000)
        self.page.wait_for_timeout(350)
        self.assertEqual(self.page.locator("body").get_attribute("data-display-state"), "period")
        self.assertEqual(self.page.locator("#heroTitle").inner_text(), "الحصة الثانية")
        self.assertEqual(self.page.locator("#pcCount").inner_text(), "12")
        self.assertEqual(self.errors, [])

    def test_announcement_expires_and_future_one_starts_without_fetching(self):
        self.snapshot["announcements"] = [
            {"id": 1, "title": "قبل الانتهاء", "body": "رسالة أولى", "expires_at": "2026-10-11T08:20:05+03:00"},
            {"id": 2, "title": "الإعلان التالي", "body": "رسالة ثانية", "starts_at": "2026-10-11T08:20:08+03:00",
             "expires_at": "2026-10-11T08:20:15+03:00"},
        ]
        self.open_board()
        self.offline = True
        self.page.clock.fast_forward(6000)
        self.assertEqual(self.page.locator("body").get_attribute("data-announcement-empty"), "1")
        self.page.clock.fast_forward(3000)
        self.assertEqual(self.page.locator("#alertTitle").inner_text(), "الإعلان التالي")
        self.page.clock.fast_forward(8000)
        self.assertEqual(self.page.locator("body").get_attribute("data-announcement-empty"), "1")
        self.assertEqual(self.snapshot_requests, 1)
        self.assertEqual(self.errors, [])

    def test_full_announcement_text_is_readable_across_pages(self):
        text = ("نرجو من جميع الطلاب والمعلمين الالتزام بالتعليمات المنظمة للفعاليات المدرسية والتوجه إلى مواقع الأنشطة حسب الجدول المعتمد مع إحضار الأدوات والمواد المطلوبة. " * 3)[:280]
        self.snapshot["announcements"] = [{"id": 1, "title": "تعليمات الأنشطة المدرسية", "body": text}]
        self.open_board(width=390, height=844)
        self.offline = True
        pages = int(self.page.locator("#alertContainer").get_attribute("data-page-total"))
        self.assertGreater(pages, 1)
        parts = []
        for index in range(pages):
            self.assertEqual(self.page.locator("#alertContainer").get_attribute("data-page-current"), str(index + 1))
            parts.append(self.page.locator("#alertDetails").inner_text())
            self.assertTrue(self.page.locator("#alertDetails").evaluate(
                "el=>el.scrollHeight<=el.clientHeight+1 && el.scrollWidth<=el.clientWidth+1"
            ))
            if index + 1 < pages:
                self.page.clock.fast_forward(21000)
        self.assertEqual(" ".join(" ".join(parts).split()), " ".join(text.split()))
        self.page.set_viewport_size({"width": 1280, "height": 720})
        self.page.wait_for_timeout(500)
        self.assertTrue(self.page.locator("#alertDetails").evaluate("el=>el.scrollHeight<=el.clientHeight+1"))
        self.assertEqual(self.errors, [])

    def test_period_transitions_and_emergency_expiry_still_work_offline(self):
        self.snapshot["emergency_alerts"] = [{"id": 1, "kind": "urgent", "title": "اختبار تنبيه",
                                              "message": "تنبيه مؤقت", "expires_at": "2026-10-11T08:20:05+03:00"}]
        self.open_board()
        self.assertTrue(self.page.locator("#emergencyOverlay").is_visible())
        self.offline = True
        self.page.clock.fast_forward(12000)
        self.assertFalse(self.page.locator("#emergencyOverlay").is_visible())
        self.page.clock.fast_forward(40 * 60 * 1000)
        self.assertEqual(self.page.locator("#heroTitle").inner_text(), "الحصة الثالثة")
        self.assertEqual(self.page.locator("#pcCount").inner_text(), "0")
        self.assertEqual(self.errors, [])

    def test_offline_reload_and_reconnection_use_the_display_cache(self):
        self.open_board(worker=True)
        self.page.wait_for_function("""async () => {
            if (!navigator.serviceWorker.controller) return false;
            return !!(await caches.match(location.href)) &&
                   !!(await caches.match(location.origin + '/static/sounds/bell.mp3'));
        }""")
        self.assertEqual(self.page.locator("#heroTitle").inner_text(), "الحصة الثانية")
        self.offline = True
        self.server.simulate_outage = True
        self.addCleanup(setattr, self.server, "simulate_outage", False)
        self.context.set_offline(True)
        self.page.clock.fast_forward(40 * 60 * 1000)
        self.assertEqual(self.page.locator("#heroTitle").inner_text(), "الحصة الثالثة")
        self.page.clock.set_system_time(datetime.fromisoformat("2026-10-11T09:00:10+03:00"))

        response = self.page.goto(self.url + "/s/TEST01", wait_until="domcontentloaded")
        self.assertTrue(response.from_service_worker)
        self.page.wait_for_function("document.body.dataset.displayState !== 'loading'", timeout=10000)
        self.assertEqual(self.page.locator("#heroTitle").inner_text(), "الحصة الثالثة", self.errors)
        self.assertTrue(self.page.locator("#offlineModeBadge").is_visible())
        self.assertEqual(self.page.locator("#pcCount").inner_text(), "0")
        bell = self.page.evaluate("""async () => {
            const response = await fetch('/static/sounds/bell.mp3', {headers: {Range: 'bytes=0-15'}});
            return {status: response.status, size: (await response.arrayBuffer()).byteLength};
        }""")
        self.assertEqual(bell, {"status": 206, "size": 16})

        self.offline = False
        self.server.simulate_outage = False
        self.snapshot["now"] = "2026-10-11T09:00:10+03:00"
        self.snapshot["meta"]["schedule_revision"] = 2
        self.snapshot["period_classes_map"]["3"] = [{
            "class_name": "فصل اختباري", "subject_name": "مادة بعد عودة الاتصال", "teacher_name": "معلم اختباري",
        }]
        self.context.set_offline(False)
        # A sleeping network adapter can recover without dispatching an online
        # event. Exercise the ordinary retry as well as the reconnect handler.
        self.page.clock.fast_forward(60 * 1000)
        self.page.get_by_text("مادة بعد عودة الاتصال", exact=True).wait_for(timeout=10000)
        self.assertFalse(self.page.locator("#offlineModeBadge").is_visible())
        self.assertEqual(self.errors, [])

    def test_missing_modern_apis_fall_back_to_tv_polling(self):
        self.snapshot["meta"]["ws_enabled"] = True
        self.open_board(width=1280, height=720, query="?lite=1", init_script="""() => {
            delete window.WebSocket;
            delete window.AbortController;
            delete window.ResizeObserver;
            delete Promise.prototype.finally;
            delete String.prototype.padStart;
            delete Object.entries;
        }""")
        self.assertEqual(self.page.locator("#heroTitle").inner_text(), "الحصة الثانية")
        self.assertEqual(self.page.locator("#old-browser-banner").count(), 0)
        self.receive_update()
        self.assertGreaterEqual(self.snapshot_requests, 2)
        self.assertEqual(self.errors, [])

    def test_long_emergency_is_complete_and_polls_do_not_reset_its_pages(self):
        title = ("تنبيه مهم لجميع الطلاب والمعلمين بخصوص إجراءات السلامة في المدرسة " * 3)[:120]
        message = ("يرجى الالتزام بتعليمات فريق السلامة والتوجه بهدوء إلى نقطة التجمع المحددة واتباع توجيهات المشرفين مع المحافظة على سلامة الجميع. " * 6)[:600]
        self.snapshot["emergency_alerts"] = [{"id": 1, "kind": "urgent", "title": title,
                                              "message": message, "expires_at": "2026-10-11T08:25:00+03:00"}]
        self.open_board(width=320, height=568)
        overlay = self.page.locator("#emergencyOverlay")
        pages = int(overlay.get_attribute("data-page-total"))
        self.assertGreater(pages, 1)
        titles, messages = [], []
        for index in range(pages):
            self.assertEqual(overlay.get_attribute("data-page-current"), str(index + 1))
            titles.append(self.page.locator("#emergencyTitle").inner_text())
            messages.append(self.page.locator("#emergencyMessage").inner_text())
            for selector in ("#emergencyTitle", "#emergencyMessage", "#emergencyKind"):
                self.assertTrue(self.page.locator(selector).evaluate("""el => {
                    const r=el.getBoundingClientRect();
                    return r.top>=0 && r.bottom<=innerHeight && r.left>=0 && r.right<=innerWidth &&
                           el.scrollHeight<=el.clientHeight+1 && el.scrollWidth<=el.clientWidth+1;
                }"""), selector)
            self.receive_update()
            self.assertEqual(overlay.get_attribute("data-page-current"), str(index + 1))
            if index + 1 < pages:
                self.page.clock.fast_forward(21000)
        self.assertTrue(" ".join(" ".join(titles).split()).startswith(" ".join(title.split())))
        self.assertTrue(" ".join(" ".join(messages).split()).startswith(" ".join(message.split())))
        self.offline = True
        self.page.set_viewport_size({"width": 1280, "height": 720})
        self.page.wait_for_timeout(400)
        self.assertTrue(self.page.locator("#emergencyMessage").evaluate("el=>el.scrollHeight<=el.clientHeight+1"))
        self.page.clock.fast_forward(5 * 60 * 1000)
        self.assertFalse(overlay.is_visible())
        self.assertEqual(self.errors, [])

    def test_saved_calendar_honours_closure_on_next_offline_day(self):
        self.snapshot["meta"]["week_plan"] = {str(i): self.snapshot["day_path"] for i in range(1, 8)}
        self.snapshot["meta"]["offline_calendar"] = {
            "version": 1, "timezone_name": "Asia/Riyadh", "utc_offset_minutes": 180,
            "valid_from": "2026-10-11", "valid_through": "2026-10-31",
            "closed_dates": {"2026-10-12": "تعليق الدراسة بسبب الأحوال الجوية"},
        }
        self.open_board()
        self.offline = True
        self.page.evaluate("window.dispatchEvent(new Event('offline'))")
        self.page.clock.fast_forward(24 * 60 * 60 * 1000)
        self.page.wait_for_function("document.querySelector('#heroTitle').textContent === 'تعليق الدراسة بسبب الأحوال الجوية'")
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_function("document.body.dataset.displayState !== 'loading'")
        self.assertEqual(self.page.locator("#heroTitle").inner_text(), "تعليق الدراسة بسبب الأحوال الجوية")
        self.assertEqual(self.page.locator("#pcCount").inner_text(), "0")
        self.assertFalse(self.page.locator("#exCard").is_visible())
        self.assertTrue(self.page.locator("#offlineModeBadge").is_visible())
        self.assertEqual(self.errors, [])

    def test_legacy_offline_calendar_shows_uncertainty_instead_of_holiday(self):
        self.snapshot["meta"]["week_plan"] = {str(i): self.snapshot["day_path"] for i in range(1, 8)}
        self.open_board()
        self.offline = True
        self.page.clock.fast_forward(24 * 60 * 60 * 1000)
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_function("document.body.dataset.displayState !== 'loading'")
        self.assertEqual(self.page.locator("#heroTitle").inner_text(), "بانتظار الاتصال لتحديث جدول اليوم")
        self.assertEqual(self.page.locator("#boardStatusText").inner_text(), "بانتظار تحديث الجدول")
        self.assertEqual(self.page.locator("#pcCount").inner_text(), "0")
        self.assertEqual(self.page.locator("#offlineModeBadge").get_attribute("data-severity"), "critical")
        self.assertEqual(self.errors, [])

    def test_honor_reason_and_remaining_timetable_are_readable_without_interaction(self):
        reason = " ".join(f"المشاركة المدرسية {i}" for i in range(30))
        self.snapshot["excellence"] = [{"name": "ريم أحمد", "reason": reason}]
        for stop in self.snapshot["day_path"]:
            stop["label"] = "حصة"
        self.open_board(width=390, height=844)
        self.offline = True
        self.assertEqual(self.page.locator(".honor-initials").inner_text(), "ر أ")
        reason_box = self.page.locator("#exCard .honor-reason")
        parts = []
        for _ in range(30):
            part = reason_box.inner_text()
            if part in parts:
                break
            self.assertTrue(reason_box.evaluate("el => el.scrollHeight <= el.clientHeight + 1"))
            parts.append(part)
            self.page.clock.fast_forward(7000)
        self.assertGreater(len(parts), 1)
        self.assertEqual(" ".join(parts), reason)
        self.assertEqual(self.page.locator("#miniSchedule .is-active").text_content(), "08:00الحصة الثانية")
        stops = self.page.locator("#miniSchedule .mini-schedule-item__label")
        visible = set()
        for _ in range(6):
            visible.update(stops.evaluate_all("els => els.filter(el => el.offsetWidth).map(el => el.textContent)"))
            self.page.clock.fast_forward(8000)
        self.assertEqual(visible, set(stops.all_text_contents()))
        self.assertEqual(self.errors, [])

    def test_inherited_scroll_speed_uses_the_school_limits_on_the_open_board(self):
        self.snapshot["settings"].update(periods_scroll_speed=0.05, standby_scroll_speed=0.5)
        self.open_board()
        tracks = self.page.locator("#periodClassesTrack, #standbyTrack")
        positions = "els => els.map(el => Math.abs(parseFloat(el.style.transform.split('(')[1]) || 0))"
        def distances_in_one_second():
            before = tracks.evaluate_all(positions)
            self.page.clock.run_for(1000)
            return [end - start for start, end in zip(before, tracks.evaluate_all(positions))]
        periods, standby = distances_in_one_second()
        self.assertAlmostEqual(periods, 3, delta=1)
        self.assertAlmostEqual(standby, 30, delta=3)
        self.snapshot["settings"].update(periods_scroll_speed=5, standby_scroll_speed=1.5)
        self.receive_update()
        periods, standby = distances_in_one_second()
        self.assertAlmostEqual(periods, 300, delta=15)
        self.assertAlmostEqual(standby, 90, delta=6)
        self.assertEqual(self.errors, [])

    def test_cards_show_substitute_timing_and_duty_after_rotating(self):
        self.snapshot["settings"]["school_type"] = "girls"
        self.snapshot["period_classes"] = self.snapshot["period_classes"][:4]
        self.snapshot["period_classes_map"]["2"] = self.snapshot["period_classes"]
        classroom = self.snapshot["period_classes"][0]["class_name"]
        self.snapshot["standby"] = [
            {"class_name": classroom, "teacher_name": name, "period_index": index}
            for index, name in [(4, "ريم"), (2, "مريم"), (1, "هدى"), (3, "نورة")]
        ]
        self.snapshot["excellence"] = [{"name": "ريم أحمد", "reason": "الابتكار العلمي", "image_src": "/missing-photo.jpg"}]
        self.open_board()
        substitute = self.page.locator("#periodClassesTrack > :first-child .slot-teacher--substitute")
        self.assertEqual(substitute.locator(".label").text_content(), "المعلمة البديلة")
        self.assertEqual(substitute.locator(".name").text_content(), "مريم")
        lists = self.page.locator("#standbyTrack > :first-child")
        self.assertEqual(lists.locator(".standby-section-title").all_text_contents(), ["الآن", "لاحقًا اليوم"])
        self.assertEqual(lists.locator(".slot-teacher .name").all_text_contents(), ["مريم", "نورة", "ريم"])
        self.assertEqual(self.page.locator("#sbCount").text_content(), "3")
        self.assertEqual(self.page.locator("#exCard .honor-initials").inner_text(), "ر أ")
        self.assertFalse(self.page.locator("#exCard .honor-counter").is_visible())
        self.snapshot["settings"]["featured_panel"] = "duty"
        self.snapshot["duty"]["items"] = [dict(teacher_name="مريم", location="البوابة الرئيسية", duty_type="supervision")]
        self.page.set_viewport_size({"width": 720, "height": 1280})
        self.receive_update()
        duty = self.page.locator("#dutyTrack")
        self.assertEqual(duty.locator(".duty-role").inner_text(), "إشراف")
        self.assertEqual(duty.locator(".duty-name").inner_text(), "مريم")
        self.assertEqual(duty.locator(".duty-location").inner_text(), "البوابة الرئيسية")
        self.assertEqual(duty.get_attribute("data-scrolling"), "0")
        self.assertEqual(self.page.locator("body").get_attribute("data-board-shape"), "portrait")
        for selector in ("#periodClassesTrack", "#standbyTrack"):
            self.page.wait_for_function("""selector => {
                const track = document.querySelector(selector);
                const fits = track.firstElementChild.offsetHeight <= track.parentElement.offsetHeight + 1;
                return track.dataset.scrolling === (fits ? "0" : "1");
            }""", arg=selector)
        self.assertEqual(self.errors, [])

    def test_long_school_message_fits_the_heading_above_the_timeline(self):
        title = ("نرحب بكم في مدرستنا ونتمنى لكم بداية دراسية موفقة " * 3).strip()
        self.snapshot["settings"]["display_before_title"] = title
        self.snapshot["now"] = "2026-10-11T06:45:00+03:00"
        self.snapshot["current_period"] = None
        self.snapshot["next_period"] = self.snapshot["day_path"][0]
        self.snapshot["state"] = {"type": "before", "label": title, "from": "07:00", "to": "07:45"}
        self.open_board(width=320, height=568)
        self.assertEqual(self.page.locator("#heroTitle").inner_text(), title)
        self.assertTrue(self.page.locator("#heroTitle").evaluate("""el => {
            const r = el.getBoundingClientRect(), hero = el.closest('#heroCard').getBoundingClientRect();
            return r.top >= hero.top && r.bottom <= document.querySelector('#dayTimelinePanel').getBoundingClientRect().top
                && r.right <= hero.right && el.scrollWidth <= el.clientWidth + 1;
        }"""))

    def test_viewports_keep_all_panels_inside_the_screen(self):
        matrix = [(1920, 1080, 1), (1280, 720, 1), (3840, 2160, 1), (1366, 768, 1),
                  (1920, 1200, 1), (1024, 768, 1), (3440, 1440, 1), (1080, 1920, 1),
                  (768, 1024, 1), (390, 844, 3), (320, 568, 2), (960, 540, 2), (1000, 600, 1),
                  (800, 480, 1), (600, 600, 1), (5120, 1440, 1), (2160, 3840, 1)]
        for width, height, dpr in matrix:
            with self.subTest(width=width, height=height, dpr=dpr):
                screen = {"width": 1920, "height": 1080} if (width, height) == (1000, 600) else None
                self.open_board(width, height, dpr, screen)
                measurements = self.page.evaluate("""() => {
                    const selectors=['#fitStage','#schoolName','#heroCard','#exCard','#periodCard','#standbyCard','#alertContainer'];
                    const panels=selectors.map(s=>{
                        const e=document.querySelector(s), r=e.getBoundingClientRect();
                        return {selector:s,x:r.x,y:r.y,right:r.right,bottom:r.bottom,w:r.width,h:r.height};
                    });
                    const current=document.querySelector('#currentScheduleList').getBoundingClientRect();
                    const timeline=document.querySelector('#dayTimelinePanel').getBoundingClientRect();
                    const scale=Number(document.body.dataset.uiScale);
                    const announcementFont=parseFloat(getComputedStyle(document.querySelector('#alertDetails')).fontSize)*scale;
                    const teacherFont=parseFloat(getComputedStyle(document.querySelector('.slot-teacher .name')).fontSize)*scale;
                    const header=document.querySelector('#fitStage > header').getBoundingClientRect();
                    const hero=document.querySelector('#heroCard').getBoundingClientRect();
                    const next=document.querySelector('#nextLabel').getBoundingClientRect();
                    return {panels,w:innerWidth,h:innerHeight,sw:document.documentElement.scrollWidth,
                            headerBottom:header.bottom,heroTop:hero.top,nextBottom:next.bottom,
                            currentBottom:current.bottom,timelineTop:timeline.top,announcementFont,teacherFont};
                }""")
                self.assertLessEqual(measurements["sw"], width + 2)
                self.assertLessEqual(measurements["headerBottom"], measurements["heroTop"] + 1)
                self.assertLessEqual(measurements["nextBottom"], measurements["timelineTop"] + 1)
                for rect in measurements["panels"]:
                    self.assertGreater(rect["w"], 0, rect)
                    self.assertGreater(rect["h"], 0, rect)
                    self.assertGreaterEqual(rect["x"], -1, rect)
                    self.assertGreaterEqual(rect["y"], -1, rect)
                    self.assertLessEqual(rect["right"], width + 1, rect)
                    self.assertLessEqual(rect["bottom"], height + 1, rect)
                stage = measurements["panels"][0]
                self.assertGreaterEqual(stage["w"] * stage["h"] / (width * height), .94)
                self.assertLessEqual(measurements["currentBottom"], measurements["timelineTop"] + 1)
                if (width, height) == (1280, 720):
                    self.assertGreaterEqual(measurements["announcementFont"], 16)
                    self.assertGreaterEqual(measurements["teacherFont"], 17)
                self.assertEqual(self.errors, [])
                self.context.close()

    def test_duty_and_empty_layouts_fit_when_panels_change(self):
        self.snapshot["settings"]["featured_panel"] = "duty"
        self.open_board(width=1080, height=1920, query="?lite=1")
        self.assertTrue(self.page.locator("#dutyCard").is_visible())
        self.assertFalse(self.page.locator("#exCard").is_visible())
        self.snapshot.update(period_classes_map={}, period_classes=[], standby=[], announcements=[])
        self.snapshot["duty"] = {"items": []}
        self.receive_update()
        self.assertFalse(self.page.locator("#boardSideColumn").is_visible())
        self.assertFalse(self.page.locator("#dutyCard").is_visible())
        self.assertEqual(self.page.locator("body").get_attribute("data-layout-mode"), "ambient")
        self.assertEqual(self.errors, [])
