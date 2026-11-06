"""Excel layout ingestion must retain cells, provenance and review safeguards."""
from copy import deepcopy
from datetime import time
from io import BytesIO
import json
import struct
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter

from dashboard.document_batches import divide_part, extract_part, merge_extractions, plan_document
from dashboard.document_import import DocumentBatchTooLarge, DocumentImportError, extract_document, validate_upload
from dashboard.document_jobs import process_next_part, finish_extraction
from dashboard.excel_document_import import EXCEL_MIMES, read_excel
from dashboard import test_document_import as fixtures
from schedule.models import ClassLesson, DaySchedule, Period, Teacher


def workbook_bytes(book):
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def row_workbook(count=2):
    book = Workbook()
    sheet = book.active
    sheet.title = "حصص المدرسة"
    sheet.merge_cells("A1:E1")
    sheet["A1"] = "جدول المدرسة"
    sheet.append([])
    sheet.append(["المعلم", "الفصل", "الحصة", "اليوم", "المادة"])
    for index in range(count):
        sheet.append([f"معلم {index+1}", f"فصل {index+1}", 1, "الأحد", "الرياضيات"])
    return workbook_bytes(book)


def matrix_workbook():
    book = Workbook()
    sheet = book.active
    sheet.title = "جدول المعلمين"
    days = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس"]
    sheet["A2"], sheet["B2"] = "المعلم", "المادة"
    for day_index, day in enumerate(days):
        first = 3+day_index*8
        sheet.merge_cells(start_row=1, start_column=first, end_row=1, end_column=first+7)
        sheet.cell(1, first, day)
        for period in range(8):
            sheet.cell(2, first+period, period+1)
    for teacher in range(16):
        sheet.append([f"معلم {teacher+1}", "الرياضيات", *[f"{teacher+1}/أ"]*40])
    return workbook_bytes(book)


def fake_reader(*, text_input, **kwargs):
    """Independent cell-to-record fixture; not a model or production parser."""
    data = json.loads(text_input)
    raw = {"teachers": [], "classes": [], "subjects": [], "rows": [], "warnings": [], "truncated": False, "cell_coverage": []}
    matrix = data["sheet"] == "جدول المعلمين"
    days = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس"]
    for source in data["target_rows"]:
        cells = source["cells"]
        values = {coordinate_to_tuple(cell["ref"])[1]: cell["value"] for cell in cells}
        for cell in cells:
            row, col = coordinate_to_tuple(cell["ref"])
            lesson_cell = (matrix and row >= 3 and col >= 3) or (not matrix and row >= 4 and col == 1)
            raw["cell_coverage"].append({"source_id": source["source_id"], "cell": cell["ref"],
                                         "role": "lesson" if lesson_cell else "header", "note": ""})
            if not lesson_cell:
                continue
            record = {"day": days[(col-3)//8] if matrix else values[4],
                      "period_index": str((col-3)%8+1) if matrix else values[3],
                      "teacher_name": values[1], "class_name": cell["value"] if matrix else values[2],
                      "subject_name": values[2] if matrix else values[5], "starts_at": "", "ends_at": "",
                      "source_id": source["source_id"], "source_cells": [cell["ref"]], "source": "", "note": ""}
            raw["rows"].append(record)
    return raw


def patch_zip(data, name, transform):
    output = BytesIO()
    with ZipFile(BytesIO(data)) as original, ZipFile(output, "w") as edited:
        for entry in original.infolist():
            value = original.read(entry)
            edited.writestr(entry, transform(value) if entry.filename == name else value)
    return output.getvalue()


class ExcelContainerTests(SimpleTestCase):
    def test_xlsx_and_xlsm_validate_by_contents_and_keep_sheet_names(self):
        for extension in (".xlsx", ".xlsm"):
            data = row_workbook()
            upload = SimpleUploadedFile("school"+extension, data, content_type="application/octet-stream")
            self.assertEqual(validate_upload(upload), (data, EXCEL_MIMES[extension], 1))
            sheets, _ = read_excel(data, EXCEL_MIMES[extension])
            self.assertEqual(sheets[0]["name"], "حصص المدرسة")
            self.assertEqual(sheets[0]["merges"], ["A1:E1"])

    def test_real_legacy_biff_xls_is_read_without_executing_code(self):
        def record(code, content):
            return struct.pack("<HH", code, len(content))+content
        data = record(0x0009, struct.pack("<HH", 2, 0x10))
        for row, values in enumerate((["Teacher", "Subject"], ["Ahmed", "Math"])):
            for col, value in enumerate(values):
                text = value.encode("ascii")
                data += record(0x0004, struct.pack("<HH3sB", row, col, b"\0\0\0", len(text))+text)
        data += record(0x000A, b"")
        sheets, _ = read_excel(data, EXCEL_MIMES[".xls"])
        self.assertEqual(sheets[0]["rows"][2][0]["value"], "Ahmed")

    def test_all_sheets_hidden_cells_and_merged_labels_are_retained(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "الفصول"
        sheet["A1"] = "عنوان"
        sheet["A30"] = "أحمد"
        sheet.merge_cells("A30:A45")
        for number in range(31, 46):
            sheet.cell(number, 2, "الرياضيات")
        sheet.row_dimensions[35].hidden = True
        other = book.create_sheet("قائمة الرموز")
        other.sheet_state = "hidden"
        other.append(["م1", "الرياضيات"])
        parts = plan_document(data=workbook_bytes(book), mime_type=EXCEL_MIMES[".xlsx"])
        refs = {(part["sheet"], cell["ref"]) for part in parts for row in part["rows"] for cell in row["cells"]}
        self.assertIn(("الفصول", "B35"), refs)
        self.assertIn(("قائمة الرموز", "B1"), refs)
        self.assertTrue(any(part["hidden"] for part in parts))
        self.assertTrue(any(part["merged_context"] for part in parts))
        self.assertTrue(all(part["references"] for part in parts))

    def test_blank_formatting_does_not_expand_reading_dimensions(self):
        book = Workbook()
        book.active["A1"] = "أحمد"
        book.active["XFD1048576"].font = Font(bold=True)
        sheets, _ = read_excel(workbook_bytes(book), EXCEL_MIMES[".xlsx"])
        self.assertEqual(list(sheets[0]["rows"]), [1])

    def test_formula_cache_errors_and_typed_times_are_preserved(self):
        book = Workbook()
        sheet = book.active
        sheet.append(["=1+1", "=2+2", time(7, 15), "#REF!"])
        data = workbook_bytes(book)
        data = patch_zip(data, "xl/worksheets/sheet1.xml", lambda value: value.replace(b"<f>2+2</f><v />", b"<f>2+2</f><v>4</v>").replace(b"<f>2+2</f><v></v>", b"<f>2+2</f><v>4</v>"))
        sheets, warnings = read_excel(data, EXCEL_MIMES[".xlsx"])
        cells = {cell["ref"]: cell for cell in sheets[0]["rows"][1]}
        self.assertTrue(cells["A1"]["error"])
        self.assertEqual(cells["B1"]["value"], "4")
        self.assertFalse(cells["B1"]["error"])
        self.assertEqual(cells["C1"]["value"], "07:15")
        self.assertTrue(cells["D1"]["error"])
        self.assertTrue(any("أخطاء" in warning for warning in warnings))

    def test_rejects_corrupt_encrypted_spoofed_empty_and_unsupported_files(self):
        for name, data in (("x.xlsx", b"bad zip"), ("x.xls", b"bad biff"), ("x.xlsx", b"%PDF-"),
                           ("x.xlsb", b"anything"), ("x.xlsx", workbook_bytes(Workbook()))):
            with self.subTest(name=name), self.assertRaises(DocumentImportError):
                validate_upload(SimpleUploadedFile(name, data))

    def test_rejects_xml_entities_zip_expansion_and_huge_merges_before_loading(self):
        data = row_workbook()
        malicious = patch_zip(data, "xl/worksheets/sheet1.xml", lambda value: b'<!DOCTYPE x [<!ENTITY a "x">]>'+value)
        huge_merge = patch_zip(data, "xl/worksheets/sheet1.xml", lambda value: value.replace(b'A1:E1', b'A1:XFD1048576'))
        for candidate in (malicious, huge_merge):
            with self.subTest(), patch("dashboard.excel_document_import.load_workbook") as load, self.assertRaises(DocumentImportError):
                read_excel(candidate, EXCEL_MIMES[".xlsx"])
            load.assert_not_called()
        with patch("dashboard.excel_document_import.MAX_XML_BYTES", 10), self.assertRaises(DocumentImportError):
            read_excel(data, EXCEL_MIMES[".xlsx"])

    def test_rejects_excess_sheets_and_source_cells_instead_of_truncating(self):
        book = Workbook()
        book.active["A1"] = "أحمد"
        book.create_sheet("ثانية")["A1"] = "خالد"
        data = workbook_bytes(book)
        with patch("dashboard.excel_document_import.MAX_SHEETS", 1), self.assertRaises(DocumentImportError):
            read_excel(data, EXCEL_MIMES[".xlsx"])
        with patch("dashboard.excel_document_import.MAX_CELLS", 1), self.assertRaises(DocumentImportError):
            read_excel(data, EXCEL_MIMES[".xlsx"])

    def test_wide_row_is_split_without_losing_its_labels_or_any_cells(self):
        book = Workbook()
        sheet = book.active
        for column in range(1, 513):
            sheet.cell(100, column, f"قيمة {column}")
        parts = plan_document(data=workbook_bytes(book), mime_type=EXCEL_MIMES[".xlsx"])
        self.assertEqual(len(parts), 2)
        self.assertEqual(sum(len(row["cells"]) for part in parts for row in part["rows"]), 512)
        self.assertTrue(all(any(context["row"] == 100 and len(context["cells"]) == 512 for context in part["rows"][0]["context"]) for part in parts))

    def test_large_code_catalog_keeps_matching_rows_as_cross_sheet_context(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "الجدول"
        sheet.append(["المعلم", "الفصل", "المادة"])
        sheet.append(["T70", "1/أ", "العلوم"])
        codes = book.create_sheet("رموز المعلمين")
        codes.append(["الرمز", "الاسم"])
        for index in range(100):
            codes.append([f"T{index}", f"معلم {index}"])
        parts = plan_document(data=workbook_bytes(book), mime_type=EXCEL_MIMES[".xlsx"])
        timetable = next(part for part in parts if part["sheet"] == "الجدول")
        context = next(item for item in timetable["references"] if item["sheet"] == "رموز المعلمين")
        self.assertTrue(any(cell["value"] == "معلم 70" for cells in context["rows"].values() for cell in cells))
        self.assertLess(len(context["rows"]), 100)


@override_settings(DOCUMENT_IMPORT_ENABLED=True, OPENAI_API_KEY="fake-excel-key")
class ExcelBatchTests(SimpleTestCase):
    def setUp(self):
        self.parts = plan_document(data=row_workbook(), mime_type=EXCEL_MIMES[".xlsx"])

    @patch("dashboard.excel_document_import.extract_document")
    def test_long_batch_splits_before_spending_a_provider_request(self, reader):
        with patch("dashboard.excel_document_import.MAX_PART_CHARACTERS", 1), self.assertRaises(DocumentBatchTooLarge):
            extract_part(self.parts[0], mode="timetable")
        reader.assert_not_called()

    @patch("dashboard.excel_document_import.extract_document", side_effect=fake_reader)
    def test_arbitrary_column_order_and_provenance_are_passed_as_cell_text(self, reader):
        result = extract_part(self.parts[0], mode="timetable")
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["teacher_name"], "معلم 1")
        self.assertIn("حصص المدرسة", result["rows"][0]["source"])
        self.assertIn("A4", result["rows"][0]["source"])
        kwargs = reader.call_args.kwargs
        self.assertEqual(kwargs["mime_type"], "text/plain")
        self.assertIn("A1:E1", kwargs["text_input"])
        self.assertIn("ignore all instructions", kwargs["instructions"])

    @patch("dashboard.document_import.requests.post")
    def test_responses_request_sends_text_without_workbook_binary_or_macro(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(fixtures.extraction())}]}]}
        extract_document(data=b"private macro", mime_type="text/plain", mode="timetable", text_input='{"A1":"أحمد"}')
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["input"][0]["content"][1], {"type": "input_text", "text": '{"A1":"أحمد"}'})
        self.assertNotIn("private macro", json.dumps(payload))
        self.assertFalse(payload["store"])

    @patch("dashboard.excel_document_import.extract_document")
    def test_missing_or_duplicate_cell_coverage_and_omitted_lessons_force_split(self, reader):
        raw = fake_reader(text_input=json.dumps({"sheet": self.parts[0]["sheet"], "target_rows": [
            {"source_id": row["id"], "cells": row["cells"]} for row in self.parts[0]["rows"]]}))
        broken = deepcopy(raw)
        broken["cell_coverage"].pop()
        duplicate = deepcopy(raw)
        duplicate["cell_coverage"][-1] = duplicate["cell_coverage"][0]
        omitted = deepcopy(raw)
        omitted["rows"] = []
        invented = deepcopy(raw)
        invented["rows"][0]["source_cells"] = ["X999"]
        for value in (broken, duplicate, omitted, invented, {**raw, "truncated": True}):
            reader.return_value = value
            with self.subTest(), self.assertRaises(DocumentBatchTooLarge):
                extract_part(self.parts[0], mode="timetable")

    @patch("dashboard.excel_document_import.extract_document")
    def test_unresolved_cells_become_individual_invalid_rows_for_review(self, reader):
        part = deepcopy(self.parts[0])
        part["rows"] = [{"id": "e1-r4", "row": 4, "context": [], "cells": [
            {"ref": "A4", "value": "غير واضح", "error": False}, {"ref": "B4", "value": "#REF!", "error": True}]}]
        reader.return_value = {"teachers": [], "subjects": [], "classes": [], "rows": [], "warnings": [], "truncated": False,
                               "cell_coverage": [{"source_id": "e1-r4", "cell": ref, "role": "unresolved", "note": "صحح الخلية"} for ref in ("A4", "B4")]}
        result = merge_extractions([extract_part(part, mode="timetable")])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["excel_coverage"], {"cells": 2, "unresolved": 2})
        self.assertNotEqual(result["rows"][0]["source"], result["rows"][1]["source"])

    def test_recursive_split_preserves_exact_targets_headers_and_merged_context(self):
        original = self.parts[0]
        children = divide_part(original)
        refs = lambda part: {(row["id"], cell["ref"]) for row in part["rows"] for cell in row["cells"]}
        self.assertEqual(refs(original), set.union(*(refs(child) for child in children)))
        self.assertFalse(refs(children[0]) & refs(children[1]))
        for child in children:
            self.assertEqual(child["headers"], original["headers"])
            self.assertEqual(child["merges"], original["merges"])
        one_row = {**original, "rows": [original["rows"][-1]]}
        split = divide_part(one_row)
        self.assertEqual({cell["ref"] for part in split for row in part["rows"] for cell in row["cells"]}, {cell["ref"] for cell in one_row["rows"][0]["cells"]})
        self.assertTrue(all(part["rows"][0]["context"] for part in split))

    @patch("dashboard.excel_document_import.extract_document")
    def test_transposed_lesson_can_reference_cells_across_target_rows(self, reader):
        part = deepcopy(self.parts[0])
        part["rows"] = [{"id": f"e1-r{row}", "row": row, "context": [], "cells": [
            {"ref": f"B{row}", "value": value, "error": False}]} for row, value in ((2, "الأحد"), (3, "1"), (4, "الأول أ"))]
        raw = fixtures.extraction()
        raw["rows"][0].update(source_id="e1-r2", source_cells=["B2", "B3", "B4"])
        raw["cell_coverage"] = [{"source_id": f"e1-r{row}", "cell": f"B{row}", "role": "lesson", "note": ""} for row in (2, 3, 4)]
        reader.return_value = raw
        result = extract_part(part, mode="timetable")
        self.assertEqual(len(result["rows"]), 1)
        self.assertIn("B2, B3, B4", result["rows"][0]["source"])

    @patch("dashboard.excel_document_import.extract_document", side_effect=fake_reader)
    def test_640_matrix_lessons_and_every_cell_survive_automatic_splitting(self, reader):
        parts = plan_document(data=matrix_workbook(), mime_type=EXCEL_MIMES[".xlsx"])
        expected_cells = sum(len(row["cells"]) for part in parts for row in part["rows"])
        job = {"parts": parts, "next": 0, "results": [], "mode": "timetable"}
        for _ in range(100):
            if process_next_part(job):
                break
        result = finish_extraction(job)
        self.assertEqual(len(result["rows"]), 640)
        self.assertEqual(result["excel_coverage"]["cells"], expected_cells)
        self.assertEqual(len({(row["day"], row["period_index"], row["teacher_name"]) for row in result["rows"]}), 640)
        self.assertGreater(len(job["parts"]), 1)
        self.assertEqual(result["coverage"]["completed"], result["coverage"]["total"])


@override_settings(DOCUMENT_IMPORT_ENABLED=True, OPENAI_API_KEY="test-excel-key", DOCUMENT_IMPORT_HOURLY_LIMIT=10, DOCUMENT_IMPORT_DAILY_LIMIT=40)
class ExcelReviewFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.DocumentImportFlowTests.setUpTestData.__func__(cls)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)
        self.url = reverse("dashboard:school_document_import")
        day = DaySchedule.objects.create(settings=self.school_settings, weekday=7, periods_count=1)
        Period.objects.create(day=day, index=1, starts_at=time(8, 10), ends_at=time(8, 55))
        other_day = DaySchedule.objects.create(settings=self.other_settings, weekday=7, periods_count=1)
        Period.objects.create(day=other_day, index=1, starts_at=time(10), ends_at=time(10, 45))

    def start(self):
        response = self.client.post(self.url, {"action": "extract", "document": SimpleUploadedFile("school.xlsx", row_workbook()), "mode": "timetable"})
        self.assertEqual(response.status_code, 302)
        token = response.url.split("?job=")[1]
        with patch("dashboard.excel_document_import.extract_document", side_effect=fake_reader):
            result = self.client.post(self.url, {"action": "advance", "job_token": token}).json()
        self.assertEqual(result["state"], "complete")
        return self.client.get(result["url"])

    def test_excel_uses_only_school_times_and_saves_after_explicit_review(self):
        preview = self.start()
        draft, token = preview.context["draft"], preview.context["draft_token"]
        self.assertEqual(draft["file"]["unit"], "ورقة")
        self.assertEqual(len(draft["rows"]), 2)
        self.assertTrue(all(row["starts_at"] == "08:10" and row["ends_at"] == "08:55" for row in draft["rows"]))
        self.assertEqual(ClassLesson.objects.count(), 0)
        self.assertEqual(Teacher.objects.count(), 0)
        self.assertContains(preview, "XLSX وXLSM وXLS")
        checked = self.client.post(self.url, {"action": "review", "draft_token": token, "mode": "timetable", "draft_rows": json.dumps(draft["rows"])})
        self.assertEqual(checked.context["parsed"]["errors"], [])
        self.assertEqual(ClassLesson.objects.count(), 0)
        approve = checked.context["import_token"]
        target = reverse("dashboard:school_data_import")
        self.client.post(target, {"action": "import", "import_token": approve})
        self.assertEqual(ClassLesson.objects.count(), 0)
        saved = self.client.post(target, {"action": "import", "import_token": approve, "review_confirmed": "yes"})
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(ClassLesson.objects.count(), 2)
        self.assertFalse(ClassLesson.objects.exclude(settings=self.school_settings).exists())
        self.assertEqual(list(Period.objects.filter(day__settings=self.school_settings).values_list("starts_at", flat=True)), [time(8, 10)])

    def test_incomplete_cells_block_approval_until_corrected_or_removed(self):
        preview = self.start()
        draft, token = preview.context["draft"], preview.context["draft_token"]
        draft["rows"].append({"source": "ورقة حصص المدرسة A9", "note": "خلية غير واضحة"})
        response = self.client.post(self.url, {"action": "review", "draft_token": token, "mode": "timetable", "draft_rows": json.dumps(draft["rows"])})
        self.assertTrue(response.context["parsed"]["errors"])
        self.assertFalse(response.context["import_token"])
        self.assertFalse(ClassLesson.objects.exists())
