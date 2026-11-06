"""Review all deletions on mobile, replace the timetable, then undo it."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from django.db import close_old_connections
from django.test import override_settings
from django.urls import reverse

from dashboard.document_import import normalize_extraction
from dashboard.excel_import import _import_state_fingerprint
from dashboard.test_timetable_replacement import replacement_fixture, replacement_raw
from dashboard import test_document_import as fixtures
from schedule.models import ClassLesson, SchoolClass

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


def read_database(callback):
    """Playwright's sync wrapper runs a loop; keep ORM reads off that thread."""
    def read():
        try:
            return callback()
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(read).result()


@unittest.skipIf(sync_playwright is None, "Playwright is required")
@override_settings(DOCUMENT_IMPORT_ENABLED=True, OPENAI_API_KEY="fake-key", ALLOWED_HOSTS=["localhost", "testserver"], PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReplacementBrowserTests(StaticLiveServerTestCase):
    def test_review_paginated_deletions_replace_and_restore_original_timetable(self):
        cache.clear()
        replacement_fixture(type(self))
        extra_classes = SchoolClass.objects.bulk_create([SchoolClass(settings=self.school_settings, name=f"حذف {index}") for index in range(1, 206)])
        ClassLesson.objects.bulk_create([ClassLesson(settings=self.school_settings, school_class=school_class, weekday=7, period_index=1, subject=self.math, teacher=self.teachers["أحمد"]) for school_class in extra_classes])
        before = _import_state_fingerprint(self.school_settings)
        other_before = _import_state_fingerprint(self.other_settings)
        self.client.force_login(self.user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
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
                self.assertEqual(page.locator("#uploadStrategy").input_value(), "merge")
                page.locator("#uploadStrategy").select_option("replace")
                page.locator("[name=document]").set_input_files({"name": "school.png", "mimeType": "image/png", "buffer": fixtures.image_upload().read()})
                with patch("dashboard.views_document_import.extract_document", return_value=normalize_extraction(replacement_raw())):
                    page.locator("[data-extract-form] button[type=submit]").click()
                    page.wait_for_url("**?draft=*")
                self.assertEqual(page.locator("#reviewStrategy").input_value(), "replace")
                page.locator("[data-review-form] button[type=submit]").click()
                page.locator("[data-import-confirmation]").wait_for()
                changes = json.loads(page.locator("#importChangesData").text_content())
                self.assertEqual(sum(row["kind"] == "deleted" for row in changes), 206)
                page.locator("[data-change-filter]").select_option("deleted")
                self.assertEqual(page.locator("[data-changes-body] tr").count(), 100)
                page.locator("[data-changes-next]").click()
                self.assertEqual(page.locator("[data-changes-body] tr").count(), 100)
                page.locator("[data-changes-next]").click()
                self.assertEqual(page.locator("[data-changes-body] tr").count(), 6)
                page.locator("[data-change-search]").fill("حذف 205")
                self.assertEqual(page.locator("[data-changes-body] tr").count(), 1)
                page.locator("[data-change-search]").fill("")
                page.locator("[data-change-filter]").select_option("moved")
                moved = page.locator("[data-changes-body]").text_content()
                self.assertIn("الأحد", moved)
                self.assertIn("الاثنين", moved)
                for width in (390, 1440):
                    page.set_viewport_size({"width": width, "height": 900})
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth + 2"))
                page.locator("#reviewStrategy").select_option("merge")
                self.assertTrue(page.locator("[data-final-import]").is_disabled())
                page.locator("#reviewStrategy").select_option("replace")
                page.locator("[data-review-form] button[type=submit]").click()
                page.locator("[data-import-confirmation]").wait_for()
                self.assertEqual(read_database(lambda: _import_state_fingerprint(self.school_settings)), before)
                page.locator("[name=review_confirmed]").check()
                page.locator("[name=replace_confirmed]").check()
                page.locator("[data-final-import]").click()
                page.wait_for_url("**?completed=*")
                self.assertEqual(read_database(lambda: ClassLesson.objects.filter(settings=self.school_settings).count()), 4)
                self.assertFalse(read_database(lambda: ClassLesson.objects.filter(pk=self.deleting.pk).exists()))
                self.assertTrue(read_database(lambda: ClassLesson.objects.filter(pk=self.moving.pk, weekday=1, period_index=2).exists()))
                page.get_by_role("button", name="التراجع عن آخر استيراد", exact=True).click()
                page.locator("[data-confirm-submit]").click()
                page.wait_for_url("**/school-data/import/")
                self.assertEqual(read_database(lambda: _import_state_fingerprint(self.school_settings)), before)
                self.assertEqual(read_database(lambda: _import_state_fingerprint(self.other_settings)), other_before)
                self.assertEqual(errors, [])
            finally:
                browser.close()
