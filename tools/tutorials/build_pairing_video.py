"""Record the real Django UI with Arabic callouts against an isolated demo DB.

Run with the project venv, Playwright Chromium and --ffmpeg /path/to/ffmpeg.
No production requests or existing database records are used.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import logging
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def setup(work):
    import dotenv

    # Never inherit the checkout's .env, Redis, email or database configuration.
    dotenv.load_dotenv = lambda *args, **kwargs: False
    os.environ.update(DEBUG="True", SECRET_KEY="isolated-tutorial-only", DATABASE_URL=f"sqlite:///{work / 'tutorial.sqlite3'}")
    for key in ("REDIS_URL", "REDIS_CACHE_URL", "CACHE_REDIS_URL", "REDIS_CHANNELS_URL", "CHANNELS_REDIS_URL"):
        os.environ[key] = ""
    from config import settings as base

    module = types.ModuleType("tutorial_settings")
    module.__dict__.update({k: v for k, v in vars(base).items() if k.isupper()})
    module.DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": work / "tutorial.sqlite3"}}
    module.ALLOWED_HOSTS = ["school-display.com", "127.0.0.1", "localhost", "testserver"]
    module.CSRF_TRUSTED_ORIGINS = ["https://school-display.com", "http://127.0.0.1:8779"]
    module.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    module.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    module.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    module.X_FRAME_OPTIONS = "SAMEORIGIN"  # Capture studio only; production remains DENY.
    module.STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                       "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
    module.LOGGING = {"version": 1, "disable_existing_loggers": False, "root": {"handlers": [], "level": "ERROR"}}
    sys.modules[module.__name__] = module
    os.environ["DJANGO_SETTINGS_MODULE"] = module.__name__
    import django
    django.setup()
    from django.core.management import call_command
    from django.contrib.auth import get_user_model
    from django.test import Client
    from core.models import DisplayScreen
    from schedule.models import SchoolSettings

    with contextlib.redirect_stdout(io.StringIO()):
        call_command("migrate", verbosity=0)
        call_command("setup_demo", username="tutorial_manager", password=secrets.token_urlsafe(24), verbosity=0)
    user = get_user_model().objects.get(username="tutorial_manager")
    school = user.profile.active_school
    DisplayScreen.objects.filter(school=school).delete()
    # The recording includes an actual timetable, even outside school hours.
    SchoolSettings.objects.filter(school=school).update(test_mode_weekday_override=7)
    client = Client()
    client.force_login(user)
    return client.cookies["sessionid"].value, school.pk


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def serve(work):
    from django.core.wsgi import get_wsgi_application
    app = get_wsgi_application()
    studio = Path(__file__).with_name("pairing-studio.html").read_bytes()

    def handler(environ, start_response):
        environ["wsgi.url_scheme"] = "https"
        if environ["PATH_INFO"] == "/__tutorial__/":
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [studio]
        return app(environ, start_response)

    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "school-display.com")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("school-display.com"), x509.DNSName("www.school-display.com")]), critical=False)
            .sign(key, hashes.SHA256()))
    key_path, cert_path = work / "localhost.key", work / "localhost.crt"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    server = make_server("127.0.0.1", 443, handler, server_class=ThreadedServer, handler_class=QuietHandler)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stamp(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}.{ms % 1000:03}"


def capture(args, work, cookie):
    from playwright.sync_api import sync_playwright
    from core.models import DisplayPairingSession, DisplayScreen
    from django.urls import reverse
    from PIL import Image

    if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    timeline = []
    chapters = []
    database_reads = ThreadPoolExecutor(max_workers=1)
    pace = 0.1 if args.rehearsal else 1
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--host-resolver-rules=MAP school-display.com 127.0.0.1, MAP www.school-display.com 127.0.0.1",
            "--proxy-server=direct://", "--proxy-bypass-list=*",
        ])
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="ar-SA",
                                      record_video_dir=str(work / "recording"),
                                      record_video_size={"width": 1920, "height": 1080}, service_workers="block", ignore_https_errors=True)
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": "https://school-display.com"}])

        def local_only(route):
            parsed = urlsplit(route.request.url)
            if parsed.hostname in {"school-display.com", "www.school-display.com", "cdnjs.cloudflare.com"}:
                # Chromium resolves the two app domains to the TLS server above.
                route.continue_()
            else:
                route.abort()

        context.route("**/*", local_only)
        # Self-hosted font fallback keeps the recording independent of Google.
        font_css = """@font-face{font-family:Cairo;src:url('https://school-display.com/static/fonts/ibm-plex-sans-arabic-400-arabic.woff2')}@font-face{font-family:Alexandria;src:url('https://school-display.com/static/fonts/ibm-plex-sans-arabic-600-arabic.woff2');font-weight:100 900}"""
        context.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(body=font_css, content_type="text/css"))
        context.route_web_socket("**", lambda ws: ws.close())
        page = context.new_page()
        page.goto("https://school-display.com/__tutorial__/", wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        started = time.monotonic()

        def scene(chapter, title, text, hint="", seconds=5):
            page.evaluate("a => studio.scene(...a)", [chapter, title, text, hint])
            now = time.monotonic() - started
            if not chapters or chapters[-1]["title"] != chapter:
                chapters.append({"title": chapter, "start": now})
            timeline.append({"start": now, "text": text, "hint": hint, "title": title})
            page.screenshot(path=str(work / "latest.png"))
            print(f"{now:6.1f}s {chapter}: {title}", flush=True)
            page.wait_for_timeout(seconds * 1000 * pace)

        def mode(value):
            page.evaluate("v => studio.mode(v)", value)

        def navigate(name, path):
            if name == "phone":
                page.evaluate("document.querySelector('#phone-wait').hidden=true")
            frame = page.frame(name=f"{name}-frame")
            frame.goto("https://school-display.com" + path, wait_until="networkidle")
            page.evaluate("a => studio.address(...a)", [name, path.split("?")[0]])
            frame.locator("body").wait_for()
            page.wait_for_timeout(800)
            close_guide(frame)
            if "/login/" in frame.url:
                page.screenshot(path=str(work / "unexpected-login.png"))
                raise RuntimeError(f"Unexpected login while opening {path}")
            return frame

        def close_guide(frame):
            guide = frame.locator("#pairingGuideDialog[open]")
            if guide.count():
                guide.locator("[data-pairing-guide-close]").first.click()

        def point(locator, seconds=2):
            locator.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            rect = locator.bounding_box()
            if rect:
                page.evaluate("r => studio.mark(r)", rect)
                page.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2, steps=18)
            page.wait_for_timeout(seconds * 1000 * pace)

        scene("قبل البداية", "ربط أول شاشة مدرسية", "تحتاج إلى متصفح مناسب وإنترنت وحساب مسؤول المدرسة.", "يمكن استخدام تلفاز ذكي، أو شاشة موصولة بحاسب أو جهاز تشغيل.", 8)
        mode("desktop")
        desktop = navigate("desktop", reverse("dashboard:screen_list"))
        scene("تجهيز الشاشة", "ابدأ من شاشات العرض", "إذا كانت الشاشة الرئيسية موجودة بالفعل، استخدمها ولا تنشئ شاشة إضافية.", "في هذا المثال سنضيف شاشة باسم موقعها داخل المدرسة.", 6)
        create = desktop.locator("#pairingGuideDialog[open] .pairing-guide-primary")
        if not create.count():
            create = desktop.get_by_role("link", name="شاشة جديدة", exact=True).first
        point(create)
        create.click()
        desktop.wait_for_url("**/screens/new/")
        scene("تجهيز الشاشة", "سمِّ الشاشة حسب مكانها", "اكتب اسمًا يسهل تمييزه، مثل: شاشة المدخل الرئيسي.", "هذا الاسم لإدارة الشاشات، ولا يربط التلفاز بمفرده.", 4)
        field = desktop.locator("#id_name")
        field.fill("شاشة المدخل الرئيسي")
        point(field, 2)
        save = desktop.get_by_role("button", name="حفظ الشاشة")
        point(save, 1)
        save.click()
        desktop.wait_for_url("**/dashboard/screens/")
        page.wait_for_timeout(800)
        close_guide(desktop)
        scene("تجهيز الشاشة", "الشاشة أُضيفت إلى لوحة التحكم", "حفظ الشاشة يجهّزها للربط. الخطوة التالية تتم على التلفاز.", "سنشرح الآن طرق الربط الثلاث، ويمكنك اختيار طريقة واحدة فقط.", 5)
        screen = database_reads.submit(lambda: DisplayScreen.objects.get(name="شاشة المدخل الرئيسي")).result()
        short_path = reverse("website:short_display", args=[screen.short_code])

        mode("split")
        tv = navigate("tv", "/tv/")
        tv.locator("#pairingCode").filter(has_text="—").wait_for(state="hidden", timeout=15000)
        scene("الطريقة الأولى: QR", "افتح صفحة الربط على التلفاز", "على التلفاز: افتح المتصفح واكتب school-display.com/tv", "على الجوال: جهّز الكاميرا لمسح QR الظاهر على التلفاز.", 5)
        point(tv.locator("#qrFrame"), 3)
        pairing = database_reads.submit(lambda: DisplayPairingSession.objects.filter(status="pending").latest("created_at")).result()
        mobile = navigate("phone", reverse("dashboard:screen_pairing_confirm", args=[pairing.pk]))
        scene("الطريقة الأولى: QR", "امسح QR بكاميرا الجوال", "افتح الرابط الذي تقترحه الكاميرا، وسجّل الدخول إلى حساب المدرسة إذا طُلب ذلك.", "تظهر هنا صفحة الاختيار نفسها التي يفتحها رمز QR.", 6)
        option = mobile.locator(".pair-screen-option").first
        point(option, 2)
        option.click()
        scene("الطريقة الأولى: QR", "اختر شاشة المدرسة المقصودة", "اختر «شاشة المدخل الرئيسي»، ثم اضغط «ربط وبدء العرض».", "راجع اسم المدرسة النشطة قبل تأكيد الربط.", 3)
        point(mobile.get_by_role("button", name="ربط وبدء العرض"), 2)
        page.screenshot(path=str(work / "poster.png"))
        mobile.get_by_role("button", name="ربط وبدء العرض").click()
        tv.wait_for_url("**/s/**", timeout=20000)
        page.wait_for_timeout(3000)
        scene("الطريقة الأولى: QR", "ينتقل التلفاز إلى عرض المدرسة", "تم الربط، وانتقل التلفاز تلقائيًا إلى العرض. يمكنك إغلاق صفحة الجوال.", "لا تحتاج إلى إبقاء الجوال مفتوحًا لتستمر الشاشة في العمل.", 7)

        # A new local browser identity represents another TV for the next route.
        tv.evaluate("localStorage.clear()")
        tv = navigate("tv", "/tv/")
        tv.wait_for_function("document.querySelector('#pairingCode').textContent.replace(/\\D/g,'').length===6")
        mobile = navigate("phone", reverse("dashboard:screen_pairing"))
        scene("الطريقة الثانية: رمز الربط", "إذا لم تتمكن من مسح QR", "افتح school-display.com/connect على الجوال للوصول إلى إدخال رمز الربط.", "يمكن أيضًا فتح «ربط تلفاز» من صفحة شاشات العرض.", 6)
        point(tv.locator("#pairingCode"), 3)
        code = tv.locator("#pairingCode").inner_text()
        scene("الطريقة الثانية: رمز الربط", "انقل رمز التلفاز إلى الجوال", "أدخل الأرقام الستة الظاهرة على التلفاز، ثم اضغط «متابعة واختيار الشاشة».", "رمز الربط مؤقت؛ إذا انتهى، أنشئ رمزًا جديدًا من التلفاز.", 4)
        mobile.locator("#pairing-user-code").fill(code)
        point(mobile.locator("#pairing-user-code"), 2)
        mobile.get_by_role("button", name="متابعة واختيار الشاشة").click()
        mobile.locator(".pair-screen-option").first.wait_for()
        scene("الطريقة الثانية: رمز الربط", "انتبه إلى الشاشة المرتبطة سابقًا", "اختر الشاشة. في هذا المثال هي مرتبطة بجهاز التجربة السابق.", "عند أول ربط لشاشة غير مرتبطة، لا تحتاج إلى خيار نقل الارتباط.", 5)
        mobile.locator(".pair-screen-option").first.click()
        checkbox = mobile.locator("input[name=replace_existing]")
        point(mobile.locator(".pair-replace-confirm"), 3)
        scene("الطريقة الثانية: رمز الربط", "نقل الارتباط يحتاج إلى تأكيد", "فعّل «السماح بنقل شاشة مرتبطة» فقط إذا كنت تريد استبدال الجهاز القديم.", "سيصبح هذا التلفاز هو الجهاز المعتمد لهذه الشاشة.", 4)
        checkbox.check()
        mobile.get_by_role("button", name="ربط وبدء العرض").click()
        tv.wait_for_url("**/s/**", timeout=20000)
        page.wait_for_timeout(2000)
        scene("الطريقة الثانية: رمز الربط", "اكتمل الربط بالرمز اليدوي", "النتيجة هي نفسها: يبدأ التلفاز عرض محتوى المدرسة تلقائيًا.", "QR وإدخال الرمز طريقتان للوصول إلى تأكيد الربط نفسه.", 6)

        mode("desktop")
        desktop = navigate("desktop", reverse("dashboard:screen_list"))
        scene("الطريقة الثالثة: الرابط المباشر", "يمكن فتح رابط الشاشة على التلفاز", "من بطاقة الشاشة، انسخ رابط التشغيل الكامل إلى جهاز العرض.", "هذه طريقة بديلة إذا كان إدخال الرابط على جهاز العرض مناسبًا لك.", 5)
        link = desktop.locator(f"#link-{screen.pk}")
        point(link, 3)
        desktop.locator(".screen-link-copy").first.click()
        page.wait_for_timeout(800)
        scene("الطريقة الثالثة: الرابط المباشر", "المعاينة تختلف عن تشغيل التلفاز", "زر «معاينة حية» يعرض المحتوى على جهاز الإدارة للاطلاع فقط.", "المعاينة لا تربط التلفاز، ولا تُحتسب كشاشة متصلة.", 4)
        point(desktop.locator("[data-live-preview-open]").first, 3)
        desktop.locator("[data-live-preview-open]").first.click()
        desktop.locator("#livePreviewDialog[open]").wait_for(timeout=5000)
        page.wait_for_timeout(1500)
        scene("الطريقة الثالثة: الرابط المباشر", "هذه نافذة معاينة داخل لوحة التحكم", "استخدم المعاينة للتحقق من المحتوى، ثم أغلقها لإكمال الربط على جهاز العرض.", "لا تفتح رابط التشغيل العادي على حاسب الإدارة بدل التلفاز.", 5)
        desktop.locator("[data-live-preview-close]").click()
        unbind = desktop.locator(f"form[action$='/{screen.pk}/unbind/'] button")
        scene("الطريقة الثالثة: الرابط المباشر", "عند نقل جهاز مرتبط سابقًا", "إذا كان الرابط مرتبطًا بجهاز آخر، افصل الارتباط القديم أولًا.", "لا تحتاج إلى هذه الخطوة عندما تكون الشاشة جديدة وغير مرتبطة.", 4)
        point(unbind, 2)
        unbind.click()
        confirm = desktop.locator("[data-confirm-submit]")
        confirm.click()
        desktop.wait_for_url("**/dashboard/screens/")
        page.wait_for_timeout(700)
        mode("split")
        # Clear browser identity to demonstrate a fresh external display.
        tv.evaluate("localStorage.clear()")
        tv = navigate("tv", short_path)
        mobile = navigate("phone", reverse("dashboard:screen_list"))
        scene("الطريقة الثالثة: الرابط المباشر", "افتح الرابط في متصفح جهاز العرض", f"على التلفاز أو الحاسب المتصل بالشاشة: افتح school-display.com{short_path}", "استخدم رابط مدرستك الفعلي؛ الرابط الظاهر هنا خاص بالتجربة.", 7)
        page.wait_for_timeout(3000)
        scene("الطريقة الثالثة: الرابط المباشر", "اترك صفحة العرض مفتوحة", "يبدأ العرض من الرابط المباشر، ويُربط بأول جهاز يفتحه.", "عند استخدام حاسب: اعرض المتصفح على الشاشة المتصلة به، وفعّل ملء الشاشة.", 6)

        mode("desktop")
        desktop = navigate("desktop", reverse("dashboard:screen_list"))
        scene("التحقق وإكمال البيانات", "تحقق من حالة الشاشة", "ارجع إلى شاشات العرض وحدّث الحالة. ظهور «متصلة الآن» يعني وصول اتصال من الجهاز.", "الارتباط وحده لا يؤكد أن المحتوى ظاهر أمامك؛ راجع التلفاز أيضًا.", 5)
        point(desktop.locator(".screen-status-badge").first, 3)
        desktop = navigate("desktop", reverse("dashboard:school_data"))
        scene("التحقق وإكمال البيانات", "الربط لا يضيف الجدول تلقائيًا", "أكمل الفصول والمواد والمعلمين من «بيانات المدرسة»، أو استخدم الاستيراد من Excel.", "هذه القوائم هي أساس توزيع المواد والمعلمين على حصص الفصول.", 7)
        point(desktop.get_by_role("link", name="استيراد من Excel"), 2)
        desktop = navigate("desktop", reverse("dashboard:days_list"))
        scene("التحقق وإكمال البيانات", "اضبط الأيام والتوقيتات", "فعّل أيام الدراسة، ثم اضبط بداية ونهاية الحصص والفسح لكل يوم.", "التوقيت يحدد متى تتغير الحصة على شاشة المدرسة.", 7)
        point(desktop.locator("a[href='" + reverse("dashboard:day_edit", args=[7]) + "']"), 2)
        desktop = navigate("desktop", reverse("dashboard:timetable_day") + "?weekday=7")
        scene("التحقق وإكمال البيانات", "وزّع المواد والمعلمين على الحصص", "من «الجدول اليومي»، أسند المادة والمعلم لكل فصل وحصة، ثم تحقق من حفظ التغييرات.", "ضبط أوقات الحصص وحده لا يكفي لظهور جدول الفصل.", 8)
        point(desktop.locator("[data-tt-subject]").first, 2)
        mode("split")
        tv = navigate("tv", short_path)
        mobile = navigate("phone", reverse("dashboard:screen_list"))
        scene("التحقق وإكمال البيانات", "الشاشة مرتبطة، والمحتوى قابل للتحديث", "اختر طريقة ربط واحدة، أكمل بيانات المدرسة والجدول، ثم راجع العرض الفعلي.", "إذا لم تظهر حصة الآن، تحقق من يوم الدراسة ووقتها وإسناد المادة والمعلم.", 8)
        duration = time.monotonic() - started
        video = page.video
        # Retain readable evidence frames without adding them to the application.
        page.screenshot(path=str(work / "final.png"))
        context.close()
        video_path = Path(video.path())
        browser.close()
    database_reads.shutdown()
    timeline[-1]["end"] = duration
    for index in range(len(timeline) - 1):
        timeline[index]["end"] = timeline[index + 1]["start"]
    (work / "timeline.json").write_text(json.dumps({"duration": duration, "scenes": timeline, "chapters": chapters}, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.rehearsal:
        print(f"Rehearsal completed: {work}", flush=True)
        return
    output = ROOT / "static" / "videos"
    output.mkdir(exist_ok=True)
    captions = "WEBVTT\n\n" + "\n\n".join(f"{stamp(s['start'])} --> {stamp(s['end'])}\n{s['text']}\n{s['hint']}" for s in timeline)
    (output / "first-screen-pairing-ar.vtt").write_text(captions + "\n", encoding="utf-8")
    chapter_vtt = "WEBVTT\n\n" + "\n\n".join(f"{stamp(c['start'])} --> {stamp(chapters[i+1]['start'] if i+1 < len(chapters) else duration)}\n{c['title']}" for i, c in enumerate(chapters))
    (output / "first-screen-pairing-chapters-ar.vtt").write_text(chapter_vtt + "\n", encoding="utf-8")
    Image.open(work / "poster.png").convert("RGB").save(output / "first-screen-pairing-poster.jpg", quality=88, optimize=True)
    # Browser capture starts before navigation; trim the setup frames precisely.
    offset = max(0, duration_of(args.ffmpeg, video_path) - duration)
    command = [args.ffmpeg, "-y", "-ss", str(offset), "-i", str(video_path), "-t", str(duration),
               "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-r", "25",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output / "first-screen-pairing-ar.mp4")]
    print("Encoding MP4...", flush=True)
    with (work / "encode.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, check=True, stdout=log, stderr=log)
    print(json.dumps({"duration": duration, "chapters": chapters, "video": str(output / "first-screen-pairing-ar.mp4"), "work": str(work)}, ensure_ascii=False), flush=True)


def duration_of(ffmpeg, path):
    import re
    result = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True)
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not match:
        raise RuntimeError("Could not read video duration")
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--rehearsal", action="store_true", help="Exercise every real flow quickly without replacing final assets")
    args = parser.parse_args()
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="school-pairing-video-"))
    work.mkdir(parents=True, exist_ok=True)
    logging.disable(logging.CRITICAL)
    print(f"Preparing isolated demo in {work}", flush=True)
    cookie, school_id = setup(work)
    server = serve(work)
    try:
        capture(args, work, cookie)
    finally:
        server.shutdown()
