from __future__ import annotations

from datetime import datetime, time
from io import BytesIO
import hashlib
import json

from django.db import transaction
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from schedule.models import (
    Break,
    ClassLesson,
    DaySchedule,
    Period,
    SchoolClass,
    SchoolSettings,
    Subject,
    Teacher,
)


DAY_NAMES = {
    "الاثنين": 1,
    "الثلاثاء": 2,
    "الأربعاء": 3,
    "الخميس": 4,
    "الجمعة": 5,
    "السبت": 6,
    "الأحد": 7,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
}
DAY_LABELS = {value: key for key, value in DAY_NAMES.items() if not key.isdigit()}
MAX_IMPORT_ROWS = 5000
EXPECTED_HEADERS = {
    "المعلمون": ["اسم المعلم"],
    "الفصول": ["اسم الفصل"],
    "المواد": ["اسم المادة"],
    "الجدول": [
        "اليوم",
        "رقم الحصة",
        "وقت البداية",
        "وقت النهاية",
        "الفصل",
        "المادة",
        "المعلم",
    ],
}
TIMETABLE_COLUMNS = {
    "day": "اليوم",
    "period_index": "رقم الحصة",
    "starts_at": "وقت البداية",
    "ends_at": "وقت النهاية",
    "class_name": "الفصل",
    "subject_name": "المادة",
    "teacher_name": "المعلم",
}


def _style_sheet(ws):
    ws.sheet_view.rightToLeft = True
    fill = PatternFill("solid", fgColor="1D4ED8")
    for cell in ws[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"


def build_template_bytes() -> bytes:
    wb = Workbook()
    teachers = wb.active
    teachers.title = "المعلمون"
    teachers.append(["اسم المعلم"])
    teachers.append(["أحمد محمد"])
    classes = wb.create_sheet("الفصول")
    classes.append(["اسم الفصل"])
    classes.append(["الأول أ"])
    subjects = wb.create_sheet("المواد")
    subjects.append(["اسم المادة"])
    subjects.append(["الرياضيات"])
    timetable = wb.create_sheet("الجدول")
    timetable.append(
        ["اليوم", "رقم الحصة", "وقت البداية", "وقت النهاية", "الفصل", "المادة", "المعلم"]
    )
    timetable.append(["الأحد", 1, "07:00", "07:45", "الأول أ", "الرياضيات", "أحمد محمد"])
    instructions = wb.create_sheet("تعليمات")
    instructions.append(["تعليمات الاستيراد"])
    instructions.append(["لا تغيّر أسماء الأوراق أو عناوين الأعمدة."])
    instructions.append(["صيغة الوقت المقبولة 07:00 أو 07:00:00."])
    instructions.append(["يمكن تكرار اليوم ورقم الحصة لفصول مختلفة بشرط تطابق الوقت."])
    instructions.append(["راجع الأخطاء في شاشة المعاينة قبل تنفيذ الاستيراد."])
    for ws in wb.worksheets:
        _style_sheet(ws)
        for column in ws.columns:
            width = min(max(len(str(cell.value or "")) for cell in column) + 4, 42)
            ws.column_dimensions[column[0].column_letter].width = width
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def _text(value) -> str:
    return str(value or "").strip()


def _normalized_header(value) -> str:
    return " ".join(_text(value).split())


def _issue(*, sheet: str, row: int | None, column: str, message: str, value="") -> dict:
    return {
        "sheet": sheet,
        "row": row,
        "column": column,
        "message": message,
        "value": _text(value),
    }


def _issue_text(issue: dict) -> str:
    location = f"ورقة «{issue['sheet']}»"
    if issue.get("row"):
        location += f"، الصف {issue['row']}"
    if issue.get("column"):
        location += f"، عمود «{issue['column']}»"
    return f"{location}: {issue['message']}"


def _validate_headers(wb) -> list[dict]:
    issues = []
    for sheet_name, expected in EXPECTED_HEADERS.items():
        if sheet_name not in wb.sheetnames:
            if sheet_name == "الجدول":
                issues.append(
                    _issue(
                        sheet=sheet_name,
                        row=1,
                        column="",
                        message="الورقة غير موجودة. استخدم القالب المعتمد.",
                    )
                )
            continue
        actual = [
            _normalized_header(cell.value)
            for cell in list(wb[sheet_name].iter_rows(min_row=1, max_row=1))[0][: len(expected)]
        ]
        for index, expected_name in enumerate(expected):
            actual_name = actual[index] if index < len(actual) else ""
            if actual_name != expected_name:
                issues.append(
                    _issue(
                        sheet=sheet_name,
                        row=1,
                        column=expected_name,
                        message=f"العنوان المتوقع «{expected_name}» وليس «{actual_name or 'فارغ'}».",
                        value=actual_name,
                    )
                )
    return issues


def _parse_time(value) -> time | None:
    if isinstance(value, datetime):
        return value.time().replace(second=0, microsecond=0)
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    raw = _text(value)
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def _names_from_sheet(wb, sheet_name: str) -> tuple[list[str], bool]:
    if sheet_name not in wb.sheetnames:
        return [], False
    result, seen = [], set()
    exceeded = False
    for row_number, row in enumerate(
        wb[sheet_name].iter_rows(min_row=2, values_only=True),
        start=2,
    ):
        if row_number > MAX_IMPORT_ROWS + 1:
            exceeded = True
            break
        name = _text(row[0] if row else "")
        if name and name.casefold() not in seen:
            result.append(name)
            seen.add(name.casefold())
    return result, exceeded


def parse_workbook(uploaded_file, *, allow_entities_only: bool = False, keep_empty_rows: bool = False) -> dict:
    try:
        wb = load_workbook(uploaded_file, read_only=True, data_only=True)
    except Exception as exc:
        issue = _issue(
            sheet="الملف",
            row=None,
            column="",
            message=f"تعذر قراءة ملف Excel: {exc}",
        )
        return {
            "rows": [],
            "errors": [_issue_text(issue)],
            "issues": [issue],
            "warnings": [],
            "entities": {},
            "summary": {},
        }

    issues = _validate_headers(wb)
    if "الجدول" not in wb.sheetnames:
        return {
            "rows": [],
            "errors": [_issue_text(issue) for issue in issues],
            "issues": issues,
            "warnings": [],
            "entities": {},
            "summary": {},
        }

    teachers, teachers_exceeded = _names_from_sheet(wb, "المعلمون")
    classes, classes_exceeded = _names_from_sheet(wb, "الفصول")
    subjects, subjects_exceeded = _names_from_sheet(wb, "المواد")
    entities = {"teachers": teachers, "classes": classes, "subjects": subjects}
    rows, seen_slots, slot_times, teacher_slots = [], set(), {}, {}
    for sheet_name, names, model in (("المعلمون", teachers, Teacher), ("الفصول", classes, SchoolClass), ("المواد", subjects, Subject)):
        maximum = model._meta.get_field("name").max_length
        for name in names:
            if len(name) > maximum:
                issues.append(_issue(sheet=sheet_name, row=None, column="الاسم",
                    message=f"الاسم يتجاوز الحد الأقصى ({maximum} حرفًا).", value=name))
    for label, exceeded in (
        ("المعلمون", teachers_exceeded),
        ("الفصول", classes_exceeded),
        ("المواد", subjects_exceeded),
    ):
        if exceeded:
            issues.append(
                _issue(
                    sheet=label,
                    row=MAX_IMPORT_ROWS + 2,
                    column="",
                    message=f"تجاوزت الورقة الحد الأقصى ({MAX_IMPORT_ROWS} صف).",
                )
            )
    for row_number, values in enumerate(wb["الجدول"].iter_rows(min_row=2, values_only=True), start=2):
        if row_number > MAX_IMPORT_ROWS + 1:
            issues.append(
                _issue(
                    sheet="الجدول",
                    row=row_number,
                    column="",
                    message=f"تجاوز الملف الحد الأقصى ({MAX_IMPORT_ROWS} صف).",
                )
            )
            break
        padded = tuple(values or ()) + (None,) * max(0, 7 - len(tuple(values or ())))
        day_raw, index_raw, start_raw, end_raw, class_raw, subject_raw, teacher_raw = padded[:7]
        if not keep_empty_rows and not any(_text(value) for value in padded[:7]):
            continue
        row_issues = []
        day = DAY_NAMES.get(_text(day_raw))
        try:
            period_index = int(index_raw)
            if isinstance(index_raw, float) and index_raw != period_index:
                period_index = 0
        except (TypeError, ValueError):
            period_index = 0
        starts_at, ends_at = _parse_time(start_raw), _parse_time(end_raw)
        class_name, subject_name, teacher_name = _text(class_raw), _text(subject_raw), _text(teacher_raw)
        if day is None:
            row_issues.append(("day", "اسم اليوم غير صحيح", day_raw))
        if period_index < 1 or period_index > 20:
            row_issues.append(("period_index", "رقم الحصة يجب أن يكون بين 1 و20", index_raw))
        if starts_at is None:
            row_issues.append(("starts_at", "وقت البداية غير صحيح", start_raw))
        if ends_at is None:
            row_issues.append(("ends_at", "وقت النهاية غير صحيح", end_raw))
        if starts_at and ends_at and starts_at >= ends_at:
            row_issues.append(("ends_at", "وقت النهاية يجب أن يكون بعد البداية", end_raw))
        if not class_name:
            row_issues.append(("class_name", "الفصل مطلوب", class_raw))
        if not subject_name:
            row_issues.append(("subject_name", "المادة مطلوبة", subject_raw))
        if not teacher_name:
            row_issues.append(("teacher_name", "المعلم مطلوب", teacher_raw))
        for field, name, model in (("class_name", class_name, SchoolClass), ("subject_name", subject_name, Subject), ("teacher_name", teacher_name, Teacher)):
            maximum = model._meta.get_field("name").max_length
            if len(name) > maximum:
                row_issues.append((field, f"الاسم يتجاوز الحد الأقصى ({maximum} حرفًا).", name))
        if day and period_index and starts_at and ends_at:
            slot = (day, period_index)
            previous = slot_times.get(slot)
            if previous and previous != (starts_at, ends_at):
                row_issues.append(
                    ("starts_at", "وقت الحصة لا يطابق الصفوف الأخرى لنفس اليوم ورقم الحصة", start_raw)
                )
            else:
                slot_times[slot] = (starts_at, ends_at)
            lesson_slot = (day, period_index, class_name.casefold())
            if lesson_slot in seen_slots:
                row_issues.append(("class_name", "الحصة مكررة لنفس الفصل", class_raw))
            seen_slots.add(lesson_slot)
            if teacher_name:
                teacher_slot = (day, period_index, teacher_name.casefold())
                previous_teacher = teacher_slots.get(teacher_slot)
                if previous_teacher and previous_teacher[0] != class_name.casefold():
                    row_issues.append(
                        (
                            "teacher_name",
                            f"المعلم مرتبط في الوقت نفسه بالفصل «{previous_teacher[1]}» في الصف {previous_teacher[2]}",
                            teacher_raw,
                        )
                    )
                else:
                    teacher_slots[teacher_slot] = (class_name.casefold(), class_name, row_number)

        normalized_issues = [
            _issue(
                sheet="الجدول",
                row=row_number,
                column=TIMETABLE_COLUMNS[field],
                message=message,
                value=value,
            )
            for field, message, value in row_issues
        ]
        issues.extend(normalized_issues)
        normalized = {
            "row": row_number,
            "day": day,
            "day_label": DAY_LABELS.get(day, _text(day_raw)),
            "period_index": period_index,
            "starts_at": starts_at.strftime("%H:%M") if starts_at else "",
            "ends_at": ends_at.strftime("%H:%M") if ends_at else "",
            "class_name": class_name,
            "subject_name": subject_name,
            "teacher_name": teacher_name,
            "errors": [issue["message"] for issue in normalized_issues],
            "issues": normalized_issues,
        }
        rows.append(normalized)
    for day in {slot[0] for slot in slot_times}:
        ordered = sorted((times[0], times[1], slot[1]) for slot, times in slot_times.items() if slot[0] == day)
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] < previous[1]:
                for row in rows:
                    if row["day"] == day and row["period_index"] == current[2]:
                        issue = _issue(sheet="الجدول", row=row["row"], column=TIMETABLE_COLUMNS["starts_at"],
                            message=f"يتداخل وقت الحصة مع الحصة رقم {previous[2]} في اليوم نفسه.")
                        issues.append(issue)
                        row["issues"].append(issue)
                        row["errors"].append(issue["message"])
    if not rows and not (allow_entities_only and any(entities.values())):
        issues.append(
            _issue(
                sheet="الجدول",
                row=None,
                column="",
                message="ورقة الجدول لا تحتوي أي حصص.",
            )
        )
    errors = [_issue_text(issue) for issue in issues]
    return {
        "rows": rows,
        "errors": errors,
        "issues": issues,
        "warnings": [],
        "entities": entities,
        "summary": {
            "rows": len(rows),
            "valid_rows": sum(1 for row in rows if not row["errors"]),
            "invalid_rows": sum(1 for row in rows if row["errors"]),
            "teachers": len({row["teacher_name"].casefold() for row in rows if row["teacher_name"]}),
            "subjects": len({row["subject_name"].casefold() for row in rows if row["subject_name"]}),
            "classes": len({row["class_name"].casefold() for row in rows if row["class_name"]}),
        },
    }


@transaction.atomic
def build_import_impact(*, school, parsed: dict) -> dict:
    """Describe what a valid import will create or update without writing data."""
    from .import_changes import lesson_changes
    settings_obj = SchoolSettings.objects.select_for_update().filter(school=school).first()
    if parsed.get("strategy") == "replace" and settings_obj:
        list(ClassLesson.objects.select_for_update().filter(settings=settings_obj).values_list("pk", flat=True))
    teacher_names = {row["teacher_name"] for row in parsed.get("rows", []) if row["teacher_name"]}
    subject_names = {row["subject_name"] for row in parsed.get("rows", []) if row["subject_name"]}
    class_names = {row["class_name"] for row in parsed.get("rows", []) if row["class_name"]}
    for name in (parsed.get("entities") or {}).get("teachers", []):
        teacher_names.add(name)
    for name in (parsed.get("entities") or {}).get("subjects", []):
        subject_names.add(name)
    for name in (parsed.get("entities") or {}).get("classes", []):
        class_names.add(name)

    existing_teachers = {
        name.casefold() for name in Teacher.objects.filter(school=school).values_list("name", flat=True)
    }
    existing_subjects = {
        name.casefold() for name in Subject.objects.filter(school=school).values_list("name", flat=True)
    }
    existing_classes = set()
    if settings_obj is not None:
        existing_classes = {
            name.casefold()
            for name in SchoolClass.objects.filter(settings=settings_obj).values_list("name", flat=True)
        }
    result = {
        "new_teachers": sum(1 for name in teacher_names if name.casefold() not in existing_teachers),
        "existing_teachers": sum(1 for name in teacher_names if name.casefold() in existing_teachers),
        "new_subjects": sum(1 for name in subject_names if name.casefold() not in existing_subjects),
        "existing_subjects": sum(1 for name in subject_names if name.casefold() in existing_subjects),
        "new_classes": sum(1 for name in class_names if name.casefold() not in existing_classes),
        "existing_classes": sum(1 for name in class_names if name.casefold() in existing_classes),
    }
    result.update(lesson_changes(settings_obj=settings_obj, parsed=parsed))
    result["state_fingerprint"] = _import_state_fingerprint(settings_obj) if settings_obj else "no-settings"
    return result


def _get_or_create_named(model, *, name: str, **scope):
    if not name or len(name) > model._meta.get_field("name").max_length:
        raise ValueError("أحد أسماء الفصول أو المواد أو المعلمين غير صالح أو يتجاوز الحد المسموح.")
    existing = model.objects.filter(**scope, name__iexact=name).first()
    if existing:
        return existing, False
    return model.objects.create(**scope, name=name), True


def _import_state_fingerprint(settings_obj) -> str:
    """Protect undo from erasing later edits, including cascading day deletions."""
    scope = {"settings": settings_obj}
    data = {
        "days": list(DaySchedule.objects.filter(**scope).order_by("pk").values_list("pk", "weekday", "is_active", "periods_count")),
        "periods": list(Period.objects.filter(day__settings=settings_obj).order_by("pk").values_list("pk", "day_id", "index", "starts_at", "ends_at", "school_class_id", "subject_id", "teacher_id")),
        "breaks": list(Break.objects.filter(day__settings=settings_obj).order_by("pk").values_list("pk", "day_id", "label", "starts_at", "duration_min")),
        "lessons": list(ClassLesson.objects.filter(**scope).order_by("pk").values_list("pk", "school_class_id", "weekday", "period_index", "subject_id", "teacher_id", "is_active")),
        "classes": list(SchoolClass.objects.filter(**scope).order_by("pk").values_list("pk", "name")),
        "teachers": list(Teacher.objects.filter(school_id=settings_obj.school_id).order_by("pk").values_list("pk", "name")),
        "subjects": list(Subject.objects.filter(school_id=settings_obj.school_id).order_by("pk").values_list("pk", "name")),
    }
    return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode("utf-8")).hexdigest()


def _validate_imported_timetable(settings_obj, weekdays):
    for weekday in weekdays:
        periods = Period.objects.filter(day__settings=settings_obj, day__weekday=weekday)
        breaks = Break.objects.filter(day__settings=settings_obj, day__weekday=weekday)
        intervals = [(p.starts_at, p.ends_at) for p in periods] + [(b.starts_at, b.ends_at) for b in breaks]
        intervals.sort()
        if any(start >= end for start, end in intervals) or any(current[0] < previous[1] for previous, current in zip(intervals, intervals[1:])):
            raise ValueError(f"يتداخل الجدول المستورد مع حصص أو فسح يوم {DAY_LABELS.get(weekday, weekday)}. راجع الأوقات ثم أعد المعاينة.")
        seen = set()
        for teacher_id, period_index in ClassLesson.objects.filter(settings=settings_obj, weekday=weekday, is_active=True).values_list("teacher_id", "period_index"):
            slot = (teacher_id, period_index)
            if slot in seen:
                raise ValueError(f"يوجد معلم مرتبط بأكثر من فصل في الحصة نفسها يوم {DAY_LABELS.get(weekday, weekday)}.")
            seen.add(slot)


@transaction.atomic
def apply_import(*, school, parsed: dict) -> dict:
    if parsed.get("errors"):
        raise ValueError("لا يمكن استيراد ملف يحتوي أخطاء.")
    replacing = parsed.get("strategy") == "replace"
    if replacing and (not parsed.get("rows") or parsed.get("mode") == "entities"):
        raise ValueError("استبدال الجدول يتطلب ملف حصص كاملًا وسليمًا؛ لا يمكن استخدام قائمة أسماء أو جدول فارغ.")
    settings_obj, settings_created = SchoolSettings.objects.select_for_update().get_or_create(school=school, defaults={"name": school.name})
    moves = {}
    if replacing:
        from .import_changes import lesson_slot, lesson_changes
        list(ClassLesson.objects.select_for_update().filter(settings=settings_obj).values_list("pk", flat=True))
        current_state = "no-settings" if settings_created else _import_state_fingerprint(settings_obj)
        if not parsed.get("replacement_fingerprint") or parsed["replacement_fingerprint"] != current_state:
            raise ValueError("تغير الجدول أو بيانات المدرسة بعد المعاينة. أعد التحقق ومراجعة التغييرات والحذف قبل الاستبدال.")
        changes = lesson_changes(settings_obj=settings_obj, parsed=parsed)["changes"]
        moves = {lesson_slot(item["after"]): item["before"]["id"] for item in changes if item["kind"] == "moved"}
    entities = parsed.get("entities") or {}
    undo = {
        "created_teachers": [],
        "created_subjects": [],
        "created_classes": [],
        "created_days": [],
        "updated_days": [],
        "created_periods": [],
        "updated_periods": [],
        "created_lessons": [],
        "updated_lessons": [],
        "moved_lessons": [],
        "deleted_lessons": [],
    }
    snapshot_fields = ("id", "school_class_id", "weekday", "period_index", "subject_id", "teacher_id", "is_active")
    if replacing:
        deleted_ids = [item["before"]["id"] for item in changes if item["kind"] == "deleted"]
        removed = ClassLesson.objects.filter(settings=settings_obj, pk__in=deleted_ids)
        undo["deleted_lessons"] = list(removed.values(*snapshot_fields))
        removed.delete()

    def ensure_teacher(name):
        obj, created = _get_or_create_named(Teacher, school=school, name=name)
        if created:
            undo["created_teachers"].append(obj.pk)
        return obj

    def ensure_subject(name):
        obj, created = _get_or_create_named(Subject, school=school, name=name)
        if created:
            undo["created_subjects"].append(obj.pk)
        return obj

    def ensure_class(name):
        obj, created = _get_or_create_named(SchoolClass, settings=settings_obj, name=name)
        if created:
            undo["created_classes"].append(obj.pk)
        return obj

    for name in entities.get("teachers", []):
        ensure_teacher(name)
    for name in entities.get("subjects", []):
        ensure_subject(name)
    for name in entities.get("classes", []):
        ensure_class(name)

    period_slots, imported_lessons = {}, 0
    for row in parsed["rows"]:
        teacher = ensure_teacher(row["teacher_name"])
        subject = ensure_subject(row["subject_name"])
        school_class = ensure_class(row["class_name"])
        day, day_created = DaySchedule.objects.get_or_create(
            settings=settings_obj,
            weekday=row["day"],
            defaults={"is_active": True, "periods_count": row["period_index"]},
        )
        if day_created:
            undo["created_days"].append(day.pk)
        if not day.is_active or day.periods_count < row["period_index"]:
            if not day_created and not any(item["id"] == day.pk for item in undo["updated_days"]):
                undo["updated_days"].append(
                    {"id": day.pk, "is_active": day.is_active, "periods_count": day.periods_count}
                )
            day.is_active = True
            day.periods_count = max(day.periods_count, row["period_index"])
            day.save(update_fields=("is_active", "periods_count"))
        slot = (row["day"], row["period_index"])
        if slot not in period_slots:
            period, period_created = Period.objects.get_or_create(
                day=day,
                index=row["period_index"],
                defaults={
                    "starts_at": datetime.strptime(row["starts_at"], "%H:%M").time(),
                    "ends_at": datetime.strptime(row["ends_at"], "%H:%M").time(),
                },
            )
            if period_created:
                undo["created_periods"].append(period.pk)
            else:
                starts_at = datetime.strptime(row["starts_at"], "%H:%M").time()
                ends_at = datetime.strptime(row["ends_at"], "%H:%M").time()
                # Document imports use registered school timings. In particular,
                # don't round away seconds the minute-based preview cannot show.
                if (period.starts_at != starts_at or period.ends_at != ends_at) and not parsed.get("requires_document_review") and not replacing:
                    undo["updated_periods"].append(
                        {
                            "id": period.pk,
                            "starts_at": period.starts_at.strftime("%H:%M:%S"),
                            "ends_at": period.ends_at.strftime("%H:%M:%S"),
                        }
                    )
                    period.starts_at = starts_at
                    period.ends_at = ends_at
                    period.save(update_fields=("starts_at", "ends_at"))
            period_slots[slot] = True
        moved_id = moves.get((row["day"], row["period_index"], row["class_name"].casefold()))
        if moved_id:
            lesson = ClassLesson.objects.get(settings=settings_obj, pk=moved_id)
            undo["moved_lessons"].append({field: getattr(lesson, field if field != "id" else "pk") for field in snapshot_fields})
            lesson.weekday, lesson.period_index = row["day"], row["period_index"]
            lesson.school_class, lesson.subject, lesson.teacher, lesson.is_active = school_class, subject, teacher, True
            lesson.save(update_fields=("weekday", "period_index", "school_class", "subject", "teacher", "is_active"))
        lesson, lesson_created = ClassLesson.objects.get_or_create(
            settings=settings_obj,
            weekday=row["day"],
            period_index=row["period_index"],
            school_class=school_class,
            defaults={"subject": subject, "teacher": teacher, "is_active": True},
        )
        if lesson_created:
            undo["created_lessons"].append(lesson.pk)
        elif (
            lesson.subject_id != subject.pk
            or lesson.teacher_id != teacher.pk
            or not lesson.is_active
        ):
            undo["updated_lessons"].append(
                {
                    "id": lesson.pk,
                    "subject_id": lesson.subject_id,
                    "teacher_id": lesson.teacher_id,
                    "is_active": lesson.is_active,
                }
            )
            lesson.subject = subject
            lesson.teacher = teacher
            lesson.is_active = True
            lesson.save(update_fields=("subject", "teacher", "is_active"))
        imported_lessons += 1
    _validate_imported_timetable(settings_obj, {row["day"] for row in parsed["rows"]})
    undo["expected_state"] = _import_state_fingerprint(settings_obj)
    return {
        "lessons": imported_lessons,
        "teachers": Teacher.objects.filter(school=school).count(),
        "subjects": Subject.objects.filter(school=school).count(),
        "classes": SchoolClass.objects.filter(settings=settings_obj).count(),
        "deleted_lessons": len(undo["deleted_lessons"]),
        "moved_lessons": len(undo["moved_lessons"]),
        "undo": undo,
    }


@transaction.atomic
def undo_import(*, school, undo: dict) -> dict:
    """Reverse one import without touching records that existed before it."""
    settings_obj = SchoolSettings.objects.select_for_update().get(school=school)
    list(ClassLesson.objects.select_for_update().filter(settings=settings_obj).values_list("pk", flat=True))
    if not undo.get("expected_state") or undo["expected_state"] != _import_state_fingerprint(settings_obj):
        raise ValueError("تغيرت بيانات المدرسة أو الجدول بعد الاستيراد. تعذر التراجع حفاظًا على التعديلات اللاحقة.")
    restored = 0

    for item in undo.get("updated_lessons", []):
        restored += ClassLesson.objects.filter(
            pk=item["id"], settings=settings_obj
        ).update(
            subject_id=item["subject_id"],
            teacher_id=item["teacher_id"],
            is_active=item["is_active"],
        )
    deleted_lessons, _ = ClassLesson.objects.filter(
        settings=settings_obj,
        pk__in=undo.get("created_lessons", []),
    ).delete()
    for item in undo.get("moved_lessons", []):
        restored += ClassLesson.objects.filter(settings=settings_obj, pk=item["id"]).update(
            **{field: value for field, value in item.items() if field != "id"})
    deleted_snapshots = undo.get("deleted_lessons", [])
    ClassLesson.objects.bulk_create([ClassLesson(settings=settings_obj, **item) for item in deleted_snapshots])
    restored += len(deleted_snapshots)

    day_ids = list(DaySchedule.objects.filter(settings=settings_obj).values_list("pk", flat=True))
    for item in undo.get("updated_periods", []):
        restored += Period.objects.filter(pk=item["id"], day_id__in=day_ids).update(
            starts_at=datetime.strptime(item["starts_at"], "%H:%M:%S").time(),
            ends_at=datetime.strptime(item["ends_at"], "%H:%M:%S").time(),
        )
    deleted_periods, _ = Period.objects.filter(
        day_id__in=day_ids,
        pk__in=undo.get("created_periods", []),
    ).delete()

    for item in undo.get("updated_days", []):
        restored += DaySchedule.objects.filter(pk=item["id"], settings=settings_obj).update(
            is_active=item["is_active"],
            periods_count=item["periods_count"],
        )
    deleted_days, _ = DaySchedule.objects.filter(
        settings=settings_obj,
        pk__in=undo.get("created_days", []),
    ).delete()

    created_class_ids = undo.get("created_classes", [])
    created_subject_ids = undo.get("created_subjects", [])
    created_teacher_ids = undo.get("created_teachers", [])
    deleted_classes, _ = SchoolClass.objects.filter(
        settings=settings_obj,
        pk__in=created_class_ids,
        class_lessons__isnull=True,
        periods__isnull=True,
    ).delete()
    deleted_subjects, _ = Subject.objects.filter(
        school=school,
        pk__in=created_subject_ids,
        class_lessons__isnull=True,
        periods__isnull=True,
    ).delete()
    deleted_teachers, _ = Teacher.objects.filter(
        school=school,
        pk__in=created_teacher_ids,
        class_lessons__isnull=True,
        periods__isnull=True,
    ).delete()
    return {
        "restored": restored,
        "deleted": (
            deleted_lessons
            + deleted_periods
            + deleted_days
            + deleted_classes
            + deleted_subjects
            + deleted_teachers
        ),
    }
