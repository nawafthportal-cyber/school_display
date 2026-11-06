"""Replacement must match the reviewed plan and be completely reversible."""
from copy import deepcopy
from datetime import time
from io import BytesIO
import json
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from dashboard import test_document_import as fixtures
from dashboard.document_import import normalize_extraction, parse_draft, prepare_draft
from dashboard.excel_import import (
    _import_state_fingerprint, apply_import, build_import_impact, build_template_bytes, undo_import,
)
from dashboard.document_jobs import job_key
from schedule.models import ClassLesson, DaySchedule, Period, SchoolClass, Subject, Teacher


def replacement_fixture(case):
    fixtures.DocumentImportFlowTests.setUpTestData.__func__(case)
    case.math = Subject.objects.create(school=case.school, name="الرياضيات")
    case.science = Subject.objects.create(school=case.school, name="العلوم")
    case.teachers = {name: Teacher.objects.create(school=case.school, name=name) for name in ("أحمد", "خالد", "عمر", "سعيد")}
    case.classes = {name: SchoolClass.objects.create(settings=case.school_settings, name=name) for name in ("أ", "ب", "ج")}
    for weekday in (1, 2, 3, 4, 7):
        day = DaySchedule.objects.create(settings=case.school_settings, weekday=weekday, periods_count=2)
        for index in (1, 2):
            Period.objects.create(day=day, index=index, starts_at=time(6+index, 10, 15), ends_at=time(6+index, 55, 15))
    case.moving = ClassLesson.objects.create(settings=case.school_settings, school_class=case.classes["أ"], weekday=7, period_index=1, subject=case.math, teacher=case.teachers["أحمد"])
    case.changing = ClassLesson.objects.create(settings=case.school_settings, school_class=case.classes["أ"], weekday=7, period_index=2, subject=case.science, teacher=case.teachers["خالد"])
    case.deleting = ClassLesson.objects.create(settings=case.school_settings, school_class=case.classes["ب"], weekday=2, period_index=1, subject=case.science, teacher=case.teachers["عمر"], is_active=False)
    case.unchanged = ClassLesson.objects.create(settings=case.school_settings, school_class=case.classes["ج"], weekday=3, period_index=1, subject=case.math, teacher=case.teachers["سعيد"])
    other_teacher = Teacher.objects.create(school=case.other, name="أحمد")
    other_class = SchoolClass.objects.create(settings=case.other_settings, name="أ")
    other_subject = Subject.objects.create(school=case.other, name="الرياضيات")
    case.other_lesson = ClassLesson.objects.create(settings=case.other_settings, school_class=other_class, weekday=7, period_index=1, teacher=other_teacher, subject=other_subject)


def replacement_raw():
    result = fixtures.extraction()
    base = {**result["rows"][0], "starts_at": "", "ends_at": ""}
    result.update(teachers=[], subjects=[], classes=[])
    result["rows"] = [
        {**base, "day": "الاثنين", "period_index": "2", "class_name": "أ", "teacher_name": "أحمد", "subject_name": "الرياضيات"},
        {**base, "day": "الأحد", "period_index": "2", "class_name": "أ", "teacher_name": "علي", "subject_name": "الرياضيات"},
        {**base, "day": "الأربعاء", "period_index": "1", "class_name": "ج", "teacher_name": "سعيد", "subject_name": "الرياضيات"},
        {**base, "day": "الخميس", "period_index": "1", "class_name": "د", "teacher_name": "زيد", "subject_name": "الرياضيات"},
    ]
    return result


class ReplacementModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        replacement_fixture(cls)

    def parsed(self, strategy="replace", raw=None):
        draft = prepare_draft(school=self.school, extraction=normalize_extraction(raw or replacement_raw()))
        parsed = parse_draft(school=self.school, draft=draft)
        self.assertFalse(parsed["errors"], parsed["errors"])
        parsed.update(strategy=strategy, requires_document_review=True)
        impact = build_import_impact(school=self.school, parsed=parsed)
        parsed["replacement_fingerprint"] = impact["state_fingerprint"]
        return parsed, impact

    def test_preview_distinguishes_every_change_and_writes_nothing(self):
        before = _import_state_fingerprint(self.school_settings)
        _, impact = self.parsed()
        self.assertEqual({key: impact[key] for key in ("new_lessons", "updated_lessons", "moved_lessons", "deleted_lessons", "unchanged_lessons")},
                         dict(new_lessons=1, updated_lessons=1, moved_lessons=1, deleted_lessons=1, unchanged_lessons=1))
        self.assertEqual(next(item["before"]["id"] for item in impact["changes"] if item["kind"] == "deleted"), self.deleting.pk)
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)
        self.assertFalse(Teacher.objects.filter(school=self.school, name="زيد").exists())

    def test_replace_moves_same_record_deletes_absent_lessons_and_undo_restores_exact_state(self):
        before = _import_state_fingerprint(self.school_settings)
        other_before = _import_state_fingerprint(self.other_settings)
        parsed, _ = self.parsed()
        result = apply_import(school=self.school, parsed=parsed)
        self.assertEqual((result["deleted_lessons"], result["moved_lessons"]), (1, 1))
        self.assertEqual(ClassLesson.objects.filter(settings=self.school_settings).count(), 4)
        self.assertFalse(ClassLesson.objects.filter(pk=self.deleting.pk).exists())
        self.moving.refresh_from_db()
        self.assertEqual((self.moving.weekday, self.moving.period_index), (1, 2))
        self.changing.refresh_from_db()
        self.assertEqual(self.changing.teacher.name, "علي")
        self.assertEqual(Period.objects.filter(day__settings=self.school_settings, index=1).first().starts_at, time(7, 10, 15))
        undo_import(school=self.school, undo=result["undo"])
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)
        self.assertEqual(_import_state_fingerprint(self.other_settings), other_before)

    def test_merge_default_preserves_absent_lessons_and_does_not_classify_addition_as_move(self):
        parsed, impact = self.parsed("merge")
        self.assertEqual((impact["deleted_lessons"], impact["moved_lessons"], impact["new_lessons"]), (0, 0, 2))
        apply_import(school=self.school, parsed=parsed)
        self.assertTrue(ClassLesson.objects.filter(pk=self.deleting.pk).exists())
        self.assertTrue(ClassLesson.objects.filter(pk=self.moving.pk, weekday=7, period_index=1).exists())

    def test_empty_or_names_only_replacement_cannot_delete_anything(self):
        before = _import_state_fingerprint(self.school_settings)
        parsed, _ = self.parsed()
        for value in ({**parsed, "rows": []}, {**parsed, "mode": "entities"}):
            with self.subTest(), self.assertRaises(ValueError):
                apply_import(school=self.school, parsed=value)
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)

    def test_changed_or_missing_review_fingerprint_rejects_before_deletion(self):
        parsed, _ = self.parsed()
        self.unchanged.is_active = False
        self.unchanged.save(update_fields=["is_active"])
        after_edit = _import_state_fingerprint(self.school_settings)
        for value in (parsed, {**parsed, "replacement_fingerprint": ""}):
            with self.subTest(), self.assertRaisesMessage(ValueError, "بعد المعاينة"):
                apply_import(school=self.school, parsed=value)
        self.assertEqual(_import_state_fingerprint(self.school_settings), after_edit)

    def test_another_school_change_does_not_invalidate_own_review(self):
        parsed, _ = self.parsed()
        ClassLesson.objects.filter(pk=self.other_lesson.pk).update(is_active=False)
        apply_import(school=self.school, parsed=parsed)
        self.assertTrue(ClassLesson.objects.filter(pk=self.other_lesson.pk, is_active=False).exists())

    def test_validation_failure_rolls_back_deletions_moves_and_created_names(self):
        before = _import_state_fingerprint(self.school_settings)
        parsed, _ = self.parsed()
        parsed["rows"][-1].update(day=3, teacher_name="سعيد")
        with self.assertRaisesMessage(ValueError, "بأكثر من فصل"):
            apply_import(school=self.school, parsed=parsed)
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)

    def test_undo_after_later_edit_is_rejected_without_restoring_deleted_data(self):
        parsed, _ = self.parsed()
        result = apply_import(school=self.school, parsed=parsed)
        ClassLesson.objects.filter(pk=self.unchanged.pk).update(is_active=False)
        later = _import_state_fingerprint(self.school_settings)
        with self.assertRaisesMessage(ValueError, "تغيرت بيانات"):
            undo_import(school=self.school, undo=result["undo"])
        self.assertEqual(_import_state_fingerprint(self.school_settings), later)
        self.assertFalse(ClassLesson.objects.filter(pk=self.deleting.pk).exists())

    def test_repeated_lessons_are_not_guessed_as_one_to_one_moves(self):
        ClassLesson.objects.filter(settings=self.school_settings).delete()
        for day in (7, 1):
            ClassLesson.objects.create(settings=self.school_settings, school_class=self.classes["أ"], weekday=day, period_index=1, subject=self.math, teacher=self.teachers["أحمد"])
        raw = replacement_raw()
        raw["rows"] = [{**raw["rows"][0], "day": day, "period_index": "1"} for day in ("الثلاثاء", "الأربعاء")]
        parsed, impact = self.parsed(raw=raw)
        self.assertEqual((impact["moved_lessons"], impact["new_lessons"], impact["deleted_lessons"]), (0, 2, 2))
        before = _import_state_fingerprint(self.school_settings)
        result = apply_import(school=self.school, parsed=parsed)
        undo_import(school=self.school, undo=result["undo"])
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)

    def test_reuploading_same_complete_timetable_is_idempotent(self):
        parsed, _ = self.parsed()
        apply_import(school=self.school, parsed=parsed)
        after_first = _import_state_fingerprint(self.school_settings)
        repeated, impact = self.parsed()
        self.assertEqual(impact["changes"], [])
        self.assertEqual(impact["unchanged_lessons"], 4)
        result = apply_import(school=self.school, parsed=repeated)
        self.assertEqual(_import_state_fingerprint(self.school_settings), after_first)
        undo_import(school=self.school, undo=result["undo"])
        self.assertEqual(_import_state_fingerprint(self.school_settings), after_first)


@override_settings(DOCUMENT_IMPORT_ENABLED=True, OPENAI_API_KEY="fake-replacement-key", DOCUMENT_IMPORT_HOURLY_LIMIT=10, DOCUMENT_IMPORT_DAILY_LIMIT=40)
class ReplacementReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        replacement_fixture(cls)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)
        self.url = reverse("dashboard:school_document_import")

    extract = fixtures.DocumentImportFlowTests.extract
    review = fixtures.DocumentImportFlowTests.review

    def preview(self, **options):
        token, draft = self.extract(replacement_raw())
        return token, draft, self.review(token, draft, strategy="replace", **options)

    def save(self, token, **options):
        return self.client.post(reverse("dashboard:school_data_import"), {"action": "import", "import_token": token, "review_confirmed": "yes", **options})

    def test_review_displays_changes_requires_explicit_replacement_and_undo_restores_deletions(self):
        before = _import_state_fingerprint(self.school_settings)
        _, _, preview = self.preview()
        self.assertContains(preview, "مقارنة الحصص قبل الحفظ")
        self.assertContains(preview, "الحصص التي ستُحذف")
        self.assertContains(preview, 'name="replace_confirmed"')
        self.assertEqual(preview.context["impact"]["deleted_lessons"], 1)
        token = preview.context["import_token"]
        self.save(token)
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)
        saved = self.save(token, replace_confirmed="yes")
        self.assertEqual(saved.status_code, 302)
        self.assertFalse(ClassLesson.objects.filter(pk=self.deleting.pk).exists())
        completion = self.client.get(saved.url)
        undo_token = completion.context["completed_import"]["token"]
        self.client.post(reverse("dashboard:school_data_import"), {"action": "undo", "undo_token": undo_token})
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)

    def test_stale_replacement_token_is_invalidated_and_returns_to_review(self):
        _, _, preview = self.preview()
        token = preview.context["import_token"]
        ClassLesson.objects.filter(pk=self.unchanged.pk).update(is_active=False)
        after_edit = _import_state_fingerprint(self.school_settings)
        saved = self.save(token, replace_confirmed="yes")
        self.assertIn("?draft=", saved.url)
        self.assertFalse(cache.get(f"school-data-import:{self.user.pk}:{self.school.pk}:{token}"))
        self.assertEqual(_import_state_fingerprint(self.school_settings), after_edit)

    def test_final_post_cannot_turn_reviewed_merge_into_replacement(self):
        token, draft = self.extract(replacement_raw())
        checked = self.review(token, draft, strategy="merge")
        self.save(checked.context["import_token"], strategy="replace", replace_confirmed="yes")
        self.assertTrue(ClassLesson.objects.filter(pk=self.deleting.pk).exists())

    def test_names_only_and_invalid_data_do_not_get_replacement_approval(self):
        _, _, response = self.preview(mode="entities")
        self.assertFalse(response.context["import_token"])
        self.assertContains(response, "يتطلب اختيار", status_code=400)
        token, draft = self.extract(replacement_raw())
        draft["rows"][0]["teacher_name"] = ""
        invalid = self.review(token, draft, strategy="replace")
        self.assertTrue(invalid.context["parsed"]["errors"])
        self.assertFalse(invalid.context["import_token"])

    def test_revalidation_invalidates_old_replacement_and_changes_strategy(self):
        token, draft, preview = self.preview()
        old = preview.context["import_token"]
        reviewed = self.review(token, draft, strategy="merge")
        self.assertFalse(cache.get(f"school-data-import:{self.user.pk}:{self.school.pk}:{old}"))
        self.assertEqual(reviewed.context["parsed"]["strategy"], "merge")
        self.assertEqual(reviewed.context["impact"]["deleted_lessons"], 0)

    def test_upload_strategy_survives_all_batched_parts_and_never_saves_early(self):
        normalized = normalize_extraction(replacement_raw())
        results = [{**deepcopy(normalized), "rows": normalized["rows"][:2]}, {**deepcopy(normalized), "rows": normalized["rows"][2:]}]
        parts = [{"direct": b"one", "rows": []}, {"direct": b"two", "rows": []}]
        before = _import_state_fingerprint(self.school_settings)
        with patch("dashboard.views_document_import.plan_document", return_value=parts):
            started = self.client.post(self.url, {"action": "extract", "document": fixtures.image_upload(), "mode": "timetable", "strategy": "replace"})
        token = started.url.split("?job=")[1]
        self.assertEqual(cache.get(job_key(self.user.pk, self.school.pk, token))["strategy"], "replace")
        with patch("dashboard.document_jobs.extract_part", side_effect=results):
            progress = self.client.post(self.url, {"action": "advance", "job_token": token}).json()
            self.assertNotEqual(progress["state"], "complete")
            self.assertEqual(_import_state_fingerprint(self.school_settings), before)
            complete = self.client.post(self.url, {"action": "advance", "job_token": token}).json()
        preview = self.client.get(complete["url"])
        self.assertEqual(preview.context["draft"]["strategy"], "replace")
        self.assertFalse(preview.context["import_token"])
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)

    def test_template_excel_supports_the_same_reviewed_replace_and_undo(self):
        from openpyxl import load_workbook
        book = load_workbook(BytesIO(build_template_bytes()))
        for sheet in book:
            if sheet.max_row > 1:
                sheet.delete_rows(2, sheet.max_row)
        book["الجدول"].append(["الأربعاء", 1, "10:10", "10:55", "ج", "الرياضيات", "سعيد"])
        output = BytesIO()
        book.save(output)
        book.close()
        url = reverse("dashboard:school_data_import")
        before = _import_state_fingerprint(self.school_settings)
        preview = self.client.post(url, {"action": "preview", "strategy": "replace", "excel_file": SimpleUploadedFile("full.xlsx", output.getvalue())})
        self.assertFalse(preview.context["parsed"]["errors"])
        self.assertEqual(preview.context["parsed"]["rows"][0]["starts_at"], "07:10")
        self.assertEqual(preview.context["impact"]["deleted_lessons"], 3)
        token = preview.context["import_token"]
        self.save(token)
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)
        saved = self.save(token, replace_confirmed="yes")
        self.assertEqual(ClassLesson.objects.filter(settings=self.school_settings).count(), 1)
        complete = self.client.get(saved.url)
        self.client.post(url, {"action": "undo", "undo_token": complete.context["completed_import"]["token"]})
        self.assertEqual(_import_state_fingerprint(self.school_settings), before)
