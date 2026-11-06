"""Large imports must cover every part and preserve school isolation/timings."""
from copy import deepcopy
from datetime import time
from io import BytesIO
import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image, ImageDraw

from dashboard.document_batches import (
    _grid, _native_timetable, class_code, divide_part, extract_part, merge_extractions, plan_document, add_native_name_checks,
)
from dashboard.excel_import import apply_import, undo_import, DAY_LABELS
from dashboard.document_import import DocumentBatchTooLarge, DocumentImportError, normalize_extraction, prepare_draft, parse_draft
from dashboard.document_jobs import JOB_TTL, job_key, process_next_part, finish_extraction
from dashboard import test_document_import as fixtures
from schedule.models import ClassLesson, DaySchedule, Period, SchoolClass, Teacher

extraction = fixtures.extraction
image_upload = fixtures.image_upload


def png(width=400, height=40):
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, "PNG")
    return output.getvalue()


def native_part():
    return {"page": 2, "header": png(), "native": True,
            "rows": [{"id": "p2-r3", "image": png(), "native": [
                {"day": "الأحد", "period_index": "1", "class_name": "1/1"},
                {"day": "الاثنين", "period_index": "2", "class_name": "1/1"},
            ]}]}


def metadata():
    return {"source_rows": [{"id": "p2-r3", "teacher_name": "أحمد محمد", "subject_name": "الرياضيات", "class_name": "", "printed_workload": "٢", "note": ""}], "warnings": []}


class BatchGeometryTests(SimpleTestCase):
    def test_native_text_disagreement_is_visible_in_affected_rows(self):
        part = native_part()
        part["rows"][0]["printed_teacher"] = "أحمد محمود"
        result = normalize_extraction(extraction())
        result["rows"][0]["source_id"] = "p2-r3"
        result["audit"] = metadata()["source_rows"]
        checked = add_native_name_checks(part, result)
        self.assertIn("نص PDF", checked["warnings"][0])
        self.assertIn("نص PDF", checked["rows"][0]["note"])
        self.assertEqual(checked["rows"][0]["teacher_name"], "أحمد محمد")

    def test_unruled_large_response_can_split_further_automatically(self):
        part = {"page": 1, "direct": png(400, 500), "native": False, "rows": []}
        children = divide_part(part)
        self.assertEqual(len(children), 2)
        self.assertNotEqual(children[0]["rows"][0]["id"], children[1]["rows"][0]["id"])

    def test_native_header_controls_weekdays_and_period_counts(self):
        xs, ys = list(range(0, 1100, 100)), list(range(0, 201, 40))
        cells = {(0, 5): [(550, "الأحد")], (0, 1): [(150, "الاثنين")],
                 (0, 8): [(850, "اسم المعلم")], (0, 9): [(950, "المقرر")]}
        for column in range(8):
            cells[1, column] = [(column*100+20, str(4-column%4))]
        cells[2, 7] = [(710, "١"), (720, "/"), (730, "٢")]
        cells[2, 0] = [(10, "د"), (20, "/"), (30, "٣")]
        result = _native_timetable(cells, xs, ys)
        self.assertEqual(result["header_row"], 1)
        self.assertEqual(result["slots"][7], ("الأحد", 1))
        self.assertEqual(result["slots"][0], ("الاثنين", 4))
        self.assertEqual({row["class_name"] for row in result["bodies"][2]}, {"1/2", "3/د"})
        cells[2, 0] = [(10, "unreadable glyph")]
        self.assertIsNone(_native_timetable(cells, xs, ys))

    def test_class_codes_use_geometry_and_keep_names(self):
        self.assertEqual(class_code("د/٢"), "2/د")
        self.assertEqual(class_code("١ / ٧"), "1/7")
        self.assertEqual(class_code("الأول أ"), "الأول أ")
        self.assertNotEqual(class_code("١/٢"), class_code("٢/١"))

    def test_ruled_image_splits_without_losing_or_repeating_target_rows(self):
        image = Image.new("RGB", (1100, 1000), "white")
        draw = ImageDraw.Draw(image)
        for x in range(50, 1051, 100):
            draw.line((x, 50, x, 950), fill="black", width=2)
        for y in range(50, 951, 45):
            draw.line((50, y, 1050, y), fill="black", width=2)
        for index in range(18):
            draw.text((860, 65+index*45), f"Teacher {index}", fill="black")
        self.assertIsNotNone(_grid(image))
        output = BytesIO()
        image.save(output, "PNG")
        parts = plan_document(data=output.getvalue(), mime_type="image/png")
        ids = [row["id"] for part in parts for row in part["rows"]]
        self.assertGreater(len(parts), 1)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 18)

    @patch("dashboard.document_batches.extract_document", return_value=metadata())
    def test_native_cells_supply_all_lessons_while_model_only_reads_labels(self, provider):
        result = extract_part(native_part(), mode="timetable")
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][1]["day"], "الاثنين")
        self.assertEqual(result["rows"][0]["source_id"], "p2-r3")
        self.assertEqual(result["audit"][0]["observed"], 2)
        self.assertIn("Only transcribe", provider.call_args.kwargs["task"])

    @patch("dashboard.document_batches.extract_document", return_value={"source_rows": [], "warnings": []})
    def test_omitted_target_row_never_counts_as_completed(self, provider):
        with self.assertRaises(DocumentBatchTooLarge):
            extract_part(native_part(), mode="timetable")

    def test_split_and_merge_preserve_more_than_300_lessons(self):
        part = native_part()
        part["rows"] *= 4
        children = divide_part(part)
        self.assertEqual(sum(len(child["rows"]) for child in children), 4)
        results = []
        for offset in (0, 200, 400):
            result = normalize_extraction(extraction())
            result["rows"] = [{**result["rows"][0], "class_name": f"class-{index}"} for index in range(offset, offset+200)]
            results.append(result)
        self.assertEqual(len(merge_extractions(results)["rows"]), 600)
        results.append(results[0])
        merged = merge_extractions(results)
        self.assertEqual(len(merged["rows"]), 600)
        self.assertTrue(any("متطابقة" in value for value in merged["warnings"]))

    @patch("dashboard.document_jobs.extract_part", side_effect=DocumentBatchTooLarge("split"))
    def test_incomplete_batch_adapts_without_advancing_or_losing_rows(self, provider):
        part = native_part()
        part["rows"] *= 4
        job = {"next": 0, "parts": [part], "results": [], "mode": "timetable"}
        self.assertFalse(process_next_part(job))
        self.assertEqual(job["next"], 0)
        self.assertEqual(len(job["parts"]), 2)
        self.assertEqual(job["results"], [])
        with self.assertRaises(DocumentImportError):
            finish_extraction(job)


@override_settings(DOCUMENT_IMPORT_ENABLED=True, OPENAI_API_KEY="test-server-key", DOCUMENT_IMPORT_HOURLY_LIMIT=10, DOCUMENT_IMPORT_DAILY_LIMIT=40)
class DocumentJobFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.DocumentImportFlowTests.setUpTestData.__func__(cls)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)
        self.url = reverse("dashboard:school_document_import")

    def start(self):
        parts = [native_part(), native_part()]
        with patch("dashboard.views_document_import.plan_document", return_value=parts):
            response = self.client.post(self.url, {"action": "extract", "document": image_upload()})
        self.assertEqual(response.status_code, 302)
        return response.url.split("job=", 1)[1]

    def advance(self, token):
        return self.client.post(self.url, {"action": "advance", "job_token": token})

    def test_full_job_requires_all_parts_before_draft_and_discards_images(self):
        token = self.start()
        result = normalize_extraction(extraction())
        with patch("dashboard.document_jobs.extract_part", return_value=result):
            first = self.advance(token).json()
            self.assertEqual((first["completed"], first["total"]), (1, 2))
            self.assertNotIn("url", first)
            last = self.advance(token).json()
        self.assertEqual(last["state"], "complete")
        self.assertNotIn("parts", cache.get(job_key(self.user.pk, self.school.pk, token)))
        preview = self.client.get(last["url"])
        self.assertEqual(preview.context["draft"]["coverage"], {"completed": 2, "total": 2})
        self.assertFalse(Teacher.objects.exists())
        self.assertFalse(ClassLesson.objects.exists())

    def test_failure_retains_completed_results_and_retry_only_reads_remaining_part(self):
        token = self.start()
        result = normalize_extraction(extraction())
        with patch("dashboard.document_jobs.extract_part", side_effect=[result, DocumentImportError("temporary"), result]) as provider:
            self.advance(token)
            paused = self.advance(token).json()
            self.assertEqual((paused["state"], paused["completed"]), ("paused", 1))
            self.assertEqual(self.advance(token).json()["state"], "complete")
            self.assertEqual(provider.call_count, 3)

    def test_job_token_cannot_cross_school_boundary(self):
        token = self.start()
        from core.models import UserProfile
        UserProfile.objects.filter(user=self.user).update(active_school=self.other)
        with patch("dashboard.document_jobs.extract_part") as provider:
            self.assertEqual(self.advance(token).status_code, 400)
            provider.assert_not_called()

    def test_school_timings_take_priority_and_missing_times_remain_errors(self):
        day = DaySchedule.objects.create(settings=self.school_settings, weekday=7, periods_count=1)
        Period.objects.create(day=day, index=1, starts_at=time(8), ends_at=time(8, 45))
        draft = prepare_draft(school=self.school, extraction=normalize_extraction(extraction()))
        self.assertEqual(draft["rows"][0]["starts_at"], "08:00")
        self.assertEqual(draft["rows"][0]["time_source"], "school")
        draft["rows"].append({**draft["rows"][0], "day": "الاثنين", "starts_at": "", "ends_at": ""})
        self.assertTrue(parse_draft(school=self.school, draft=draft)["errors"])

    def test_changed_school_times_revoke_approved_preview_before_save(self):
        day = DaySchedule.objects.create(settings=self.school_settings, weekday=7, periods_count=1)
        period = Period.objects.create(day=day, index=1, starts_at=time(8), ends_at=time(8, 45))
        token = "x"*40
        draft = prepare_draft(school=self.school, extraction=normalize_extraction(extraction()))
        draft.update(file={"name": "test.png"}, mode="timetable")
        cache.set(f"school-document-draft:{self.user.pk}:{self.school.pk}:{token}", draft)
        response = self.client.post(self.url, {"action": "review", "draft_token": token, "draft_rows": json.dumps(draft["rows"]), "mode": "timetable"})
        approved = response.context["import_token"]
        self.assertTrue(approved)
        Period.objects.filter(pk=period.pk).update(starts_at=time(9), ends_at=time(9,45))
        response = self.client.post(reverse("dashboard:school_data_import"), {"action": "import", "import_token": approved, "review_confirmed": "yes"})
        self.assertEqual(response.url, f"{self.url}?draft={token}")
        self.assertFalse(Teacher.objects.exists())
        period.refresh_from_db()
        self.assertEqual(period.starts_at, time(9))

    def test_document_save_preserves_registered_seconds_exactly(self):
        day = DaySchedule.objects.create(settings=self.school_settings, weekday=7, periods_count=1)
        period = Period.objects.create(day=day, index=1, starts_at=time(8,0,30), ends_at=time(8,45,30))
        draft = prepare_draft(school=self.school, extraction=normalize_extraction(extraction()))
        parsed = parse_draft(school=self.school, draft=draft)
        parsed["requires_document_review"] = True
        result = apply_import(school=self.school, parsed=parsed)
        period.refresh_from_db()
        self.assertEqual(period.starts_at, time(8,0,30))
        self.assertEqual(period.ends_at, time(8,45,30))
        undo_import(school=self.school, undo=result["undo"])
        period.refresh_from_db()
        self.assertEqual(period.starts_at, time(8,0,30))

    def test_existing_arabic_class_code_is_reused_for_native_ascii_code(self):
        SchoolClass.objects.create(settings=self.school_settings, name="١/٢")
        result = normalize_extraction(extraction())
        result["rows"][0]["class_name"] = "1/2"
        draft = prepare_draft(school=self.school, extraction=result)
        self.assertEqual(draft["rows"][0]["class_name"], "١/٢")

    def test_workload_discrepancy_is_visible_without_inventing_lessons(self):
        draft = normalize_extraction(extraction())
        draft["rows"][0]["source_id"] = "p1-r3"
        draft["audit"] = [{"id": "p1-r3", "teacher_name": "أحمد محمد", "printed_workload": "٢٠", "observed": 1, "page": 1}]
        parsed = parse_draft(school=self.school, draft=draft)
        self.assertTrue(any("20 مقابل 1" in text for text in parsed["warnings"]))
        self.assertEqual(len(parsed["rows"]), 1)

    def test_640_registered_time_lessons_can_be_saved_and_undone(self):
        from datetime import datetime, timedelta
        slots = []
        for weekday, total in ((7, 7), (1, 7), (2, 6), (3, 6), (4, 6)):
            day = DaySchedule.objects.create(settings=self.school_settings, weekday=weekday, periods_count=total)
            for index in range(1, total+1):
                start = datetime(2026, 11, 6, 7)
                start += timedelta(minutes=45*(index-1))
                Period.objects.create(day=day, index=index, starts_at=start.time(), ends_at=(start+timedelta(minutes=45)).time())
                slots.append((DAY_LABELS[weekday], index))
        sample = normalize_extraction(extraction())["rows"][0]
        raw = {"rows": [{**sample, "day": day, "period_index": str(index), "class_name": f"فصل {number}",
                          "teacher_name": f"معلم {number}", "starts_at": "", "ends_at": ""}
                         for number in range(20) for day, index in slots],
               "entities": {"teachers": [], "subjects": [], "classes": []}, "warnings": []}
        draft = prepare_draft(school=self.school, extraction=raw)
        self.assertEqual(draft["timing_summary"]["from_school"], 640)
        parsed = parse_draft(school=self.school, draft=draft)
        self.assertEqual(parsed["errors"], [])
        result = apply_import(school=self.school, parsed=parsed)
        self.assertEqual(ClassLesson.objects.count(), 640)
        self.assertEqual(Period.objects.count(), 32)
        undo_import(school=self.school, undo=result["undo"])
        self.assertFalse(ClassLesson.objects.exists())
        self.assertFalse(Teacher.objects.exists())
        self.assertEqual(Period.objects.count(), 32)
