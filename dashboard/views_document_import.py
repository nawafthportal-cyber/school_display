"""School-scoped, review-first Excel/image/PDF timetable import."""
from __future__ import annotations

import json

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import get_random_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .access import get_active_school_or_redirect
from .decorators import manager_required
from .document_import import (
    DocumentImportError, DocumentBatchTooLarge, MAX_ROWS, clean_text, extract_document, is_configured,
    normalize_extraction, parse_draft, prepare_draft, validate_upload,
    school_timing_fingerprint,
)
from .document_batches import plan_document, divide_part
from .document_jobs import JOB_TTL, job_key, process_next_part, finish_extraction
from .excel_import import DAY_LABELS, build_import_impact

DRAFT_TTL = 30 * 60
MAX_EDIT_BYTES = 8 * 1024 * 1024


def _strategy(post, *, mode, default="merge"):
    value = post.get("strategy", default)
    if value not in ("merge", "replace"):
        raise DocumentImportError("اختر دمج التعديلات أو استبدال الجدول كاملًا.")
    if value == "replace" and mode == "entities":
        raise DocumentImportError("استبدال الجدول يتطلب اختيار «الجدول والأسماء» ورفع جدول المدرسة كاملًا.")
    return value


def _draft_key(user_id, school_id, token):
    return f"school-document-draft:{user_id}:{school_id}:{token}"


def _quota(school_id):
    # Increment atomically on the shared production cache, keeping the original
    # expiry. Limits apply to attempts, including unsuccessful provider calls.
    for window, seconds, limit in (
        ("hour", 3600, settings.DOCUMENT_IMPORT_HOURLY_LIMIT),
        ("day", 86400, settings.DOCUMENT_IMPORT_DAILY_LIMIT),
    ):
        key = f"school-document-quota:{school_id}:{window}"
        if cache.add(key, 1, timeout=seconds):
            count = 1
        else:
            try:
                count = cache.incr(key)
            except ValueError:
                raise DocumentImportError("تعذر حجز عملية القراءة. أعد المحاولة بعد قليل.") from None
        if count > limit:
            raise DocumentImportError("وصلت المدرسة إلى حد قراءة الملفات المتاح لهذه الفترة. يمكنك متابعة تعديل المعاينات السابقة أو المحاولة لاحقًا.")


def _edited_draft(post, previous):
    raw = post.get("draft_rows", "")
    if len(raw.encode("utf-8")) > MAX_EDIT_BYTES:
        raise DocumentImportError("بيانات المعاينة أكبر من الحد المسموح.")
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        raise DocumentImportError("تعذر قراءة تعديلات الجدول. حدّث الصفحة وحاول مجددًا.") from None
    entities = {}
    for key in ("teachers", "subjects", "classes"):
        value = post.get(key, "")
        if len(value) > 500_000:
            raise DocumentImportError("قائمة الأسماء أطول من الحد المسموح.")
        entities[key] = [clean_text(line) for line in value.splitlines() if line.strip()]
    result = normalize_extraction({**entities, "rows": rows, "warnings": [], "truncated": False}, row_limit=MAX_ROWS)
    result["warnings"] = previous["warnings"]
    result["file"] = previous["file"]
    result["mode"] = "entities" if post.get("mode") == "entities" else "timetable"
    result["strategy"] = _strategy(post, mode=result["mode"], default=previous.get("strategy", "merge"))
    result["audit"] = previous.get("audit", [])
    result["coverage"] = previous.get("coverage", {})
    if "excel_coverage" in previous:
        result["excel_coverage"] = previous["excel_coverage"]
    return result


def _job_progress(job, token):
    return {"token": token, "state": job["state"], "completed": job["next"],
            "total": len(job.get("parts", [])) or job.get("total", 0), "error": job.get("error", "")}


def _start_job(request, school, parts, mode, pages, strategy="merge"):
    token = get_random_string(40)
    pointer = f"school-document-current-job:{request.user.pk}:{school.pk}"
    previous = cache.get(pointer)
    if previous:
        cache.delete(job_key(request.user.pk, school.pk, previous))
    job = {"state": "running", "next": 0, "parts": parts, "results": [], "mode": mode, "strategy": strategy,
           "file": {"name": request.FILES["document"].name[:180], "pages": pages, "size": request.FILES["document"].size,
                    "unit": "ورقة" if parts[0].get("kind") == "excel" else "صفحة"}}
    cache.set(job_key(request.user.pk, school.pk, token), job, timeout=JOB_TTL)
    cache.set(pointer, token, timeout=JOB_TTL)
    return redirect(f"{reverse('dashboard:school_document_import')}?job={token}")


def _advance_job(request, school):
    token = (request.POST.get("job_token") or "").strip()
    key = job_key(request.user.pk, school.pk, token)
    job = cache.get(key) if len(token) == 40 else None
    if not job:
        return JsonResponse({"error": "انتهت صلاحية القراءة أو لا تتبع المدرسة الحالية. ارفع الملف مجددًا."}, status=400)
    if job["state"] == "complete":
        return JsonResponse({"state": "complete", "url": f"{reverse('dashboard:school_document_import')}?draft={job['draft_token']}"})
    lease_key = f"school-document-running:{school.pk}"
    lease = get_random_string(40)
    if not cache.add(lease_key, lease, timeout=115):
        return JsonResponse({"state": "busy", "message": "تجري قراءة جزء آخر. انتظر قليلًا."})
    try:
        job = cache.get(key)
        if not job or job["state"] == "complete":
            return JsonResponse({"state": "busy"})
        job.pop("error", None)
        complete = process_next_part(job)
        if complete:
            extraction = finish_extraction(job)
            draft = prepare_draft(school=school, extraction=extraction)
            draft["mode"], draft["file"] = job["mode"], job["file"]
            draft["strategy"] = job.get("strategy", "merge")
            draft_token = get_random_string(40)
            cache.set(_draft_key(request.user.pk, school.pk, draft_token), draft, timeout=DRAFT_TTL)
            # Remove all original page/row images as soon as the draft is ready.
            job = {"state": "complete", "next": len(job["parts"]), "total": len(job["parts"]), "draft_token": draft_token}
            cache.set(key, job, timeout=JOB_TTL)
            return JsonResponse({"state": "complete", "url": f"{reverse('dashboard:school_document_import')}?draft={draft_token}"})
        cache.set(key, job, timeout=JOB_TTL)
        return JsonResponse(_job_progress(job, token))
    except DocumentImportError as exc:
        job["state"], job["error"] = "paused", str(exc)
        cache.set(key, job, timeout=JOB_TTL)
        return JsonResponse(_job_progress(job, token))
    finally:
        if cache.get(lease_key) == lease:
            cache.delete(lease_key)


@manager_required
@never_cache
@require_http_methods(["GET", "POST"])
def school_document_import(request):
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    if request.method == "POST" and request.POST.get("action") == "advance":
        return _advance_job(request, school)
    token = (request.POST.get("draft_token") or request.GET.get("draft") or "").strip()
    draft = cache.get(_draft_key(request.user.pk, school.pk, token)) if len(token) == 40 else None
    parsed = None
    impact = None
    import_token = ""
    status = 200
    job_view = None
    job_token = (request.GET.get("job") or "").strip()
    if len(job_token) == 40:
        job = cache.get(job_key(request.user.pk, school.pk, job_token))
        if job and job["state"] == "complete":
            return redirect(f"{reverse('dashboard:school_document_import')}?draft={job['draft_token']}")
        if job:
            job_view = _job_progress(job, job_token)
        else:
            messages.error(request, "انتهت صلاحية القراءة. ارفع الملف مجددًا.")

    if request.method == "POST" and request.POST.get("action") == "extract":
        lease_key = f"school-document-running:{school.pk}"
        lease = get_random_string(40)
        locked = False
        try:
            if not is_configured():
                raise DocumentImportError("ميزة الاستيراد الذكي غير مفعلة حاليًا. تواصل مع مسؤول المنصة.")
            mode = "entities" if request.POST.get("mode") == "entities" else "timetable"
            strategy = _strategy(request.POST, mode=mode)
            data, mime, pages = validate_upload(request.FILES.get("document"))
            locked = cache.add(lease_key, lease, timeout=115)
            if not locked:
                raise DocumentImportError("تجري قراءة ملف آخر لهذه المدرسة. انتظر اكتمالها ثم حاول مجددًا.")
            _quota(school.pk)
            parts = plan_document(data=data, mime_type=mime)
            if len(parts) > 1 or not parts[0].get("direct"):
                return _start_job(request, school, parts, mode, pages, strategy)
            try:
                extraction = extract_document(data=data, mime_type=mime, mode=mode)
            except DocumentBatchTooLarge:
                return _start_job(request, school, divide_part(parts[0]), mode, pages, strategy)
            draft = prepare_draft(school=school, extraction=extraction)
            draft["mode"] = mode
            draft["strategy"] = strategy
            draft["file"] = {"name": request.FILES["document"].name[:180], "pages": pages, "size": request.FILES["document"].size}
            token = get_random_string(40)
            cache.set(_draft_key(request.user.pk, school.pk, token), draft, timeout=DRAFT_TTL)
            return redirect(f"{reverse('dashboard:school_document_import')}?draft={token}")
        except DocumentImportError as exc:
            messages.error(request, str(exc))
            status = 400
        finally:
            if locked and cache.get(lease_key) == lease:
                cache.delete(lease_key)
    elif request.method == "POST" and request.POST.get("action") == "review":
        if not draft:
            messages.error(request, "انتهت صلاحية المعاينة أو لا تتبع المدرسة الحالية. ارفع الملف مجددًا.")
            status = 400
        else:
            # Revalidation invalidates every previously approved version of this
            # draft, including a tab that still holds an old import token.
            old_token = draft.pop("import_token", "")
            if old_token:
                cache.delete(f"school-data-import:{request.user.pk}:{school.pk}:{old_token}")
            try:
                draft = _edited_draft(request.POST, draft)
                # Refresh registered timings for untouched school-derived times,
                # and fill newly added or previously untimed lessons as needed.
                draft = prepare_draft(school=school, extraction=draft)
                parsed = _parse_for_mode(school, draft)
                if parsed["errors"]:
                    messages.warning(request, "راجع الأخطاء المحددة وصححها، ثم اضغط التحقق مجددًا.")
                else:
                    parsed["file"] = draft["file"]
                    parsed["strategy"] = draft.get("strategy", "merge")
                    if parsed["strategy"] == "replace" and not parsed["rows"]:
                        raise DocumentImportError("لا يمكن استبدال الجدول بقائمة أسماء أو جدول فارغ. ارفع جدول المدرسة كاملًا.")
                    coverage = draft.get("coverage", {})
                    if parsed["strategy"] == "replace" and coverage and coverage.get("completed") != coverage.get("total"):
                        raise DocumentImportError("أكمل قراءة جميع أجزاء الملف قبل استبدال الجدول.")
                    parsed["requires_document_review"] = True
                    parsed["document_draft_token"] = token
                    parsed["school_timing_fingerprint"] = school_timing_fingerprint(school)
                    impact = build_import_impact(school=school, parsed=parsed)
                    if parsed["strategy"] == "replace":
                        parsed["replacement_fingerprint"] = impact["state_fingerprint"]
                    import_token = get_random_string(40)
                    cache.set(f"school-data-import:{request.user.pk}:{school.pk}:{import_token}", parsed, timeout=DRAFT_TTL)
                    draft["import_token"] = import_token
                    messages.success(request, "اكتمل التحقق. راجع ملخص الإضافات والتحديثات ثم اعتمد الاستيراد.")
            except DocumentImportError as exc:
                messages.error(request, str(exc))
                status = 400
            cache.set(_draft_key(request.user.pk, school.pk, token), draft, timeout=DRAFT_TTL)
    elif request.method == "POST":
        status = 400
        messages.error(request, "العملية المطلوبة غير صالحة.")
    elif token and not draft:
        messages.error(request, "انتهت صلاحية المعاينة أو لا تتبع المدرسة الحالية. ارفع الملف مجددًا.")

    if draft and parsed is None:
        parsed = _parse_for_mode(school, draft)
    if draft and parsed:
        for raw, row in zip(draft["rows"], parsed["rows"]):
            raw["errors"] = row["errors"]
    return render(request, "dashboard/school_document_import.html", {
        "configured": is_configured(), "draft": draft, "draft_token": token,
        "parsed": parsed, "impact": impact, "import_token": import_token,
        "day_choices": DAY_LABELS.items(), "max_rows": MAX_ROWS,
        "job": job_view,
        "review_errors": (parsed or {}).get("errors", [])[:100],
        "hidden_review_errors": max(0, len((parsed or {}).get("errors", []))-100),
    }, status=status)


def _parse_for_mode(school, draft):
    if draft["mode"] != "entities":
        return parse_draft(school=school, draft=draft)
    # Explicit names-only mode imports the names visible in lessons too, without
    # requiring or inventing the times and relationships missing from the file.
    names = {key: list(values) for key, values in draft["entities"].items()}
    for key, field in (("teachers", "teacher_name"), ("subjects", "subject_name"), ("classes", "class_name")):
        names[key] = list(dict.fromkeys([*names[key], *[row[field] for row in draft["rows"] if row[field]]]))
    return parse_draft(school=school, draft={"entities": names, "rows": [], "warnings": draft["warnings"], "audit": draft.get("audit", [])}, names_only=True)
