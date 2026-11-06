"""Pagination and automatic requests must not drop unseen school data."""
import unittest
from datetime import time
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import School, UserProfile, SubscriptionPlan
from schedule.models import Teacher, ClassLesson, SchoolSettings, DaySchedule, Period
from subscriptions.models import SchoolSubscription
from dashboard.document_batches import plan_document
from dashboard.excel_document_import import EXCEL_MIMES
from dashboard.test_excel_document_import import row_workbook, fake_reader

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "Playwright is required")
@override_settings(DOCUMENT_IMPORT_ENABLED=True, OPENAI_API_KEY="test-key", ALLOWED_HOSTS=["localhost", "testserver"], PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DocumentBatchBrowserTests(StaticLiveServerTestCase):
    def test_excel_automatic_jobs_and_editing_across_review_pages(self):
        cache.clear()
        school = School.objects.create(name="مدرسة مراجعة الدفعات", slug="batch-browser")
        user = get_user_model().objects.create_user(username="batch-browser")
        profile = UserProfile.objects.create(user=user, active_school=school)
        profile.schools.add(school)
        plan = SubscriptionPlan.objects.create(code="batch-browser", name="اختبار", price=10, duration_days=365)
        SchoolSubscription.objects.create(school=school, plan=plan, starts_at=timezone.localdate(), status="active")
        school_settings = SchoolSettings.objects.create(school=school, name=school.name)
        day = DaySchedule.objects.create(settings=school_settings, weekday=7, periods_count=1)
        Period.objects.create(day=day, index=1, starts_at=time(8, 10), ends_at=time(8, 55))
        self.client.force_login(user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        source = row_workbook(205)
        parts = plan_document(data=source, mime_type=EXCEL_MIMES[".xlsx"])
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as error:
                raise unittest.SkipTest(f"Chromium unavailable: {error}")
            page = browser.new_page(viewport={"width": 390, "height": 900})
            page.context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": cookie, "url": self.live_server_url}])
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(self.live_server_url+reverse("dashboard:school_document_import"))
                page.locator("[name=document]").set_input_files({"name": "table.xlsx", "mimeType": EXCEL_MIMES[".xlsx"], "buffer": source})
                with patch("dashboard.excel_document_import.extract_document", side_effect=fake_reader) as provider:
                    page.locator("[data-extract-form] button[type=submit]").click()
                    page.wait_for_url("**?draft=*", timeout=30000)
                    page.locator("[data-rows-body] tr").first.wait_for()
                    self.assertEqual(provider.call_count, len(parts))
                self.assertEqual(page.locator("[data-rows-body] tr").count(), 100)
                self.assertEqual(page.locator("[data-field=starts_at]").first.input_value(), "08:10")
                self.assertTrue(page.locator("[data-field=starts_at]").first.evaluate("input => input.readOnly"))
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth + 2"))
                page.locator("[data-field=teacher_name]").first.fill("تصحيح في الصفحة الأولى")
                page.locator("[data-page-next]").click()
                page.locator("[data-field=teacher_name]").first.fill("تصحيح في الصفحة الثانية")
                page.locator("[data-page-previous]").click()
                self.assertEqual(page.locator("[data-field=teacher_name]").first.input_value(), "تصحيح في الصفحة الأولى")
                page.locator("[data-row-search]").fill("تصحيح في الصفحة الثانية")
                self.assertEqual(page.locator("[data-rows-body] tr").count(), 1)
                page.locator("[data-row-search]").fill("")
                page.get_by_text("تصحيح اسم أو رمز متكرر في جميع الحصص", exact=True).click()
                page.locator("[data-bulk-field]").select_option("subject_name")
                page.locator("[data-bulk-old]").fill("الرياضيات")
                page.locator("[data-bulk-new]").fill("مادة مصححة")
                page.locator("[data-bulk-apply]").click()
                page.locator("[data-review-form] button[type=submit]").click()
                page.wait_for_load_state("load")
                import json
                rows = json.loads(page.locator("#documentDraftRows").text_content())
                self.assertEqual(len(rows), 205)
                self.assertEqual(rows[0]["teacher_name"], "تصحيح في الصفحة الأولى")
                self.assertEqual(rows[100]["teacher_name"], "تصحيح في الصفحة الثانية")
                self.assertTrue(all(row["subject_name"] == "مادة مصححة" for row in rows))
                self.assertTrue(all(row["starts_at"] == "08:10" for row in rows))
                self.assertEqual(errors, [])
            finally:
                browser.close()
        self.assertFalse(Teacher.objects.exists())
        self.assertFalse(ClassLesson.objects.exists())
