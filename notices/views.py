# notices/views.py
from __future__ import annotations

from typing import Optional

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from dashboard.decorators import manager_required

from .forms import AnnouncementForm, ExcellenceForm
from .models import Announcement, Excellence


def _get_school_or_deny(request):
    """Return the active school from request or raise PermissionDenied."""
    school = getattr(request, "school", None)
    if not school:
        raise PermissionDenied("لا توجد مدرسة نشطة مرتبطة بحسابك.")
    return school


# =========================
# التنبيهات (Announcements)
# =========================

@manager_required
def ann_list(request: HttpRequest) -> HttpResponse:
    """
    قائمة التنبيهات مع ترقيم صفحات.
    يدعم فلترة بسيطة بالمستقبل (مثلاً ?q= ... ).
    """
    school = _get_school_or_deny(request)
    qs = Announcement.objects.filter(school=school).order_by("-starts_at")
    page = Paginator(qs, 10).get_page(request.GET.get("page"))
    return render(request, "notices/ann_list.html", {"page": page})


@manager_required
def ann_create(request: HttpRequest) -> HttpResponse:
    school = _get_school_or_deny(request)
    if request.method == "POST":
        form = AnnouncementForm(request.POST, school=school)
        if form.is_valid():
            ann = form.save(commit=False)
            ann.school = school
            ann.save()
            messages.success(request, "تم إنشاء التنبيه.")
            return redirect("notices:ann_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = AnnouncementForm(school=school)
    return render(
        request,
        "notices/ann_form.html",
        {"form": form, "title": "إنشاء تنبيه"},
    )


@manager_required
def ann_edit(request: HttpRequest, pk: int) -> HttpResponse:
    school = _get_school_or_deny(request)
    obj = get_object_or_404(Announcement, pk=pk, school=school)
    if request.method == "POST":
        form = AnnouncementForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تحديث التنبيه.")
            return redirect("notices:ann_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = AnnouncementForm(instance=obj)
    return render(
        request,
        "notices/ann_form.html",
        {"form": form, "title": "تعديل تنبيه"},
    )


@manager_required
def ann_delete(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("طريقة غير مدعومة.")
    school = _get_school_or_deny(request)
    obj = get_object_or_404(Announcement, pk=pk, school=school)
    obj.delete()
    messages.success(request, "تم حذف التنبيه.")
    return redirect("notices:ann_list")


# =========================
# بطاقات التميّز (Excellence)
# =========================

@manager_required
def exc_list(request: HttpRequest) -> HttpResponse:
    """
    قائمة بطاقات التميز مع ترقيم صفحات.
    """
    school = _get_school_or_deny(request)
    qs = Excellence.objects.filter(school=school).order_by("priority", "-start_at")
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    return render(request, "notices/exc_list.html", {"page": page})


@manager_required
def exc_create(request: HttpRequest) -> HttpResponse:
    """
    إنشاء بطاقة تميّز — يدعم رفع الصورة (multipart/form-data).
    """
    school = _get_school_or_deny(request)
    if request.method == "POST":
        form = ExcellenceForm(request.POST, request.FILES)
        if form.is_valid():
            exc = form.save(commit=False)
            exc.school = school
            exc.save()
            messages.success(request, "تم إضافة بطاقة التميز.")
            return redirect("notices:exc_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = ExcellenceForm()
    return render(
        request,
        "notices/exc_form.html",
        {"form": form, "title": "إضافة تميز"},
    )


@manager_required
def exc_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """
    تعديل بطاقة تميّز — يدعم استبدال الصورة مع تنظيف القديمة (model.save يتكفل).
    """
    school = _get_school_or_deny(request)
    obj = get_object_or_404(Excellence, pk=pk, school=school)
    if request.method == "POST":
        form = ExcellenceForm(request.POST, request.FILES, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تحديث بطاقة التميز.")
            return redirect("notices:exc_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = ExcellenceForm(instance=obj)
    return render(
        request,
        "notices/exc_form.html",
        {"form": form, "title": "تعديل تميز"},
    )


@manager_required
def exc_delete(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("طريقة غير مدعومة.")
    school = _get_school_or_deny(request)
    obj = get_object_or_404(Excellence, pk=pk, school=school)
    obj.delete()
    messages.success(request, "تم حذف البطاقة.")
    return redirect("notices:exc_list")
