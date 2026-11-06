"""Document import must remain review-first, tenant-scoped and reversible."""
from copy import deepcopy
from datetime import time
from io import BytesIO
import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from pypdf import PdfWriter
import requests

from core.models import School, SubscriptionPlan, UserProfile
from dashboard.document_import import (
    DocumentImportError, MAX_FILE_BYTES, extract_document, normalize_extraction,
    parse_draft, prepare_draft, validate_upload,
)
from schedule.models import ClassLesson, DaySchedule, Period, SchoolClass, SchoolSettings, Subject, Teacher
from subscriptions.models import SchoolSubscription


def extraction():
    return {
        "teachers": ["أحمد محمد"], "subjects": ["الرياضيات"], "classes": ["الأول أ"],
        "rows": [{"day": "الأحد", "period_index": "1", "starts_at": "07:00", "ends_at": "07:45",
                  "class_name": "الأول أ", "subject_name": "الرياضيات", "teacher_name": "أحمد محمد",
                  "source": "صفحة 1", "note": ""}],
        "warnings": [], "truncated": False,
    }


def image_upload(name="schedule.png"):
    output = BytesIO()
    Image.new("RGB", (40, 40), "white").save(output, "PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


def pdf_upload(pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if encrypted:
        writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    return SimpleUploadedFile("schedule.pdf", output.getvalue(), content_type="application/pdf")


@override_settings(DOCUMENT_IMPORT_ENABLED=True, OPENAI_API_KEY="test-server-key", DOCUMENT_IMPORT_MODEL="gpt-4.1")
class DocumentReaderTests(SimpleTestCase):
    def test_validates_real_image_and_pdf(self):
        data, mime, pages = validate_upload(image_upload())
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual((mime, pages), ("image/png", 1))
        self.assertEqual(validate_upload(pdf_upload(2))[1:], ("application/pdf", 2))

    def test_rejects_invalid_disguised_encrypted_and_long_uploads(self):
        for upload in (
            None, SimpleUploadedFile("x.png", b"not an image"), image_upload("x.jpg"),
            SimpleUploadedFile("x.pdf", b"not a pdf"), pdf_upload(11), pdf_upload(encrypted=True),
            SimpleUploadedFile("x.svg", b"<svg/>"),
            SimpleUploadedFile("large.pdf", b"x" * (MAX_FILE_BYTES + 1)),
        ):
            with self.subTest(name=getattr(upload, "name", None)), self.assertRaises(DocumentImportError):
                validate_upload(upload)

    def test_rejects_animated_and_large_pixel_images(self):
        output = BytesIO()
        Image.new("RGB", (20, 20)).save(output, "WEBP", save_all=True, append_images=[Image.new("RGB", (20, 20), "red")])
        with self.assertRaises(DocumentImportError):
            validate_upload(SimpleUploadedFile("x.webp", output.getvalue()))
        with patch("dashboard.document_import.MAX_IMAGE_PIXELS", 100), self.assertRaises(DocumentImportError):
            validate_upload(image_upload())

    def test_phone_orientation_is_corrected_and_exif_is_removed(self):
        output = BytesIO()
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = "private author"
        Image.new("RGB", (30, 20), "white").save(output, "JPEG", exif=exif)
        data, _, _ = validate_upload(SimpleUploadedFile("phone.jpg", output.getvalue()))
        with Image.open(BytesIO(data)) as result:
            self.assertEqual(result.size, (20, 30))
            self.assertFalse(result.getexif())

    @patch("dashboard.document_import.requests.post")
    def test_openai_request_uses_inline_pdf_schema_and_no_storage(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(extraction())}]}]}
        result = extract_document(data=b"pdf bytes", mime_type="application/pdf", mode="timetable")
        self.assertEqual(result["rows"][0]["teacher_name"], "أحمد محمد")
        args, kwargs = post.call_args
        self.assertEqual(args, ("https://api.openai.com/v1/responses",))
        self.assertFalse(kwargs["json"]["store"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["json"]["text"]["format"]["strict"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-server-key")
        attachment = kwargs["json"]["input"][0]["content"][1]
        self.assertEqual(attachment["type"], "input_file")
        self.assertTrue(attachment["file_data"].startswith("data:application/pdf;base64,"))

    @patch("dashboard.document_import.requests.post")
    def test_image_and_names_only_request(self, post):
        result = extraction()
        result["rows"] = []
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}]}
        extract_document(data=b"image", mime_type="image/png", mode="entities")
        content = post.call_args.kwargs["json"]["input"][0]["content"]
        self.assertEqual(content[1]["type"], "input_image")
        self.assertIn("فقط", content[0]["text"])

    @patch("dashboard.document_import.requests.post")
    def test_provider_errors_never_expose_response_or_key(self, post):
        for status in (401, 403, 429, 500, 302):
            post.return_value = Mock(status_code=status, text="secret-key raw provider error")
            with self.subTest(status=status), self.assertRaises(DocumentImportError) as error:
                extract_document(data=b"x", mime_type="image/png", mode="timetable")
            self.assertNotIn("secret-key", str(error.exception))
        post.side_effect = requests.Timeout("private content")
        with self.assertRaises(DocumentImportError):
            extract_document(data=b"x", mime_type="image/png", mode="timetable")

    @patch("dashboard.document_import.requests.post")
    def test_incomplete_refused_malformed_and_empty_output_rejected(self, post):
        for body in (
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "broken json"}]}]},
            {"status": "completed", "output": []},
            [],
        ):
            post.return_value = Mock(status_code=200)
            post.return_value.json.return_value = body
            with self.subTest(body=body), self.assertRaises(DocumentImportError):
                extract_document(data=b"x", mime_type="image/png", mode="timetable")

    @override_settings(OPENAI_API_KEY="")
    @patch("dashboard.document_import.requests.post")
    def test_unconfigured_never_calls_provider(self, post):
        with self.assertRaises(DocumentImportError):
            extract_document(data=b"x", mime_type="image/png", mode="timetable")
        post.assert_not_called()

    def test_normalizes_arabic_digits_days_and_whitespace(self):
        data = extraction()
        data["rows"][0].update(day="الاحد", period_index="١", starts_at="٠٧:٠٠", teacher_name="  أحمد   محمد ")
        result = normalize_extraction(data)
        self.assertEqual(result["rows"][0]["day"], "الأحد")
        self.assertEqual(result["rows"][0]["starts_at"], "07:00")
        self.assertEqual(result["rows"][0]["teacher_name"], "أحمد محمد")

    def test_does_not_accept_silent_truncation_wrong_types_or_empty_data(self):
        for changes in (
            {"truncated": True}, {"rows": [{}] * 301}, {"rows": ["invalid"]},
            {"teachers": "invalid"}, {"rows": [], "teachers": [], "subjects": [], "classes": []},
            {"warnings": "invalid"}, {"rows": [{"teacher_name": {"bad": "value"}}]},
        ):
            with self.subTest(changes=str(changes)[:80]), self.assertRaises(DocumentImportError):
                normalize_extraction({**extraction(), **changes})


@override_settings(DOCUMENT_IMPORT_ENABLED=True, OPENAI_API_KEY="test-server-key", DOCUMENT_IMPORT_HOURLY_LIMIT=10, DOCUMENT_IMPORT_DAILY_LIMIT=40)
class DocumentImportFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="مدرسة الاستيراد الذكي", slug="document-import")
        cls.other = School.objects.create(name="مدرسة أخرى", slug="document-other")
        cls.school_settings = SchoolSettings.objects.create(school=cls.school, name=cls.school.name)
        cls.other_settings = SchoolSettings.objects.create(school=cls.other, name=cls.other.name)
        cls.user = get_user_model().objects.create_user(username="document-manager")
        profile = UserProfile.objects.create(user=cls.user, active_school=cls.school)
        profile.schools.add(cls.school, cls.other)
        plan = SubscriptionPlan.objects.create(code="document-import", name="اختبار", price=10, duration_days=365)
        for school in (cls.school, cls.other):
            SchoolSubscription.objects.create(school=school, plan=plan, starts_at=timezone.localdate(), status="active")

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)
        self.url = reverse("dashboard:school_document_import")

    def extract(self, result=None, mode="timetable"):
        with patch("dashboard.views_document_import.extract_document", return_value=normalize_extraction(result or extraction())):
            response = self.client.post(self.url, {"action": "extract", "document": image_upload(), "mode": mode})
        self.assertEqual(response.status_code, 302)
        preview = self.client.get(response.url)
        self.assertEqual(preview.status_code, 200)
        return preview.context["draft_token"], preview.context["draft"]

    def review(self, token, draft, **overrides):
        post = {"action": "review", "draft_token": token, "draft_rows": json.dumps(draft["rows"]), "mode": draft["mode"]}
        post.update({key: "\n".join(values) for key, values in draft["entities"].items()})
        post.update(overrides)
        return self.client.post(self.url, post)

    def save(self, token, confirmed=True):
        return self.client.post(reverse("dashboard:school_data_import"), {"action": "import", "import_token": token, "review_confirmed": "yes" if confirmed else ""})

    def test_page_and_entry_links_render_and_never_expose_key(self):
        response = self.client.get(self.url)
        self.assertContains(response, "قراءة الملف واستخراج البيانات")
        self.assertContains(response, "OpenAI")
        self.assertNotContains(response, "test-server-key")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertContains(self.client.get(reverse("dashboard:school_data")), self.url)

    @override_settings(OPENAI_API_KEY="")
    @patch("dashboard.views_document_import.extract_document")
    def test_disabled_blocks_submission(self, extract):
        response = self.client.post(self.url, {"action": "extract", "document": image_upload()})
        self.assertEqual(response.status_code, 400)
        extract.assert_not_called()

    def test_extract_and_review_do_not_create_school_data(self):
        token, draft = self.extract()
        self.assertFalse(Teacher.objects.exists())
        response = self.review(token, draft)
        self.assertTrue(response.context["import_token"])
        self.assertFalse(Teacher.objects.exists())
        self.assertFalse(ClassLesson.objects.exists())
        self.assertContains(response, "راجع ملخص الإضافات")

    def test_saves_all_entities_lessons_and_undoes_via_existing_flow(self):
        token, draft = self.extract()
        response = self.review(token, draft)
        approved = response.context["import_token"]
        self.save(approved, confirmed=False)
        self.assertFalse(Teacher.objects.exists())
        result = self.save(approved)
        self.assertEqual(result.status_code, 302)
        lesson = ClassLesson.objects.get()
        self.assertEqual(lesson.teacher.name, "أحمد محمد")
        self.assertEqual(lesson.subject.name, "الرياضيات")
        self.assertEqual(lesson.school_class.name, "الأول أ")
        self.assertEqual(Period.objects.get().starts_at, time(7))
        completed = self.client.get(result.url)
        undo = completed.context["completed_import"]["token"]
        self.client.post(reverse("dashboard:school_data_import"), {"action": "undo", "undo_token": undo})
        self.assertFalse(ClassLesson.objects.exists())
        self.assertFalse(Teacher.objects.exists())

    def test_reuses_existing_names_and_never_creates_misread_corrected_name(self):
        Teacher.objects.create(school=self.school, name="محمد أحمد")
        token, draft = self.extract()
        draft["rows"][0]["teacher_name"] = "محمد أحمد"
        response = self.review(token, draft)
        self.save(response.context["import_token"])
        self.assertEqual(list(Teacher.objects.values_list("name", flat=True)), ["محمد أحمد"])

    def test_missing_times_are_editable_not_invented(self):
        result = extraction()
        result["rows"][0].update(starts_at="", ends_at="")
        token, draft = self.extract(result)
        response = self.review(token, draft)
        self.assertFalse(response.context["import_token"])
        self.assertContains(response, "وقت البداية غير صحيح")
        draft["rows"][0].update(starts_at="07:00", ends_at="07:45")
        self.assertTrue(self.review(token, draft).context["import_token"])

    def test_missing_times_reuse_only_same_school_day_and_period(self):
        day = DaySchedule.objects.create(settings=self.school_settings, weekday=7, periods_count=1)
        Period.objects.create(day=day, index=1, starts_at=time(8), ends_at=time(8, 45))
        other_day = DaySchedule.objects.create(settings=self.other_settings, weekday=7, periods_count=1)
        Period.objects.create(day=other_day, index=1, starts_at=time(9), ends_at=time(9, 45))
        result = normalize_extraction(extraction())
        result["rows"][0].update(starts_at="", ends_at="")
        draft = prepare_draft(school=self.school, extraction=result)
        self.assertEqual(draft["rows"][0]["starts_at"], "08:00")
        self.assertIn("استُكملت", draft["warnings"][0])

    def test_names_only_does_not_require_or_create_lessons(self):
        result = extraction()
        result["rows"] = []
        token, draft = self.extract(result, mode="entities")
        response = self.review(token, draft)
        self.save(response.context["import_token"])
        self.assertEqual(Teacher.objects.get().name, "أحمد محمد")
        self.assertEqual(Subject.objects.get().name, "الرياضيات")
        self.assertEqual(SchoolClass.objects.get().name, "الأول أ")
        self.assertFalse(ClassLesson.objects.exists())
        self.assertFalse(DaySchedule.objects.exists())

    def test_switch_to_names_only_extracts_names_from_unusable_lesson(self):
        token, draft = self.extract()
        draft["rows"][0].update(day="", starts_at="", ends_at="")
        response = self.review(token, draft, mode="entities")
        self.save(response.context["import_token"])
        self.assertEqual(Teacher.objects.count(), 1)
        self.assertFalse(ClassLesson.objects.exists())

    def test_duplicate_teacher_and_period_conflicts_block_approval(self):
        result = extraction()
        row = deepcopy(result["rows"][0])
        row["class_name"] = "الثاني أ"
        result["rows"].append(row)
        token, draft = self.extract(result)
        response = self.review(token, draft)
        self.assertFalse(response.context["import_token"])
        self.assertContains(response, "المعلم مرتبط في الوقت نفسه")

    def test_db_conflict_at_save_rolls_back_entire_import(self):
        teacher = Teacher.objects.create(school=self.school, name="أحمد محمد")
        subject = Subject.objects.create(school=self.school, name="الرياضيات")
        other_class = SchoolClass.objects.create(settings=self.school_settings, name="الثاني أ")
        ClassLesson.objects.create(settings=self.school_settings, school_class=other_class, weekday=7, period_index=1, subject=subject, teacher=teacher)
        token, draft = self.extract()
        approved = self.review(token, draft).context["import_token"]
        failed = self.save(approved)
        self.assertEqual(failed.url, f"{self.url}?draft={token}")
        self.assertEqual(ClassLesson.objects.count(), 1)
        self.assertFalse(SchoolClass.objects.filter(name="الأول أ").exists())
        self.assertFalse(Period.objects.exists())
        self.assertIsNone(cache.get(f"school-data-import-running:{self.school.pk}"))

    def test_concurrent_import_lease_blocks_writes_and_allows_retry(self):
        token, draft = self.extract()
        approved = self.review(token, draft).context["import_token"]
        lock_key = f"school-data-import-running:{self.school.pk}"
        cache.set(lock_key, "other-import")
        self.save(approved)
        self.assertFalse(Teacher.objects.exists())
        self.assertEqual(cache.get(lock_key), "other-import")
        cache.delete(lock_key)
        self.save(approved)
        self.assertEqual(ClassLesson.objects.count(), 1)

    def test_formula_like_name_remains_literal_text(self):
        token, draft = self.extract()
        draft["rows"][0]["teacher_name"] = "=اسم المعلم"
        approved = self.review(token, draft).context["import_token"]
        self.assertTrue(approved)
        self.save(approved)
        self.assertEqual(Teacher.objects.get().name, "=اسم المعلم")

    def test_review_and_approved_tokens_are_bound_to_school_and_user(self):
        token, draft = self.extract()
        approved = self.review(token, draft).context["import_token"]
        UserProfile.objects.filter(user=self.user).update(active_school=self.other)
        response = self.review(token, draft)
        self.assertEqual(response.status_code, 400)
        self.save(approved)
        self.assertFalse(Teacher.objects.exists())
        UserProfile.objects.filter(user=self.user).update(active_school=self.school)
        other_user = get_user_model().objects.create_user(username="second-manager")
        profile = UserProfile.objects.create(user=other_user, active_school=self.school)
        profile.schools.add(self.school)
        self.client.force_login(other_user)
        self.assertEqual(self.review(token, draft).status_code, 400)
        self.save(approved)
        self.assertFalse(Teacher.objects.exists())

    def test_expired_draft_is_rejected(self):
        response = self.client.post(self.url, {"action": "review", "draft_token": "a" * 40, "draft_rows": "[]"})
        self.assertEqual(response.status_code, 400)

    def test_revalidating_revokes_old_approval_and_replay_is_ignored(self):
        token, draft = self.extract()
        old = self.review(token, draft).context["import_token"]
        new = self.review(token, draft).context["import_token"]
        self.save(old)
        self.assertFalse(Teacher.objects.exists())
        self.save(new)
        self.save(new)
        self.assertEqual(ClassLesson.objects.count(), 1)

    def test_malformed_edited_rows_and_names_cannot_bypass_validation(self):
        token, draft = self.extract()
        for overrides in ({"draft_rows": "invalid"}, {"draft_rows": "{}"}, {"teachers": "x" * 60001}, {"draft_rows": "x" * 400001}):
            response = self.review(token, draft, **overrides)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.context["import_token"])

    @override_settings(DOCUMENT_IMPORT_HOURLY_LIMIT=1)
    @patch("dashboard.views_document_import.extract_document")
    def test_rate_limit_and_per_school_inflight_lock(self, provider):
        provider.return_value = normalize_extraction(extraction())
        first = self.client.post(self.url, {"action": "extract", "document": image_upload()})
        self.assertEqual(first.status_code, 302)
        limited = self.client.post(self.url, {"action": "extract", "document": image_upload()})
        self.assertEqual(limited.status_code, 400)
        self.assertEqual(provider.call_count, 1)
        cache.clear()
        cache.set(f"school-document-running:{self.school.pk}", "another-job")
        busy = self.client.post(self.url, {"action": "extract", "document": image_upload()})
        self.assertEqual(busy.status_code, 400)
        self.assertEqual(provider.call_count, 1)

    def test_similar_name_warning_does_not_mutate_cached_draft_or_merge(self):
        Teacher.objects.create(school=self.school, name="أحمد محمد علي")
        draft = normalize_extraction(extraction())
        draft["rows"][0]["teacher_name"] = "أحمد محمد على"
        parsed = parse_draft(school=self.school, draft=draft)
        self.assertTrue(any("قريب" in value for value in parsed["warnings"]))
        self.assertEqual(draft["warnings"], [])

    @override_settings(DOCUMENT_IMPORT_DAILY_LIMIT=1)
    @patch("dashboard.views_document_import.extract_document")
    def test_daily_limit_applies_independently_of_hourly_window(self, provider):
        provider.return_value = normalize_extraction(extraction())
        self.assertEqual(self.client.post(self.url, {"action": "extract", "document": image_upload()}).status_code, 302)
        cache.delete(f"school-document-quota:{self.school.pk}:hour")
        self.assertEqual(self.client.post(self.url, {"action": "extract", "document": image_upload()}).status_code, 400)
        self.assertEqual(provider.call_count, 1)

    def test_unauthenticated_and_unauthorized_users_cannot_extract(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        outsider = get_user_model().objects.create_user(username="document-outsider")
        self.client.force_login(outsider)
        self.assertIn(self.client.post(self.url, {"action": "extract"}).status_code, (302, 403))
