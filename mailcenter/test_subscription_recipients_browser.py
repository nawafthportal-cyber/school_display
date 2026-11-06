import asyncio
import unittest
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse

from core.models import UserProfile
from dashboard.test_school_dashboard_browser import LAYOUT_FACTS

from .test_subscription_recipients import SubscriptionRecipientFixtures

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright is required")
@override_settings(TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SubscriptionRecipientBrowserTests(SubscriptionRecipientFixtures, StaticLiveServerTestCase):
    def setUp(self):
        self.create_recipients()
        UserProfile.objects.filter(user__username="manager_active").update(mobile="٠٥٠١٢٣٤٥٦٧")
        UserProfile.objects.filter(user__username="manager_trial_ended").update(mobile="+1 (202) 555-0123")
        owner = get_user_model().objects.create_superuser(username="mail_browser_owner", email="owner@example.com", password="StrongPass123!")
        self.client.force_login(owner)
        if hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch()
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context(service_workers="block")
        self.addCleanup(self.context.close)
        self.context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value, "url": self.live_server_url}])
        self.page = self.context.new_page()

    def test_filter_selection_search_and_layout_on_phone_and_desktop(self):
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        for width in (390, 1440):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 900})
                self.page.goto(self.live_server_url+reverse("dashboard:system_mail_compose"), wait_until="networkidle")
                status = self.page.locator("#id_subscription_status")
                status.select_option("active")
                self.assertEqual(self.page.locator('#schoolChoices input:visible').count(), 5)
                self.page.get_by_role("button", name="تحديد الظاهر", exact=True).click()
                self.assertEqual(self.page.locator('#schoolChoices input:checked').count(), 5)
                status.select_option("expired_trial")
                self.assertEqual(self.page.locator('#schoolChoices input:checked').count(), 0)
                self.assertEqual(self.page.locator('#schoolChoices input:visible').count(), 2)
                self.page.locator("#schoolPickerSearch").fill("trial_ended")
                self.page.get_by_role("button", name="تحديد الظاهر", exact=True).click()
                self.assertEqual(self.page.locator('#schoolChoices input:checked').input_value(), str(self.schools["trial_ended"].pk))
                self.page.locator("#schoolPickerSearch").fill("لا يوجد اسم مطابق")
                self.assertTrue(self.page.locator("#schoolPickerEmpty").is_visible())
                self.assertIn("المدارس المحددة: 1", self.page.locator("#schoolPickerSummary").inner_text())
                self.page.get_by_role("button", name="إلغاء التحديد", exact=True).click()
                self.assertEqual(self.page.locator('#schoolChoices input:checked').count(), 0)
                self.page.locator("#schoolPickerSearch").fill("")
                status.select_option("inactive")
                self.assertEqual(self.page.locator('#schoolChoices input:visible').count(), 8)
                facts = self.page.evaluate(LAYOUT_FACTS)
                self.assertEqual(facts["unlabelled"], [], facts)
                self.assertLessEqual(facts["overflow"], 1, facts)
                self.assertEqual(facts["outside"], [], facts)
                self.page.get_by_role("button", name="تحديد الظاهر", exact=True).click()
                self.page.locator("#id_subject").fill("مسودة")
                self.page.locator("form[method=post]").evaluate("form => { form.noValidate = true; }")
                self.page.locator('form[method=post] button[type=submit]').click()
                self.page.wait_for_load_state("networkidle")
                self.assertEqual(status.input_value(), "inactive")
                self.assertEqual(self.page.locator('#schoolChoices input:checked').count(), 8)
                self.assertEqual(self.page.locator('#schoolChoices input:visible').count(), 8)
        self.assertEqual(errors, [])

    def test_whatsapp_filters_and_prepares_only_a_manual_chat(self):
        self.context.route("https://wa.me/**", lambda route: route.fulfill(status=200, content_type="text/html", body="<p>Mock WhatsApp conversation</p>"))
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        for width in (390, 1440):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 900})
                self.page.goto(self.live_server_url+reverse("dashboard:system_school_whatsapp"), wait_until="networkidle")
                status = self.page.locator("#id_subscription_status")
                status.select_option("active")
                self.assertEqual(self.page.locator('[data-wa-school]:visible').count(), 5)
                chat = self.page.locator('[data-wa-phone="966501234567"]')
                self.assertIsNone(chat.get_attribute("href"))
                self.assertEqual(chat.get_attribute("aria-disabled"), "true")
                message = "السلام عليكم\nتحديث & تفاصيل + اشتراك المدرسة 🌷"
                pages_before = len(self.context.pages)
                self.page.locator("#whatsappMessage").fill(message)
                self.assertEqual(len(self.context.pages), pages_before)
                self.assertEqual(parse_qs(urlparse(chat.get_attribute("href")).query)["text"], [message])
                with self.page.expect_popup() as popup_info:
                    chat.click()
                popup = popup_info.value
                popup.wait_for_load_state()
                self.assertEqual(urlparse(popup.url).netloc, "wa.me")
                self.assertEqual(urlparse(popup.url).path, "/966501234567")
                self.assertEqual(parse_qs(urlparse(popup.url).query)["text"], [message])
                popup.close()
                self.assertTrue(self.page.locator('[data-wa-school]:visible [data-wa-opened]').is_visible())
                self.page.locator("#whatsappMessage").fill("")
                self.assertIsNone(chat.get_attribute("href"))
                status.select_option("expired_trial")
                self.assertEqual(self.page.locator('[data-wa-school]:visible').count(), 2)
                self.assertEqual(self.page.locator('[data-wa-school]:visible [data-wa-phone]').count(), 1)
                self.page.locator("#whatsappSchoolSearch").fill("لا توجد مدرسة بهذا الاسم")
                self.assertTrue(self.page.locator("#whatsappEmpty").is_visible())
                self.page.locator("#whatsappSchoolSearch").fill("")
                facts = self.page.evaluate(LAYOUT_FACTS)
                self.assertEqual(facts["unlabelled"], [], facts)
                self.assertLessEqual(facts["overflow"], 1, facts)
                self.assertEqual(facts["outside"], [], facts)
        self.assertEqual(errors, [])
