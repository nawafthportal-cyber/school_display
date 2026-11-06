import asyncio
import unittest

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from .test_school_dashboard_browser import LAYOUT_FACTS
from . import tests as admin_fixtures

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright is required")
@override_settings(TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminBrowserRemediationTests(StaticLiveServerTestCase):
    def setUp(self):
        cache.clear()
        admin_fixtures.SystemAdminConsoleSmokeTests.setUp(self)
        self.client.force_login(self.owner)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch()
        except Exception as exc:
            self.playwright.stop()
            raise unittest.SkipTest(str(exc))
        self.context = self.browser.new_context(service_workers="block")
        self.context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": cookie, "url": self.live_server_url}])
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.context.close()
        self.browser.close()
        self.playwright.stop()

    def test_admin_forms_have_accessible_names_on_phone_and_desktop(self):
        paths = [
            reverse("dashboard:system_school_create"), reverse("dashboard:system_school_edit", args=[self.school.pk]),
            reverse("dashboard:system_user_create"), reverse("dashboard:system_user_edit", args=[self.manager.pk]),
            reverse("dashboard:system_subscription_create"), reverse("dashboard:system_subscription_edit", args=[self.subscription.pk]),
            reverse("dashboard:system_screen_addon_create"), reverse("dashboard:system_screen_addon_edit", args=[self.addon.pk]),
            reverse("dashboard:system_subscription_request_detail", args=[self.request.pk]),
            reverse("dashboard:system_support_ticket_detail", args=[self.ticket.pk]),
            reverse("dashboard:system_mail_list"), reverse("dashboard:system_mail_compose"),
        ]
        for width in (390, 1440):
            self.page.set_viewport_size({"width": width, "height": 900})
            for path in paths:
                with self.subTest(path=path, width=width):
                    response = self.page.goto(self.live_server_url+path, wait_until="networkidle")
                    self.assertEqual(response.status, 200)
                    facts = self.page.evaluate(LAYOUT_FACTS)
                    self.assertEqual(facts["unlabelled"], [], facts)
                    self.assertLessEqual(facts["overflow"], 1, facts)
        self.assertEqual(self.errors, [])

    def test_mobile_closed_menu_is_inert_and_open_menu_traps_focus(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.goto(self.live_server_url+reverse("dashboard:system_admin_dashboard"), wait_until="networkidle")
        self.assertTrue(self.page.locator("#adminSidebar").evaluate("n=>n.inert"))
        self.page.keyboard.press("Tab")
        self.assertFalse(self.page.evaluate("!!document.activeElement.closest('#adminSidebar')"))
        self.page.get_by_role("button", name="فتح قائمة الإدارة", exact=True).click()
        self.assertFalse(self.page.locator("#adminSidebar").evaluate("n=>n.inert"))
        self.assertEqual(self.page.evaluate("document.activeElement.id"), "adminSidebarCloseBtn")
        self.page.keyboard.press("Shift+Tab")
        self.assertTrue(self.page.evaluate("!!document.activeElement.closest('#adminSidebar')"))
        self.page.keyboard.press("Tab")
        self.assertEqual(self.page.evaluate("document.activeElement.id"), "adminSidebarCloseBtn")
        self.page.keyboard.press("Escape")
        self.assertTrue(self.page.locator("#adminSidebar").evaluate("n=>n.inert"))
        self.assertEqual(self.page.evaluate("document.activeElement.id"), "adminSidebarOpenBtn")
