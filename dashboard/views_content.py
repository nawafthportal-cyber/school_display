"""What the screens show: announcements, excellence, standby cover,

supervision duty and emergency alerts.
"""

from __future__ import annotations
from core.occasion_customizations import cards_for_school, customizations_for_school

from django.db.models import Exists, OuterRef, Prefetch

from .view_helpers import *  # noqa: F401,F403  (shared view layer)


def _occasion_suggestions_for(school, *, today):
    """المناسبات القادمة التي لم تُفعَّل بعد لهذه المدرسة.

    اقتراح لا تفعيل: تغيير مظهر شاشات مدرسة دون قرار بشري تصرّف لا يُغتفر إن
    أخطأ التوقيت أو النص. الاقتراح يمنح نفس الفائدة — ألا ينسى المدير مناسبة
    تاريخها ثابت ومعروف — بلا تلك المخاطرة.
    """
    Announcement = AnnouncementModel()
    try:
        upcoming = occasions.upcoming(today)
        if not upcoming:
            return []

        # مناسبة لها تنبيه مُعدّ سلفًا (مفعّل أو مجدول) لم تعد بحاجة لتذكير.
        already_prepared = set(
            Announcement.objects.filter(
                school=school,
                is_active=True,
                occasion_theme__in=[item.occasion.key for item in upcoming],
            )
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
            .values_list("occasion_theme", flat=True)
        )
        return [item for item in upcoming if item.occasion.key not in already_prepared]
    except Exception:
        # تذكير مفقود أهون بكثير من صفحة رئيسية معطّلة.
        logger.exception("occasion_suggestions_failed school_id=%s", getattr(school, "id", None))
        return []

@manager_required
def ann_list(request):
    Announcement = AnnouncementModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    qs = Announcement.objects.filter(school=school).prefetch_related("screens").order_by("-starts_at", "-id")
    now = timezone.now()
    active_q = (
        Q(is_active=True)
        & (Q(starts_at__lte=now) | Q(starts_at__isnull=True))
        & (Q(expires_at__gt=now) | Q(expires_at__isnull=True))
    )
    future_q = Q(is_active=True) & Q(starts_at__gt=now)

    active_count = qs.filter(active_q).count()
    future_count = qs.filter(future_q).count()
    total_count = qs.count()
    expired_count = max(total_count - active_count - future_count, 0)

    page = Paginator(qs, 10).get_page(request.GET.get("page"))
    occasion_customizations = customizations_for_school(school.pk)
    for ann in page.object_list:
        ann.occasion_theme_label = occasion_customizations.get(ann.occasion_theme, {}).get("name", ann.get_occasion_theme_display())
        if ann.active_now:
            ann.dashboard_status_key = "active"
            ann.dashboard_status_label = "فعال الآن"
            ann.dashboard_status_hint = "يظهر على الشاشة"
        elif ann.is_active and ann.starts_at and ann.starts_at > now:
            ann.dashboard_status_key = "future"
            ann.dashboard_status_label = "مجدول"
            ann.dashboard_status_hint = "سيظهر لاحقاً"
        elif not ann.is_active:
            ann.dashboard_status_key = "paused"
            ann.dashboard_status_label = "متوقف"
            ann.dashboard_status_hint = "لن يظهر على الشاشة"
        else:
            ann.dashboard_status_key = "expired"
            ann.dashboard_status_label = "منتهي"
            ann.dashboard_status_hint = "انتهت مدة عرضه"

    return render(
        request,
        "dashboard/ann_list.html",
        {
            "page": page,
            "active_count": active_count,
            "future_count": future_count,
            "expired_count": expired_count,
        },
    )

@manager_required
def ann_create(request):
    Announcement = AnnouncementModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    if request.method == "POST":
        form = AnnouncementForm(request.POST, school=school)
        if form.is_valid():
            ann = form.save(commit=False)
            ann.school = school
            ann.save()
            form.save_m2m()
            _invalidate_display_cache(school)
            messages.success(request, "تم إنشاء التنبيه.")
            return redirect("dashboard:ann_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        initial = {}
        occasion = occasions.get((request.GET.get("template") or "").strip())
        if occasion is not None:
            starts_at = timezone.now().replace(second=0, microsecond=0)
            initial = {
                "title": occasion.title,
                "body": occasion.body,
                "level": occasion.level,
                "occasion_theme": occasion.key,
                "starts_at": starts_at,
                "expires_at": starts_at + timedelta(hours=occasion.duration_hours),
            }
        form = AnnouncementForm(initial=initial, school=school)
    return render(
        request,
        "dashboard/ann_form.html",
        {
            "form": form,
            "title": "إنشاء تنبيه",
            "occasion_cards": cards_for_school(school.pk),
        },
    )

def _shell_base_template(request) -> str:
    """Render delegated-employee pages inside the admin console shell.

    A few screens (emergency alerts) are reachable from both the school
    dashboard and the platform sidebar.  Serving the school shell to a platform
    employee produced a navigation dead-end: every link in it points at
    manager-only views ``manager_required`` refuses for platform identities.
    The owner keeps the school shell because both panels stay usable for them.
    """

    user = getattr(request, "user", None)
    if is_system_staff_user(user) and not getattr(user, "is_superuser", False):
        return "admin/admin_base.html"
    return "dashboard/_base.html"

def _emergency_allowed_schools(request):
    School = SchoolModel()
    if getattr(request.user, "is_superuser", False) or has_system_permission(
        request.user, "emergency_alerts.view"
    ):
        return School.objects.filter(is_active=True)
    profile = _get_or_create_profile(request.user)
    return profile.schools.filter(is_active=True)


def _emergency_manageable_alerts(request, allowed_schools):
    from notices.models import EmergencyAlert

    alerts = EmergencyAlert.objects.filter(schools__in=allowed_schools).distinct()
    if not is_system_staff_user(request.user):
        # Cancelling/deleting changes the shared alert for *every* target.
        # Membership in one target school grants visibility, not authority over
        # other tenants. Include inactive schools in this boundary as well.
        foreign_schools = SchoolModel().objects.exclude(pk__in=allowed_schools.values("pk"))
        alerts = alerts.exclude(schools__in=foreign_schools)
    return alerts


@manager_or_system_permission_required("emergency_alerts.view")
def emergency_alert_list(request):
    from notices.models import EmergencyAlert

    allowed_schools = _emergency_allowed_schools(request)
    now = timezone.now()
    alerts = (
        EmergencyAlert.objects.filter(schools__in=allowed_schools)
        .annotate(has_targeted_screens=Exists(
            EmergencyAlert.screens.through.objects.filter(emergencyalert_id=OuterRef("pk"))
        ))
        .select_related("created_by", "cancelled_by")
        .distinct()
        .order_by("-created_at")
    )
    if is_system_staff_user(request.user):
        alerts = alerts.prefetch_related("schools", "screens")
    else:
        alerts = alerts.prefetch_related(
            Prefetch("schools", queryset=allowed_schools),
            Prefetch("screens", queryset=DisplayScreenModel().objects.filter(school__in=allowed_schools)),
        )
    can_manage = not is_system_staff_user(request.user) or has_system_permission(request.user, "emergency_alerts.manage")
    visible_alerts = list(alerts[:100])
    manageable_ids = set(
        _emergency_manageable_alerts(request, allowed_schools)
        .filter(pk__in=[alert.pk for alert in visible_alerts])
        .values_list("pk", flat=True)
    ) if can_manage else set()
    for alert in visible_alerts:
        alert.can_manage_for_user = alert.pk in manageable_ids
    return render(
        request,
        "dashboard/emergency_alert_list.html",
        {
            "base_template": _shell_base_template(request),
            "alerts": visible_alerts,
            "active_count": alerts.filter(
                is_active=True,
                cancelled_at__isnull=True,
                starts_at__lte=now,
            )
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
            .count(),
            "can_emergency_alerts_manage": can_manage,
        },
    )

@manager_or_system_permission_required("emergency_alerts.manage")
def emergency_alert_create(request):
    allowed_schools = _emergency_allowed_schools(request)
    initial = {}
    active_school = getattr(getattr(request.user, "profile", None), "active_school", None)
    if active_school and allowed_schools.filter(pk=active_school.pk).exists():
        initial["schools"] = [active_school.pk]
    if request.method == "POST":
        form = EmergencyAlertForm(request.POST, allowed_schools=allowed_schools)
        if form.is_valid():
            alert = form.save(commit=False)
            alert.created_by = request.user
            alert.save()
            form.save_m2m()
            for school in alert.schools.all():
                _invalidate_display_cache(school, force_bump=True)
            messages.success(request, "تم إرسال التنبيه الطارئ وتسجيل المرسل.")
            return redirect("dashboard:emergency_alert_list")
        messages.error(request, "تعذر إرسال التنبيه. راجع الحقول الموضحة.")
    else:
        form = EmergencyAlertForm(initial=initial, allowed_schools=allowed_schools)
    return render(
        request,
        "dashboard/emergency_alert_form.html",
        {
            "base_template": _shell_base_template(request),
            "form": form,
            "emergency_templates": {
                "evacuation": {"title": "إخلاء فوري", "message": "يرجى إخلاء المبنى بهدوء والتوجه إلى نقاط التجمع المعتمدة."},
                "fire": {"title": "تنبيه حريق", "message": "يرجى تنفيذ خطة الإخلاء فورًا وعدم استخدام المصاعد."},
                "weather": {"title": "تنبيه حالة جوية", "message": "يرجى البقاء في الأماكن الآمنة واتباع تعليمات إدارة المدرسة."},
                "suspension": {"title": "تعليق الدراسة", "message": "تم تعليق الدراسة. يرجى متابعة القنوات الرسمية للمدرسة."},
                "urgent": {"title": "رسالة عاجلة", "message": "تنبيه عاجل من إدارة المدرسة."},
            },
        },
    )

@manager_or_system_permission_required("emergency_alerts.manage")
@require_POST
def emergency_alert_cancel(request, pk: int):
    from notices.models import EmergencyAlert

    allowed_schools = _emergency_allowed_schools(request)
    alert = get_object_or_404(
        _emergency_manageable_alerts(request, allowed_schools),
        pk=pk,
    )
    if alert.cancelled_at is None:
        alert.is_active = False
        alert.cancelled_by = request.user
        alert.cancelled_at = timezone.now()
        alert.save(update_fields=("is_active", "cancelled_by", "cancelled_at"))
        for school in alert.schools.all():
            _invalidate_display_cache(school, force_bump=True)
        messages.success(request, "تم إلغاء التنبيه وتسجيل وقت الإلغاء.")
    return redirect("dashboard:emergency_alert_list")

@manager_or_system_permission_required("emergency_alerts.manage")
@require_POST
def emergency_alert_delete(request, pk: int):
    from notices.models import EmergencyAlert

    allowed_schools = _emergency_allowed_schools(request)
    alert = get_object_or_404(
        _emergency_manageable_alerts(request, allowed_schools),
        pk=pk,
    )
    if alert.active_now:
        messages.error(request, "يجب إلغاء التنبيه النشط قبل حذفه.")
        return redirect("dashboard:emergency_alert_list")

    affected_schools = list(alert.schools.all())
    alert.delete()
    for school in affected_schools:
        _invalidate_display_cache(school, force_bump=True)
    messages.success(request, "تم حذف التنبيه القديم نهائيًا.")
    return redirect("dashboard:emergency_alert_list")

@manager_required
def occasion_templates(request):
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    return render(
        request,
        "dashboard/occasion_templates.html",
        {
            "occasion_cards": cards_for_school(school.pk),
            "upcoming_occasions": occasions.upcoming(timezone.localdate(), lead_days=60),
        },
    )

@manager_required
def ann_edit(request, pk: int):
    Announcement = AnnouncementModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    obj = get_object_or_404(Announcement, pk=pk, school=school)
    if request.method == "POST":
        form = AnnouncementForm(request.POST, instance=obj, school=school)
        if form.is_valid():
            form.save()
            _invalidate_display_cache(school)
            messages.success(request, "تم تحديث التنبيه.")
            return redirect("dashboard:ann_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = AnnouncementForm(instance=obj, school=school)
    return render(
        request,
        "dashboard/ann_form.html",
        {
            "form": form,
            "title": "تعديل تنبيه",
            "occasion_cards": cards_for_school(school.pk),
        },
    )

@manager_required
@require_POST
def ann_delete(request, pk: int):
    Announcement = AnnouncementModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    obj = get_object_or_404(Announcement, pk=pk, school=school)
    obj.delete()
    _invalidate_display_cache(school)
    messages.success(request, "تم حذف التنبيه.")
    return redirect("dashboard:ann_list")

@manager_required
def exc_list(request):
    Excellence = ExcellenceModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    qs = Excellence.objects.filter(school=school).order_by("priority", "-start_at", "-id")

    now = timezone.now()
    active_count = Excellence.objects.filter(
        Q(school=school) & Q(start_at__lte=now) & (Q(end_at__isnull=True) | Q(end_at__gt=now))
    ).count()
    expired_count = Excellence.objects.filter(school=school, end_at__lte=now).count()
    max_p = Excellence.objects.filter(school=school).aggregate(m=Max("priority"))["m"] or 0

    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    for item in page.object_list:
        if item.active_now:
            item.dashboard_status_key = "active"
            item.dashboard_status_label = "نشطة"
            item.dashboard_status_hint = "تظهر على الشاشة"
        elif item.start_at and item.start_at > now:
            item.dashboard_status_key = "future"
            item.dashboard_status_label = "مجدولة"
            item.dashboard_status_hint = "ستظهر لاحقاً"
        else:
            item.dashboard_status_key = "expired"
            item.dashboard_status_label = "منتهية"
            item.dashboard_status_hint = "انتهت مدة العرض"

    return render(
        request,
        "dashboard/exc_list.html",
        {"page": page, "active_count": active_count, "expired_count": expired_count, "max_priority": max_p},
    )

@manager_required
def exc_create(request):
    Excellence = ExcellenceModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    if request.method == "POST":
        form = ExcellenceForm(request.POST, request.FILES)
        if form.is_valid():
            exc = form.save(commit=False)
            exc.school = school
            exc.save()
            _invalidate_display_cache(school)
            messages.success(request, "تم إضافة بطاقة التميز.")
            return redirect("dashboard:exc_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = ExcellenceForm()
    return render(request, "dashboard/exc_form.html", {"form": form, "title": "إضافة تميز"})

@manager_required
def exc_edit(request, pk: int):
    Excellence = ExcellenceModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    obj = get_object_or_404(Excellence, pk=pk, school=school)
    if request.method == "POST":
        form = ExcellenceForm(request.POST, request.FILES, instance=obj)
        if form.is_valid():
            form.save()
            _invalidate_display_cache(school)
            messages.success(request, "تم تحديث بطاقة التميز.")
            return redirect("dashboard:exc_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = ExcellenceForm(instance=obj)
    return render(request, "dashboard/exc_form.html", {"form": form, "title": "تعديل تميز"})

@manager_required
@require_POST
def exc_delete(request, pk: int):
    Excellence = ExcellenceModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    obj = get_object_or_404(Excellence, pk=pk, school=school)
    obj.delete()
    _invalidate_display_cache(school)
    messages.success(request, "تم حذف البطاقة.")
    return redirect("dashboard:exc_list")

@manager_required
def standby_list(request):
    StandbyAssignment = StandbyAssignmentModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    # فلتر التاريخ (مثل صفحة الإشراف والمناوبة)
    selected_date = timezone.localdate()
    date_str = request.GET.get("date", "").strip()
    if date_str:
        try:
            selected_date = timezone.datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            selected_date = timezone.localdate()

    # تصفية حسب التاريخ المحدد
    qs = StandbyAssignment.objects.filter(
        school=school,
        date=selected_date
    ).order_by("period_index", "-id")

    # إحصائيات
    total_count = qs.count()
    teachers_count = qs.values("teacher_name").distinct().count()

    page = Paginator(qs, 20).get_page(request.GET.get("page"))

    return render(
        request,
        "dashboard/standby_list.html",
        {
            "page": page,
            "selected_date": selected_date,
            "total_count": total_count,
            "teachers_count": teachers_count,
        },
    )

@manager_required
def standby_create(request):
    StandbyAssignment = StandbyAssignmentModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    initial = {}
    q_date = (request.GET.get("date") or "").strip()
    if q_date:
        try:
            initial["date"] = timezone.datetime.strptime(q_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    if request.method == "POST":
        form = StandbyForm(request.POST, school=school)
        if form.is_valid():
            standby = form.save(commit=False)
            standby.school = school
            standby.save()
            _invalidate_display_cache(school)
            messages.success(request, "تم إضافة تكليف الانتظار.")
            return redirect("dashboard:standby_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = StandbyForm(initial=initial, school=school)

    return render(request, "dashboard/standby_form.html", {"form": form, "title": "إضافة تكليف"})

@manager_required
@require_POST
def standby_delete(request, pk: int):
    StandbyAssignment = StandbyAssignmentModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    obj = get_object_or_404(StandbyAssignment, pk=pk, school=school)
    obj.delete()
    _invalidate_display_cache(school)
    messages.success(request, "تم الحذف.")
    return redirect("dashboard:standby_list")

@manager_required
def duty_list(request):
    DutyAssignment = DutyAssignmentModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    today = timezone.localdate()
    q_date = (request.GET.get("date") or "").strip()
    selected_date = today
    if q_date:
        try:
            selected_date = datetime.strptime(q_date, "%Y-%m-%d").date()
        except ValueError:
            messages.warning(request, "صيغة التاريخ غير صحيحة.")

    include_inactive = (request.GET.get("all") or "").strip() in {"1", "true", "yes"}

    qs = DutyAssignment.objects.filter(school=school, date=selected_date)
    if not include_inactive:
        qs = qs.filter(is_active=True)
    qs = qs.order_by("priority", "duty_type", "teacher_name", "-id")

    total_count = DutyAssignment.objects.filter(school=school, date=selected_date).count()
    active_count = DutyAssignment.objects.filter(school=school, date=selected_date, is_active=True).count()

    page = Paginator(qs, 25).get_page(request.GET.get("page"))

    return render(
        request,
        "dashboard/duty_list.html",
        {
            "page": page,
            "selected_date": selected_date,
            "include_inactive": include_inactive,
            "total_count": total_count,
            "active_count": active_count,
        },
    )

@manager_required
def duty_create(request):
    DutyAssignment = DutyAssignmentModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    initial = {}
    q_date = (request.GET.get("date") or "").strip()
    if q_date:
        try:
            initial["date"] = datetime.strptime(q_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    if request.method == "POST":
        form = DutyAssignmentForm(request.POST, school=school)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.school = school
            obj.save()
            _invalidate_display_cache(school)
            messages.success(request, "تم إضافة تكليف الإشراف/المناوبة.")
            return redirect("dashboard:duty_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = DutyAssignmentForm(initial=initial, school=school)

    return render(request, "dashboard/duty_form.html", {"form": form, "title": "إضافة تكليف"})

@manager_required
def duty_edit(request, pk: int):
    DutyAssignment = DutyAssignmentModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    obj = get_object_or_404(DutyAssignment, pk=pk, school=school)

    if request.method == "POST":
        form = DutyAssignmentForm(request.POST, instance=obj, school=school)
        if form.is_valid():
            updated = form.save(commit=False)
            updated.school = school
            updated.save()
            _invalidate_display_cache(school)
            messages.success(request, "تم حفظ التعديلات.")
            return redirect("dashboard:duty_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = DutyAssignmentForm(instance=obj, school=school)

    return render(request, "dashboard/duty_form.html", {"form": form, "title": "تعديل تكليف"})

@manager_required
@require_POST
def duty_delete(request, pk: int):
    DutyAssignment = DutyAssignmentModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    obj = get_object_or_404(DutyAssignment, pk=pk, school=school)
    obj.delete()
    _invalidate_display_cache(school)
    messages.success(request, "تم الحذف.")
    return redirect("dashboard:duty_list")

@manager_required
def duty_teacher_search(request):
    """JSON: اقتراح أسماء المعلمين للبحث الديناميكي داخل نموذج الإشراف/المناوبة."""
    Teacher = TeacherModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return JsonResponse({"results": [], "error": "no_active_school"}, status=403)

    q = (request.GET.get("q") or "").strip()
    # Reduce noise: don't hit DB for empty/1-char queries
    if len(q) < 2:
        return JsonResponse({"results": []})

    school_id = getattr(school, "id", None)
    cache_key = f"duty:teacher_search:{school_id}:{q.lower()}"
    try:
        cached = cache.get(cache_key)
        if isinstance(cached, list):
            return JsonResponse({"results": cached})
    except Exception:
        pass

    qs = Teacher.objects.filter(school=school)
    if q:
        qs = qs.filter(name__icontains=q)

    names = list(qs.order_by("name").values_list("name", flat=True)[:10])
    try:
        cache.set(cache_key, names, timeout=60)
    except Exception:
        pass
    return JsonResponse({"results": names})
