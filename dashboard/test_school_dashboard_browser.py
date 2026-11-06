"""Populated school pages must fit real phone, tablet and desktop viewports."""
from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from core.models import DisplayScreen, School, SupportTicket
from notices.models import Announcement
from schedule.models import ClassLesson, SchoolSettings, Subject
from subscriptions.models import SchoolSubscription
from . import test_manager_templates as fixtures

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


LAYOUT_FACTS = """() => {
  const visible = e => e.checkVisibility() && e.getBoundingClientRect().width > 0;
  const scrollable = e => {
    for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
      if (['auto','scroll'].includes(getComputedStyle(p).overflowX)
          && p.scrollWidth > p.clientWidth) return true;
    }
    return false;
  };
  const outside = [...document.querySelectorAll(
    'main input, main select, main textarea, main button, main a, main h1, main h2,' +
    '.dashboard-topbar button, .dashboard-topbar a, .dashboard-topbar select'
  )].filter(e => visible(e) && !scrollable(e)).filter(e => {
    const r = e.getBoundingClientRect();
    return r.left < -2 || r.right > innerWidth + 2;
  }).map(e => e.id || e.textContent.trim().slice(0, 70));
  const unlabelled = [...document.querySelectorAll(
    'main input:not([type=hidden]):not([type=submit]), main select, main textarea'
  )].filter(e => visible(e) && !e.labels?.length && !e.getAttribute('aria-label')
    && !e.getAttribute('aria-labelledby') && !e.getAttribute('title'))
    .map(e => e.id || e.name);
  const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
  return { overflow: document.documentElement.scrollWidth - innerWidth, outside,
    unlabelled, duplicateIds: ids.filter((id, i) => ids.indexOf(id) !== i) };
}"""


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class SchoolDashboardBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        # Reuse the render suite's records, without inheriting its test methods.
        fixtures.ManagerTemplateRenderTests.setUpTestData()
        fixture = fixtures.ManagerTemplateRenderTests
        school = fixture.school
        plan = SchoolSubscription.objects.get(school=school).plan
        other = School.objects.create(name="مدرسة أخرى مسموحة للمدير", slug="responsive-other")
        fixture.user.profile.schools.add(other)
        SchoolSettings.objects.create(school=other, name=other.name)
        SchoolSubscription.objects.create(school=other, plan=plan, starts_at=timezone.localdate(), status="active")
        screen = DisplayScreen.objects.create(school=school, name="شاشة المدخل الرئيسي ومركز مصادر التعلم")
        lesson = ClassLesson.objects.create(
            settings=fixture.settings, school_class=fixture.school_class,
            weekday=7, period_index=1, subject=Subject.objects.get(school=school), teacher=fixture.teacher,
        )
        ticket = SupportTicket.objects.create(
            school=school, user=fixture.user, subject="استفسار عن شاشة المدرسة", message="تفاصيل الاستفسار",
        )
        for number in range(12):
            Announcement.objects.create(
                school=school, title=f"تعليمات وفعاليات المدرسة الأسبوعية {number}",
                body="يرجى الالتزام بالتعليمات المدرسية والجدول المعتمد للأنشطة.",
                starts_at=timezone.now()-timedelta(hours=1),
            )
        names = (
            "index", "my_subscription", "schools_overview", "add_school", "settings",
            "help_getting_started", "screen_list", "screen_create", "screen_pairing",
            "days_list", "closures_list", "timetable_day", "timetable_week", "ann_list",
            "ann_create", "emergency_alert_list", "emergency_alert_create", "occasion_templates",
            "exc_list", "exc_create", "duty_list", "duty_create", "standby_list", "standby_create",
            "school_data", "school_data_import", "school_document_import", "lessons_list", "add_lesson", "customer_support_tickets",
            "customer_support_ticket_create", "change_password", "select_school", "two_factor_setup",
        )
        self.pages = [(name, reverse("dashboard:" + name)) for name in names]
        for name, pk in (
            ("ann_edit", fixture.announcement.pk), ("exc_edit", fixture.excellence.pk),
            ("duty_edit", fixture.duty.pk), ("screen_edit", screen.pk), ("edit_lesson", lesson.pk),
            ("day_edit", 7), ("customer_support_ticket_detail", ticket.pk),
        ):
            self.pages.append((name, reverse("dashboard:" + name, args=[pk])))
        self.pages += [
            ("timetable_day_filtered", reverse("dashboard:timetable_day") + f"?weekday=7&class_id={fixture.school_class.pk}"),
            ("ann_list_page2", reverse("dashboard:ann_list") + "?page=2"),
        ]
        self.client.force_login(fixture.user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover
            self.playwright.stop()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")
        self.context = self.browser.new_context(locale="ar-SA", service_workers="block")
        self.context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": cookie, "url": self.live_server_url}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.browser.close()
        self.playwright.stop()

    def assert_page_fits(self, path, width):
        self.page.set_viewport_size({"width": width, "height": 900})
        response = self.page.goto(self.live_server_url + path, wait_until="networkidle")
        self.assertEqual(response.status, 200)
        self.assertNotIn("/login/", self.page.url)
        self.page.evaluate("document.fonts.ready")
        facts = self.page.evaluate(LAYOUT_FACTS)
        self.assertLessEqual(facts["overflow"], 1, facts)
        self.assertEqual(facts["outside"], [], facts)
        self.assertEqual(facts["unlabelled"], [], facts)
        self.assertEqual(facts["duplicateIds"], [], facts)
        self.assertEqual(self.errors, [])

    def test_all_school_pages_fit_phone_tablet_and_desktop(self):
        for width in (390, 768, 1440):
            for name, path in self.pages:
                with self.subTest(page=name, width=width):
                    self.assert_page_fits(path, width)

    def test_dense_pages_and_all_settings_tabs_fit_small_phones(self):
        dense = {"my_subscription", "schools_overview", "settings", "help_getting_started", "timetable_day_filtered", "screen_edit"}
        for name, path in self.pages:
            if name not in dense:
                continue
            with self.subTest(page=name, width=320):
                self.assert_page_fits(path, 320)
                if name in {"settings", "screen_edit"}:
                    for tab in self.page.locator(".settings-tab-btn").all():
                        tab.click()
                        facts = self.page.evaluate(LAYOUT_FACTS)
                        self.assertLessEqual(facts["overflow"], 1, facts)
                        self.assertEqual(facts["outside"], [], facts)
                        self.assertEqual(facts["unlabelled"], [], facts)

    def test_autosave_persists_clear_and_restore_through_the_real_endpoint(self):
        path = dict(self.pages)["timetable_day_filtered"]
        self.assert_page_fits(path, 390)
        subject = self.page.locator("[data-tt-subject]").first
        teacher = self.page.locator("[data-tt-teacher]").first
        initial_subject, initial_teacher = subject.input_value(), teacher.input_value()
        self.assertTrue(initial_subject)
        self.assertTrue(initial_teacher)
        for subject_value, teacher_value in (("", ""), (initial_subject, initial_teacher)):
            with self.page.expect_response(
                lambda response: response.request.method == "POST" and "/timetable/day/" in response.url,
                timeout=10000,
            ) as saved:
                subject.select_option(subject_value)
                teacher.select_option(teacher_value)
            self.assertEqual(saved.value.status, 200)
            self.assertTrue(saved.value.json()["ok"])
            self.page.wait_for_function("document.querySelector('[data-tt-reset]').disabled")
            self.page.reload(wait_until="networkidle")
            self.assertEqual(subject.input_value(), subject_value)
            self.assertEqual(teacher.input_value(), teacher_value)
        self.assertEqual(self.errors, [])

    def test_compact_timetable_fields_are_clickable_above_the_save_actions(self):
        path = dict(self.pages)["timetable_day_filtered"]
        for width, height in ((320, 568), (390, 844), (844, 390)):
            with self.subTest(width=width, height=height):
                self.page.set_viewport_size({"width": width, "height": height})
                self.page.goto(self.live_server_url + path, wait_until="networkidle")
                self.page.evaluate("document.fonts.ready")
                for selector in ("[data-tt-subject]", "[data-tt-teacher]", "[data-tt-form] button[type=submit]"):
                    field = self.page.locator(selector).first
                    field.evaluate("e => e.scrollIntoView({block: 'center', behavior: 'instant'})")
                    hit = field.evaluate("""e => {
                      const r = e.getBoundingClientRect();
                      const target = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
                      return {own: target === e || e.contains(target), cover: target?.className};
                    }""")
                    self.assertTrue(hit["own"], {"selector": selector, **hit})
                    if field.evaluate("e => e.tagName === 'SELECT'"):
                        field.click(timeout=3000)
                        self.page.keyboard.press("Escape")
        self.assertEqual(self.errors, [])
