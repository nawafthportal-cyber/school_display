import asyncio
import unittest
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.utils import timezone

from core.models import DisplayScreen, School, SubscriptionPlan, UserProfile
from schedule.models import DisplayTheme, SchoolSettings
from subscriptions.models import SchoolSubscription

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CustomThemeBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        school = School.objects.create(name="مدرسة الإبداع", slug="theme-browser-school")
        user = get_user_model().objects.create_user(username="theme-browser", password="ThemeTest123!")
        profile = UserProfile.objects.create(user=user, active_school=school)
        profile.schools.add(school)
        theme = DisplayTheme.objects.create(school=school, name="الهوية الزرقاء", card_color="#142033")
        SchoolSettings.objects.create(school=school, name=school.name, custom_theme=theme)
        plan = SubscriptionPlan.objects.create(code="theme-browser-plan", name="باقة", price=1, duration_days=365, max_screens=5)
        SchoolSubscription.objects.create(school=school, plan=plan, starts_at=timezone.localdate(), status="active")
        screen = DisplayScreen.objects.create(school=school, name="شاشة المعاينة")
        self.screen_id, self.short_code = screen.pk, screen.short_code
        self.artifacts = Path(settings.BASE_DIR).parent / ".codex" / "tmp"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch()
        except Exception as exc:
            self.playwright.stop()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.context.add_init_script('window.themeViolations=[];document.addEventListener("securitypolicyviolation",e=>window.themeViolations.push(e.effectiveDirective));')
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.live_server_url + "/dashboard/login/")
        self.page.fill('[name="username"]', "theme-browser")
        self.page.fill('[name="password"]', "ThemeTest123!")
        self.page.click('button[type="submit"]')
        self.page.wait_for_load_state("networkidle")

    def tearDown(self):
        self.context.close()
        self.browser.close()
        self.playwright.stop()

    def test_editor_updates_preview_and_applies_saved_theme(self):
        self.page.goto(self.live_server_url + "/dashboard/themes/new/")
        self.page.wait_for_load_state("networkidle")
        self.page.fill('[name="name"]', "ثيم مدرستنا")
        self.page.select_option('[name="base_theme"]', "emerald")
        self.page.fill('[name="card_color"]', "#15362c")
        self.page.select_option('[name="font"]', "arial")
        self.page.fill('[name="card_radius"]', "12")
        preview = self.page.locator("#themeLivePreview")
        self.assertEqual(preview.evaluate("n=>n.style.getPropertyValue('--glass-surface')"), "#15362c")
        self.assertEqual(preview.evaluate("n=>n.style.getPropertyValue('--custom-radius')"), "12px")
        self.assertTrue(self.page.locator("#saveThemeButton").is_enabled())
        self.page.evaluate("scrollTo(0, 0)")
        self.page.screenshot(path=str(self.artifacts / "custom-theme-editor-desktop.png"), full_page=True)
        self.page.fill('[name="text_color"]', "#15362c")
        self.assertTrue(self.page.locator("#saveThemeButton").is_disabled())
        self.page.fill('[name="text_color"]', "#e2e8f0")
        self.page.click("#saveThemeButton")
        self.page.wait_for_url("**/dashboard/themes/")
        card = self.page.locator(".theme-library-card").filter(has_text="ثيم مدرستنا")
        card.locator('select[name="target"]').select_option(str(self.screen_id))
        card.get_by_role("button", name="تطبيق الثيم", exact=True).click()
        self.page.wait_for_load_state("networkidle")
        self.page.goto(self.live_server_url + f"/s/{self.short_code}/?preview=1")
        self.page.wait_for_function("() => document.body.hasAttribute('data-custom-display-theme')")
        self.page.wait_for_function("() => document.body.dataset.displayState !== 'loading'")
        self.assertEqual(self.page.locator("body").evaluate("n=>n.style.getPropertyValue('--glass-surface')"), "#15362C")
        self.page.screenshot(path=str(self.artifacts / "custom-theme-tv-preview.png"), full_page=True)
        self.assertEqual(self.errors, [])

    def test_occasion_name_palette_and_pattern_are_saved_and_rendered_on_tv(self):
        self.page.goto(self.live_server_url + "/dashboard/occasion-templates/national_day/edit/")
        self.page.fill('[name="name"]', "هوية مدرستنا الوطنية")
        self.page.fill('[name="deep"]', "#10243b")
        self.page.fill('[name="accent"]', "#436a8a")
        self.page.fill('[name="highlight"]', "#f5d083")
        self.page.fill('[name="soft"]', "#f4f6f8")
        self.page.select_option('[name="pattern"]', "dots")
        self.assertEqual(self.page.locator("#occasionLivePreview").evaluate("n=>n.style.getPropertyValue('--occasion-deep')"), "#10243b")
        self.page.click("#saveOccasionTheme")
        self.page.get_by_role("button", name="استعادة التصميم الافتراضي", exact=True).wait_for()
        self.page.reload(wait_until="networkidle")
        self.assertEqual(self.page.input_value('[name="name"]'), "هوية مدرستنا الوطنية")
        self.page.screenshot(path=str(self.artifacts / "occasion-theme-editor-desktop.png"), full_page=True, animations="disabled")
        self.page.goto(self.live_server_url + "/dashboard/announcements/new/?template=national_day")
        self.assertEqual(self.page.locator('[name="occasion_theme"] option:checked').text_content(), "هوية مدرستنا الوطنية")
        self.page.click('button[type="submit"]:has-text("حفظ التنبيه")')
        self.page.wait_for_url("**/dashboard/announcements/")
        self.page.goto(self.live_server_url + f"/s/{self.short_code}/?preview=1")
        self.page.wait_for_function("() => document.body.dataset.occasionTheme === 'national_day'")
        self.page.wait_for_function("() => document.body.style.getPropertyValue('--occasion-deep') === '#10243B'")
        self.assertEqual(self.page.locator("#occasionThemeBadgeLabel").text_content(), "هوية مدرستنا الوطنية")
        self.assertIn("radial-gradient", self.page.locator(".occasion-theme-pattern").evaluate("n=>getComputedStyle(n).backgroundImage"))
        self.page.screenshot(path=str(self.artifacts / "occasion-theme-tv-preview.png"), full_page=True, animations="disabled")
        self.page.goto(self.live_server_url + "/dashboard/occasion-templates/national_day/edit/")
        self.page.get_by_role("button", name="استعادة التصميم الافتراضي", exact=True).click()
        self.page.get_by_role("button", name="استعادة التصميم الافتراضي", exact=True).wait_for(state="hidden")
        self.page.goto(self.live_server_url + f"/s/{self.short_code}/?preview=1")
        self.page.wait_for_function("() => document.body.dataset.occasionTheme === 'national_day'")
        self.assertEqual(self.page.locator("body").evaluate("n=>n.style.getPropertyValue('--occasion-deep')"), "")
        self.assertEqual(self.page.locator("#occasionThemeBadgeLabel").text_content(), "اليوم الوطني السعودي")
        self.assertEqual(self.errors, [])

    def test_mobile_occasion_editor_fits_and_blocks_unreadable_colors(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.goto(self.live_server_url + "/dashboard/occasion-templates/national_day/edit/")
        self.page.wait_for_load_state("networkidle")
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
        self.assertEqual(self.page.evaluate("window.themeViolations"), [])
        self.page.fill('[name="soft"]', "#001d16")
        self.assertTrue(self.page.locator("#saveOccasionTheme").is_disabled())
        self.page.screenshot(path=str(self.artifacts / "occasion-theme-editor-mobile.png"), full_page=True, animations="disabled")
        self.assertEqual(self.errors, [])

    def test_mobile_editor_and_library_fit_without_horizontal_overflow(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        for path in ("/dashboard/themes/new/", "/dashboard/themes/"):
            self.page.goto(self.live_server_url + path)
            self.page.wait_for_load_state("networkidle")
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), path)
            self.assertEqual(self.page.evaluate("window.themeViolations"), [], path)
            if path.endswith("new/"):
                self.assertTrue(self.page.locator(".theme-canvas").evaluate("n=>Array.from(n.children).every(c=>{let a=n.getBoundingClientRect(),b=c.getBoundingClientRect();return b.left>=a.left-1 && b.right<=a.right+1})"))
                self.page.screenshot(path=str(self.artifacts / "custom-theme-editor-mobile.png"), full_page=True)
        self.assertEqual(self.errors, [])

    def test_inherited_theme_survives_a_legacy_snapshot_without_theme_data(self):
        intercepted = []
        def legacy_snapshot(route):
            response = route.fetch()
            payload = response.json()
            payload.get("settings", {}).pop("custom_theme", None)
            intercepted.append(True)
            route.fulfill(response=response, json=payload)
        self.context.route("**/api/display/snapshot/**", legacy_snapshot)
        self.page.goto(self.live_server_url + f"/s/{self.short_code}/?preview=1")
        self.page.wait_for_function("() => document.body.dataset.displayState !== 'loading'")
        self.assertTrue(intercepted)
        self.assertTrue(self.page.locator("body").get_attribute("data-custom-display-theme"))
        self.assertEqual(self.page.locator("body").evaluate("n=>n.style.getPropertyValue('--glass-surface')"), "#142033")

    def test_light_theme_keeps_heading_colour_in_the_live_display(self):
        self.page.goto(self.live_server_url + "/dashboard/themes/new/")
        self.page.fill('[name="name"]', "ثيم فاتح")
        for name, value in {"background_color": "#f8fafc", "card_color": "#ffffff", "text_color": "#0f172a", "heading_color": "#145a6a", "accent_color": "#145a6a"}.items():
            self.page.fill(f'[name="{name}"]', value)
        self.page.click("#saveThemeButton")
        self.page.wait_for_url("**/dashboard/themes/")
        card = self.page.locator(".theme-library-card").filter(has_text="ثيم فاتح")
        card.locator('[name="target"]').select_option("all")
        card.get_by_role("button", name="تطبيق الثيم", exact=True).click()
        self.page.wait_for_load_state("networkidle")
        self.page.goto(self.live_server_url + f"/s/{self.short_code}/?preview=1")
        self.page.wait_for_function("() => document.body.dataset.displayState !== 'loading'")
        self.page.wait_for_function("() => getComputedStyle(document.getElementById('heroTitle')).color === 'rgb(20, 90, 106)'", timeout=5000)
        self.page.screenshot(path=str(self.artifacts / "custom-theme-light-tv-preview.png"), full_page=True)
        self.assertEqual(self.page.locator("#heroTitle").evaluate("n=>getComputedStyle(n).color"), "rgb(20, 90, 106)")
        self.assertEqual(self.page.locator("#heroTitle").evaluate("n=>getComputedStyle(n).webkitTextFillColor"), "rgb(20, 90, 106)")
        for selector in ("#clock", "#countdown"):
            self.assertEqual(self.page.locator(selector).evaluate("n=>getComputedStyle(n).webkitTextFillColor"), "rgb(15, 23, 42)")
        self.assertEqual(self.errors, [])
