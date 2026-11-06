"""Describe exact timetable changes without guessing the identity of lessons."""
from collections import defaultdict

from schedule.models import ClassLesson
from .excel_import import DAY_LABELS

CHANGE_LABELS = {"added": "إضافة", "updated": "تعديل", "moved": "نقل", "deleted": "حذف"}


def lesson_slot(row):
    return row["day"], row["period_index"], row["class_name"].casefold()


def _identity(row):
    return tuple(row[field].casefold() for field in ("class_name", "subject_name", "teacher_name"))


def lesson_changes(*, settings_obj, parsed):
    replacing = parsed.get("strategy") == "replace"
    old = {}
    if settings_obj:
        for lesson in ClassLesson.objects.filter(settings=settings_obj).select_related("school_class", "subject", "teacher").order_by("weekday", "period_index", "pk"):
            item = {"id": lesson.pk, "day": lesson.weekday, "day_label": DAY_LABELS[lesson.weekday],
                    "period_index": lesson.period_index, "class_name": lesson.school_class.name,
                    "subject_name": lesson.subject.name, "teacher_name": lesson.teacher.name, "is_active": lesson.is_active}
            old[lesson_slot(item)] = item
    new = {lesson_slot(row): {**{field: row[field] for field in ("day", "period_index", "class_name", "subject_name", "teacher_name")},
                             "day_label": DAY_LABELS[row["day"]], "is_active": True}
           for row in parsed.get("rows", []) if not row.get("errors")}
    changes, unchanged = [], 0

    def add(kind, before=None, after=None):
        changes.append({"kind": kind, "label": CHANGE_LABELS[kind], "before": before, "after": after})

    for slot in new.keys() & old.keys():
        before, after = old[slot], new[slot]
        if _identity(before) == _identity(after) and before["is_active"]:
            unchanged += 1
        else:
            add("updated", before, after)
    added = {key: new[key] for key in new.keys()-old.keys()}
    removed = {key: old[key] for key in old.keys()-new.keys()} if replacing else {}
    if replacing:
        old_groups, new_groups = defaultdict(list), defaultdict(list)
        for slot, row in removed.items():
            old_groups[_identity(row)].append(slot)
        for slot, row in added.items():
            new_groups[_identity(row)].append(slot)
        # Repeated weekly lessons have no unique identity in a spreadsheet.
        # Label a move only for a one-to-one exact match. Ambiguous cases stay
        # explicit additions/deletions, with the same final timetable outcome.
        for signature in old_groups.keys() & new_groups.keys():
            if len(old_groups[signature]) == len(new_groups[signature]) == 1:
                add("moved", removed.pop(old_groups[signature][0]), added.pop(new_groups[signature][0]))
    for row in removed.values():
        add("deleted", before=row)
    for row in added.values():
        add("added", after=row)
    order = {"deleted": 0, "moved": 1, "updated": 2, "added": 3}
    changes.sort(key=lambda change: (order[change["kind"]], lesson_slot(change["after"] or change["before"])))
    return {"changes": changes, "new_lessons": sum(item["kind"] == "added" for item in changes),
            "updated_lessons": sum(item["kind"] == "updated" for item in changes),
            "moved_lessons": sum(item["kind"] == "moved" for item in changes),
            "deleted_lessons": sum(item["kind"] == "deleted" for item in changes),
            "unchanged_lessons": unchanged, "existing_lessons": len(old)}
