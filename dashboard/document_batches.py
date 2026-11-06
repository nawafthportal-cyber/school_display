"""Bounded page/row batches and independently checked native PDF cells.

No school writes, public files or background threads. A browser advances one
batch per request, so a large import survives proxy timeouts and page reloads.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from copy import deepcopy
from io import BytesIO
import math
import re
from threading import Lock
import unicodedata

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

from .document_import import (
    DIGITS, DAY_ALIASES, DocumentImportError, DocumentBatchTooLarge,
    EXTRACTION_INSTRUCTIONS, EXTRACTION_SCHEMA, MAX_ROWS, MAX_IMAGE_PIXELS,
    _object_schema, clean_text, extract_document, normalize_extraction,
)
from .excel_import import DAY_LABELS

PDFIUM_LOCK = Lock()  # PDFium explicitly forbids simultaneous calls in threads.
MAX_PARTS = 160
MAX_PLAN_BYTES = 40 * 1024 * 1024
RENDER_SCALE = 3


def _png(image):
    result = BytesIO()
    image.save(result, format="PNG")
    return result.getvalue()


def _runs(values, gap=6):
    groups = []
    for value in values:
        if not groups or value-groups[-1][-1] > gap:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [round((group[0]+group[-1])/2) for group in groups]


def _grid(image):
    gray = image.convert("L")
    # Histogram operations execute in Pillow, rather than walking every pixel
    # in Python. Strong full-width rules distinguish rows from printed text.
    horizontal = _runs([y for y in range(gray.height)
                        if sum(gray.crop((0, y, gray.width, y+1)).histogram()[:85]) > gray.width*.42])
    if len(horizontal) < 5:
        return None
    top, bottom = horizontal[0], horizontal[-1]
    vertical = _runs([x for x in range(gray.width)
                      if sum(gray.crop((x, top, x+1, bottom)).histogram()[:85]) > (bottom-top)*.66])
    if not 4 <= len(vertical) <= 160 or len(horizontal) > 350:
        return None
    return vertical, horizontal


def _native_cells(page, size, xs, ys):
    cells = defaultdict(list)
    scale_x, scale_y = size[0]/float(page.mediabox.width), size[1]/float(page.mediabox.height)
    def visit(text, cm, tm, font, font_size):
        if not text.strip():
            return
        x = (cm[0]*tm[4]+cm[2]*tm[5]+cm[4])*scale_x
        y = (float(page.mediabox.height)-(cm[1]*tm[4]+cm[3]*tm[5]+cm[5]))*scale_y
        column, row = bisect_right(xs, x)-1, bisect_right(ys, y)-1
        if 0 <= column < len(xs)-1 and 0 <= row < len(ys)-1:
            cells[row, column].append((x, unicodedata.normalize("NFKC", text.strip())))
    if page.rotation or page.cropbox != page.mediabox or float(page.get("/UserUnit", 1)) != 1:
        return cells  # Image batches handle rotated pages; don't guess matrices.
    page.extract_text(visitor_text=visit)
    return cells


def _physical_text(pieces):
    return "".join(text for _, text in sorted(pieces)).replace("ͭ", "/").replace(" ", "")


def class_code(value):
    """Canonicalize only numeric/letter codes, keeping ordinary class names."""
    value = clean_text(value)
    code = re.sub(r"[\s\u200e\u200f\u202a-\u202e]", "", value).translate(DIGITS)
    if re.fullmatch(r"[\u0621-\u064a]+/\d+", code):
        code = "/".join(reversed(code.split("/")))
    return code if re.fullmatch(r"\d+/(?:\d+|[\u0621-\u064a]+)", code) else value


def _native_timetable(cells, xs, ys):
    """Recognize the printed header rather than assume weekdays or 7 periods."""
    candidates = []
    for row in range(min(6, len(ys)-1)):
        periods = {}
        for column in range(len(xs)-1):
            text = _physical_text(cells.get((row, column), [])).translate(DIGITS)
            if text.isdigit() and 1 <= int(text) <= 20:
                periods[column] = int(text)
        if len(periods) >= 8:
            candidates.append((len(periods), row, periods))
    if not candidates:
        return None
    _, header_row, periods = max(candidates)
    groups = []
    for column, index in sorted(periods.items(), reverse=True):
        if index == 1:
            groups.append([])
        if not groups or index != len(groups[-1])+1:
            return None
        groups[-1].append(column)
    aliases = {label: label for label in DAY_LABELS.values()}
    aliases.update(DAY_ALIASES)
    slots = {}
    for columns in groups:
        days = set()
        for (row, col), pieces in cells.items():
            if row >= header_row or not min(columns) <= col <= max(columns):
                continue
            for _, text in pieces:
                label = aliases.get(text.strip())
                if label:
                    days.add(label)
        if len(days) != 1:
            return None
        day = days.pop()
        for column in columns:
            slots[column] = (day, periods[column])
    if len({day for day, _ in slots.values()}) != len(groups):
        return None
    # A teacher timetable has named teacher AND subject headings outside slots.
    headings = " ".join(text for (r, c), pieces in cells.items() if r <= header_row and c not in slots for _, text in pieces)
    if "معلم" not in headings or not any(word in headings for word in ("مقرر", "مادة")):
        return None
    bodies = {}
    for row in range(header_row+1, len(ys)-1):
        lessons = []
        for column, (day, index) in slots.items():
            pieces = cells.get((row, column), [])
            if not pieces:
                continue
            code = class_code(_physical_text(pieces))
            if not re.fullmatch(r"\d+/(?:\d+|[\u0621-\u064a]+)", code):
                return None  # Unknown cell representation: use vision batches.
            lessons.append({"day": day, "period_index": str(index), "class_name": code})
        if lessons:
            bodies[row] = lessons
    teacher_columns = {col for (row, col), pieces in cells.items() if row <= header_row and col not in slots
                       if any("معلم" in text for _, text in pieces)}
    return {"header_row": header_row, "slots": slots, "bodies": bodies, "teacher_columns": teacher_columns} if bodies else None


def _has_ink(image, xs, top, bottom):
    for left, right in zip(xs, xs[1:]):
        if right-left < 14 or bottom-top < 14:
            continue
        gray = image.crop((left+6, top+6, right-6, bottom-6)).convert("L")
        hist = gray.histogram()
        background = max(range(256), key=hist.__getitem__)
        if sum(n for value, n in enumerate(hist) if abs(value-background) > 45) > 20:
            return True
    return False


def _page_parts(image, page_number, native_page=None):
    grid = _grid(image)
    if not grid:
        # Small unruled images retain the original single-request path.
        if image.height <= 1800:
            return [{"page": page_number, "direct": _png(image), "native": False, "rows": []}]
        header = _png(image.crop((0, 0, image.width, min(300, image.height))))
        rows = []
        for index, top in enumerate(range(0, image.height, 700)):
            rows.append({"id": f"p{page_number}-s{index+1}", "image": _png(image.crop((0, max(0, top-30), image.width, min(image.height, top+730)))), "native": None})
        return [{"page": page_number, "header": header, "native": False, "rows": [row]} for row in rows]
    xs, ys = grid
    cells = _native_cells(native_page, image.size, xs, ys) if native_page is not None else {}
    native = _native_timetable(cells, xs, ys) if cells else None
    header_end = ys[native["header_row"]+1] if native else ys[min(2, len(ys)-1)]
    # For native grids the model reads the row labels at full resolution only;
    # the exact printed lesson positions come from independently read PDF cells.
    label_left = xs[max(native["slots"])+1] if native else xs[0]
    label_right = xs[-1]
    if native and min(native["slots"]) > 0:
        label_left, label_right = xs[0], xs[min(native["slots"])]
    header = _png(image.crop((label_left, ys[0], label_right+1, header_end+1)))
    rows = []
    for row, (top, bottom) in enumerate(zip(ys, ys[1:])):
        if native and row <= native["header_row"]:
            continue
        if cells and not any((row, col) in cells for col in range(len(xs)-1)):
            continue
        if not cells and not _has_ink(image, xs, top, bottom):
            continue
        if native and row not in native["bodies"]:
            # Keep metadata-only teacher rows (e.g. zero load) if printed.
            if not any((row, col) in cells for col in range(len(xs)-1) if col not in native["slots"]):
                continue
        printed_teacher = ""
        if native:
            pieces = [piece for col in native["teacher_columns"] for piece in cells.get((row, col), [])]
            printed_teacher = " ".join(text for _, text in sorted(pieces, reverse=True))
            if not re.fullmatch(r"[\u0621-\u064a\s]+", printed_teacher):
                printed_teacher = ""
        rows.append({"id": f"p{page_number}-r{row+1}", "printed_teacher": printed_teacher,
                     "image": _png(image.crop((label_left, top, label_right+1, bottom+1))),
                     "native": deepcopy(native["bodies"].get(row, [])) if native else None})
    size = 8 if native else 3
    return [{"page": page_number, "header": header, "native": bool(native), "rows": rows[index:index+size]}
            for index in range(0, len(rows), size)]


def plan_document(*, data, mime_type):
    from .excel_document_import import EXCEL_MIMES, plan_excel
    if mime_type in EXCEL_MIMES.values():
        return plan_excel(data=data, mime_type=mime_type)
    parts = []
    if mime_type == "application/pdf":
        import pypdfium2 as pdfium
        reader = PdfReader(BytesIO(data))
        with PDFIUM_LOCK:
            document = pdfium.PdfDocument(data)
            try:
                for index in range(len(document)):
                    page = document[index]
                    width, height = page.get_size()
                    scale = min(RENDER_SCALE, 6000/max(width, height), math.sqrt(MAX_IMAGE_PIXELS/(width*height)))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil().convert("RGB").copy()
                    finally:
                        bitmap.close()
                        page.close()
                    # Blank vector pages are common print-export trailing pages.
                    if not reader.pages[index].extract_text().strip() and image.convert("L").histogram()[255] > image.width*image.height*.999:
                        continue
                    parts.extend(_page_parts(image, index+1, reader.pages[index]))
            finally:
                document.close()
    else:
        with Image.open(BytesIO(data)) as image:
            parts = _page_parts(image.convert("RGB"), 1)
    total_bytes = sum(len(part.get("direct", b""))+len(part.get("header", b""))+sum(len(row["image"]) for row in part["rows"]) for part in parts)
    if not parts:
        raise DocumentImportError("لم أجد بيانات في صفحات الملف.")
    if len(parts) > MAX_PARTS or total_bytes > MAX_PLAN_BYTES:
        raise DocumentImportError("الملف يتجاوز سعة المعالجة المتاحة. قلّل عدد الصفحات في الملف.")
    if sum(len(row.get("native") or []) for part in parts for row in part["rows"]) > MAX_ROWS:
        raise DocumentImportError(f"يتجاوز الجدول سعة المعاينة ({MAX_ROWS} حصة).")
    return parts


def _part_image(part):
    header = Image.open(BytesIO(part["header"])).convert("RGB")
    images = [Image.open(BytesIO(row["image"])).convert("RGB") for row in part["rows"]]
    margin = 140
    result = Image.new("RGB", (max([header.width, *[im.width for im in images]])+margin, header.height+24+sum(im.height+6 for im in images)), "white")
    result.paste(header, (margin, 0))
    pen = ImageDraw.Draw(result)
    font = ImageFont.load_default(size=20)
    y = header.height+24
    for row, image in zip(part["rows"], images):
        result.paste(image, (margin, y))
        pen.text((4, y+max(0, (image.height-24)//2)), row["id"], font=font, fill="black")
        y += image.height+6
    return _png(result)


META_PROPERTIES = {
    "id": {"type": "string"}, "teacher_name": {"type": "string"},
    "subject_name": {"type": "string"}, "class_name": {"type": "string"},
    "printed_workload": {"type": "string"}, "note": {"type": "string"},
}
META_SCHEMA = _object_schema({"source_rows": {"type": "array", "items": _object_schema(META_PROPERTIES)},
                              "warnings": {"type": "array", "items": {"type": "string"}}})


def extract_part(part, *, mode):
    if part.get("kind") == "excel":
        from .excel_document_import import extract_excel_part
        return extract_excel_part(part, mode=mode)
    if part.get("direct"):
        return extract_document(data=part["direct"], mime_type="image/png", mode=mode)
    ids = [row["id"] for row in part["rows"]]
    schema = deepcopy(META_SCHEMA if part["native"] else EXTRACTION_SCHEMA)
    if not part["native"]:
        schema["properties"]["source_rows"] = META_SCHEMA["properties"]["source_rows"]
        schema["required"].append("source_rows")
        item = schema["properties"]["rows"]["items"]
        item["properties"]["source_id"] = {"type": "string", "enum": ids}
        item["required"].append("source_id")
    task = ("Read ONLY the labelled target rows, whose exact identifiers are: " + ", ".join(ids) + ". "
            "The unlabelled strip at the top is header CONTEXT ONLY: never extract lessons from it. "
            "Return source_rows with exactly one metadata entry for EVERY target identifier, even blank/header rows. "
            "Copy the teacher name, full subject label including level numbers, class name if a row header, printed teacher workload and any uncertainty. "
            "Use empty strings for absent metadata. Do not infer a teacher or subject not printed. ")
    if any("-s" in identifier for identifier in ids):
        task += "An identifier containing '-s' labels a page SLICE, possibly containing several teacher rows. Extract every visible lesson, but leave its source_rows teacher/subject/workload metadata empty when it represents several teachers. "
    if part["native"]:
        task += "Only transcribe the visible row labels. The lesson cells are processed separately; do not return lessons."
    else:
        task += "Every lesson must carry its source_id from the labelled target row. Never skip a visible lesson. "
        if mode == "entities":
            task += "Extract names only and leave rows empty."
    raw = extract_document(data=_part_image(part), mime_type="image/png", mode=mode, schema=schema,
                           instructions=EXTRACTION_INSTRUCTIONS, task=task, raw=True)
    metadata = raw.get("source_rows") if isinstance(raw, dict) else None
    if not isinstance(metadata, list) or len(metadata) != len(ids) or {item.get("id") for item in metadata if isinstance(item, dict)} != set(ids):
        raise DocumentBatchTooLarge("لم تؤكد خدمة القراءة تغطية جميع صفوف الدفعة.")
    records = {item["id"]: {key: clean_text(item.get(key)) for key in META_PROPERTIES} for item in metadata}
    audit = []
    if part["native"]:
        raw = {"teachers": [], "subjects": [], "classes": [], "rows": [], "warnings": raw.get("warnings", []), "truncated": False}
        for source in part["rows"]:
            meta = records[source["id"]]
            for key, field in (("teachers", "teacher_name"), ("subjects", "subject_name"), ("classes", "class_name")):
                if meta[field]:
                    raw[key].append(meta[field])
            if source["native"] and (not meta["teacher_name"] or not meta["subject_name"]):
                raise DocumentBatchTooLarge("تحتاج تسمية أحد صفوف المعلمين إلى قراءة أدق.")
            for slot in source["native"]:
                raw["classes"].append(slot["class_name"])
                if mode != "entities":
                    raw["rows"].append({**slot, "teacher_name": meta["teacher_name"], "subject_name": meta["subject_name"],
                                        "starts_at": "", "ends_at": "", "source_id": source["id"],
                                        "source": f"صفحة {part['page']}، صف {source['id']}", "note": meta["note"]})
            audit.append({**meta, "observed": len(source["native"]), "page": part["page"]})
    else:
        for row in raw.get("rows", []):
            if row.get("source_id") not in ids:
                raise DocumentImportError("وردت حصة من خارج صفوف الدفعة. أعد قراءة هذا الجزء.")
            row["source"] = f"صفحة {part['page']}، صف {row['source_id']}"
            row["class_name"] = class_code(row.get("class_name", ""))
        for meta in records.values():
            audit.append({**meta, "observed": sum(row.get("source_id") == meta["id"] for row in raw.get("rows", [])), "page": part["page"]})
    result = normalize_extraction(raw, allow_empty=True)
    result["audit"] = audit
    return add_native_name_checks(part, result)


def add_native_name_checks(part, result):
    """Surface disagreements with readable embedded text; never merge people."""
    for source in part["rows"]:
        printed = clean_text(source.get("printed_teacher", ""))
        meta = next((entry for entry in result.get("audit", []) if entry["id"] == source["id"]), None)
        if not printed or not meta or printed == meta["teacher_name"]:
            continue
        message = f"صفحة {part['page']}: قُرئ الاسم «{meta['teacher_name'][:60]}»، بينما نص PDF «{printed[:60]}». تحقق من كتابة الاسم."
        if message not in result["warnings"]:
            result["warnings"].append(message)
        for row in result["rows"]:
            if row.get("source_id") == source["id"]:
                row["note"] = (row.get("note", "")+"؛ "+message).strip("؛ ")[:240]
    return result


def divide_part(part):
    if part.get("kind") == "excel":
        from .excel_document_import import divide_excel_part
        return divide_excel_part(part)
    if part.get("direct"):
        image = Image.open(BytesIO(part["direct"])).convert("RGB")
        part = {"page": part["page"], "native": False,
                "header": _png(image.crop((0, 0, image.width, min(250, image.height//4)))),
                "rows": [{"id": f"p{part['page']}-s1", "image": part["direct"], "native": None}]}
    if len(part["rows"]) == 1 and not part["native"]:
        row = part["rows"][0]
        image = Image.open(BytesIO(row["image"])).convert("RGB")
        if image.height > 180:
            middle = image.height//2
            children = []
            for suffix, top, bottom in (("a", 0, middle+20), ("b", middle-20, image.height)):
                child = {"id": row["id"]+suffix, "image": _png(image.crop((0, top, image.width, bottom))), "native": None}
                children.append({**part, "rows": [child]})
            return children
    if len(part["rows"]) < 2:
        raise DocumentImportError("تعذرت قراءة صف كامل بدقة. يمكنك إعادة محاولة الجزء دون فقدان الأجزاء المكتملة.")
    middle = len(part["rows"])//2
    return [{**part, "rows": part["rows"][:middle]}, {**part, "rows": part["rows"][middle:]}]


def merge_extractions(results):
    merged = {"rows": [], "entities": {"teachers": [], "subjects": [], "classes": []}, "warnings": [], "audit": []}
    seen = set()
    duplicate_count = 0
    for result in results:
        for row in result["rows"]:
            key = tuple(row.get(field, "") for field in ("day", "period_index", "class_name", "subject_name", "teacher_name", "starts_at", "ends_at"))
            # Incomplete cells must remain individually reviewable, even when
            # all seven lesson fields are blank.
            if not all(row.get(field) for field in ("day", "period_index", "class_name", "subject_name", "teacher_name")):
                key += (row.get("source_id", ""), row.get("source", ""), row.get("note", ""))
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            merged["rows"].append(row)
        for key in merged["entities"]:
            merged["entities"][key].extend(result["entities"].get(key, []))
        merged["warnings"].extend(result.get("warnings", []))
        merged["audit"].extend(result.get("audit", []))
        if result.get("excel_coverage"):
            coverage = merged.setdefault("excel_coverage", {"cells": 0, "unresolved": 0})
            for field in coverage:
                coverage[field] += result["excel_coverage"][field]
    if len(merged["rows"]) > MAX_ROWS:
        raise DocumentImportError(f"يتجاوز الجدول سعة المعاينة ({MAX_ROWS} حصة).")
    for key in merged["entities"]:
        merged["entities"][key] = list(dict.fromkeys(merged["entities"][key]))
    merged["warnings"] = list(dict.fromkeys(merged["warnings"]))
    if duplicate_count:
        merged["warnings"].append(f"دُمجت {duplicate_count} حصة متطابقة ظهرت في أكثر من جزء؛ بقيت التعارضات المختلفة للمراجعة.")
    if not merged["rows"] and not any(merged["entities"].values()):
        raise DocumentImportError("لم أجد أسماء أو حصصًا قابلة للقراءة في الملف.")
    return merged
