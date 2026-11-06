"""School-manager dashboard views: schedule, content, data and timetables.

Platform administration lives in :mod:`dashboard.views_system` and billing in
:mod:`dashboard.views_billing`. Both are re-exported below so that every
existing ``dashboard.views.<name>`` reference keeps working unchanged.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .view_helpers import *  # noqa: F401,F403  (shared view layer)

from .views_system import (  # noqa: F401  (re-exported for urls.py and callers)
    _employee_form_context,
    system_admin_dashboard,
    system_employee_create,
    system_employee_edit,
    system_employees_list,
    system_reports,
    system_school_create,
    system_school_delete,
    system_school_edit,
    system_schools_list,
    system_user_create,
    system_user_delete,
    system_user_edit,
    system_users_list,
)

from .views_discounts import (  # noqa: F401  (re-exported for urls.py and callers)
    system_discount_create,
    system_discount_delete,
    system_discount_edit,
    system_discount_toggle,
    system_discounts_list,
)

from .views_billing import (  # noqa: F401  (re-exported for urls.py and callers)
    my_subscription,
    subscription_invoice_view,
    system_plan_create,
    system_plan_delete,
    system_plan_edit,
    system_plan_toggle,
    system_plans_list,
    system_screen_addon_create,
    system_screen_addon_delete,
    system_screen_addon_edit,
    system_screen_addons_list,
    system_subscription_create,
    system_subscription_delete,
    system_subscription_edit,
    system_subscription_invoice_view,
    system_subscription_request_detail,
    system_subscription_requests_list,
    system_subscriptions_list,
)

from .views_payments import (  # noqa: F401  (re-exported for urls.py and callers)
    system_payment_detail,
    system_payments_list,
)

from .views_schedule import (  # noqa: F401  (re-exported for urls.py and callers)
    closure_delete,
    closures_list,
    day_autofill,
    day_clear,
    day_edit,
    day_reindex,
    day_toggle,
    days_list,
    lesson_create,
    lesson_delete,
    lesson_edit,
    lessons_list,
    timetable_day_view,
    timetable_export_csv,
    timetable_week_view,
)

from .views_content import (  # noqa: F401  (re-exported for urls.py and callers)
    _emergency_allowed_schools,
    _occasion_suggestions_for,
    _shell_base_template,
    ann_create,
    ann_delete,
    ann_edit,
    ann_list,
    duty_create,
    duty_delete,
    duty_edit,
    duty_list,
    duty_teacher_search,
    emergency_alert_cancel,
    emergency_alert_create,
    emergency_alert_delete,
    emergency_alert_list,
    exc_create,
    exc_delete,
    exc_edit,
    exc_list,
    occasion_templates,
    standby_create,
    standby_delete,
    standby_list,
)


def demo_login(request):
    if not getattr(dj_settings, "DEBUG", False):
        from django.http import Http404
        raise Http404

    School = SchoolModel()
    DEMO_ID = "demo_user"
    DEMO_SCHOOL_SLUG = "demo-school"

    demo_school, _ = School.objects.get_or_create(
        slug=DEMO_SCHOOL_SLUG,
        defaults={"name": "مدرسة تجريبية", "is_active": True},
    )

    lookup = {}
    defaults: dict[str, Any] = {"is_active": True}
    if _model_has_field(UserModel, "username"):
        lookup["username"] = DEMO_ID
        defaults.update({"first_name": "حساب", "last_name": "تجريبي", "email": "demo@example.com"})
    elif _model_has_field(UserModel, "phone"):
        lookup["phone"] = "0500000000"
        defaults.update({"name": "حساب تجريبي"})
    else:
        lookup["email"] = "demo@example.com"
        defaults.update({"first_name": "Demo", "last_name": "User"})

    demo_user, created = UserModel.objects.get_or_create(**lookup, defaults=defaults)
    if created:
        demo_user.set_password(get_random_string(12))
        demo_user.save()

    profile = _get_or_create_profile(demo_user)
    if demo_school not in profile.schools.all():
        profile.schools.add(demo_school)
    if profile.active_school != demo_school:
        profile.active_school = demo_school
        profile.save(update_fields=["active_school"])

    login(request, demo_user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, "تم تسجيل دخولك بحساب تجريبي. البيانات هنا لأغراض العرض فقط.")
    return redirect("dashboard:index")

@manager_required
def index(request):
    Announcement = AnnouncementModel()
    Excellence = ExcellenceModel()
    StandbyAssignment = StandbyAssignmentModel()
    SchoolSettings = SchoolSettingsModel()
    DisplayScreen = DisplayScreenModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    if school is None:
        return render(request, "dashboard/no_school.html")

    now = timezone.now()
    billing_today = timezone.localdate(now)
    settings_obj = SchoolSettings.objects.filter(school=school).first()
    try:
        school_timezone = ZoneInfo(getattr(settings_obj, "timezone_name", None) or "Asia/Riyadh")
    except (ZoneInfoNotFoundError, ValueError):
        school_timezone = ZoneInfo("Asia/Riyadh")
    today = timezone.localdate(now, school_timezone)
    screens_qs = DisplayScreen.objects.filter(school=school).order_by("id")
    screens = list(screens_qs)
    live_screens_count = live_display_count(screens, now=now)
    active_screens_count = sum(1 for screen in screens if bool(getattr(screen, "is_active", False)))

    primary_screen = next(
        (screen for screen in screens if bool(getattr(screen, "is_active", False))),
        screens[0] if screens else None,
    )
    primary_screen_is_live = bool(primary_screen and display_is_live(primary_screen, now=now))
    primary_screen_is_bound = bool(
        primary_screen and (getattr(primary_screen, "bound_device_id", "") or "").strip()
    )
    primary_screen_last_seen = latest_display_presence(primary_screen) if primary_screen else None

    if primary_screen is None:
        screen_hub = {
            "exists": False,
            "is_fleet": False,
            "status": "لا توجد شاشة بعد",
            "tone": "new",
            "detail": "أنشئ شاشة واحدة لتحصل على رابط تشغيل جاهز للتلفاز أو الشاشة الذكية.",
            "primary_label": "أنشئ أول شاشة",
            "primary_url": reverse("dashboard:screen_list"),
            "short_code": "",
        }
    elif len(screens) > 1:
        # مدرسة بعدة شاشات: البطل يلخّص الأسطول بدل أن يتحدث بلسان شاشة واحدة.
        offline_count = max(active_screens_count - live_screens_count, 0)
        unbound_count = sum(
            1
            for screen in screens
            if bool(getattr(screen, "is_active", False))
            and not (getattr(screen, "bound_device_id", "") or "").strip()
        )
        if active_screens_count == 0:
            status = "جميع الشاشات موقوفة"
            tone = "paused"
            detail = "فعّل شاشاتك من صفحة إدارة الشاشات حتى تستقبل الجدول والمحتوى من جديد."
        elif live_screens_count == active_screens_count:
            status = f"كل الشاشات متصلة ({live_screens_count}/{active_screens_count})"
            tone = "live"
            detail = "جميع شاشات المدرسة تعمل وتستقبل تحديثات الجدول والتنبيهات تلقائيًا."
        elif unbound_count == active_screens_count:
            status = f"بانتظار الربط ({unbound_count})"
            tone = "waiting"
            detail = "افتح رابط كل شاشة على تلفازها مرة واحدة لإكمال الربط وبدء العرض."
        elif live_screens_count == 0:
            status = f"لا توجد شاشة متصلة (0/{active_screens_count})"
            tone = "offline"
            detail = "لم يصل اتصال حديث من أي شاشة. تحقق من تشغيل الأجهزة والإنترنت في المدرسة."
        else:
            status = f"{live_screens_count} من {active_screens_count} متصلة"
            tone = "waiting"
            detail = (
                f"{offline_count} شاشة نشطة بلا اتصال حديث. افتح إدارة الشاشات لمعرفة أيها يحتاج تدخلاً."
            )

        screen_hub = {
            "exists": True,
            "is_fleet": True,
            "name": f"{len(screens)} شاشة في {school.name}",
            "status": status,
            "tone": tone,
            "detail": detail,
            "primary_label": "إدارة الشاشات",
            "primary_url": reverse("dashboard:screen_list"),
            "short_code": "",
        }
    else:
        primary_screen_url = (
            reverse("website:short_display", args=[primary_screen.short_code])
            if getattr(primary_screen, "short_code", "")
            else reverse("dashboard:screen_list")
        )
        if not bool(getattr(primary_screen, "is_active", False)):
            status = "الشاشة موقوفة"
            tone = "paused"
            detail = "فعّل الشاشة من صفحة إدارة الشاشات حتى تستقبل الجدول والمحتوى من جديد."
        elif primary_screen_is_live:
            status = "متصلة الآن"
            tone = "live"
            detail = "الشاشة تعمل وتستقبل تحديثات الجدول والتنبيهات تلقائيًا."
        elif primary_screen_is_bound and primary_screen_last_seen:
            status = "غير متصلة حاليًا"
            tone = "offline"
            detail = "الشاشة مرتبطة بجهاز، لكن لم يصل منها اتصال حديث. تحقق من تشغيل التلفاز والإنترنت."
        elif primary_screen_is_bound:
            status = "مرتبطة وجاهزة"
            tone = "waiting"
            detail = "تم ربط الجهاز بنجاح، وسيظهر الاتصال اللحظي عند وصول أول نبض تشغيل."
        else:
            status = "بانتظار أول تشغيل"
            tone = "waiting"
            detail = "افتح رابط الشاشة على التلفاز مرة واحدة لإكمال الربط وبدء العرض."

        screen_hub = {
            "exists": True,
            "is_fleet": False,
            "name": primary_screen.name,
            "status": status,
            "tone": tone,
            "detail": detail,
            "primary_label": "فتح شاشة المدرسة",
            "primary_url": primary_screen_url,
            "short_code": getattr(primary_screen, "short_code", "") or "",
        }

    stats = {
        "ann_count": Announcement.objects.filter(school=school).count(),
        "exc_count": Excellence.objects.filter(school=school).count(),
        "standby_today": StandbyAssignment.objects.filter(school=school, date=today).count(),
        "screens_count": len(screens),
        "active_screens_count": active_screens_count,
        "live_screens_count": live_screens_count,
    }

    day_snapshot = {}
    current_state = {}
    current_period = None
    next_period = None
    if settings_obj:
        try:
            day_snapshot = build_day_snapshot(settings_obj, now=now)
            current_state = day_snapshot.get("state") or {}
            current_period = day_snapshot.get("current_period")
            next_period = day_snapshot.get("next_period")
        except Exception:
            logger.exception("dashboard index day snapshot failed school_id=%s", getattr(school, "id", None))
            day_snapshot = {}
            current_state = {}

    today_weekday = today.isoweekday()
    today_weekday_label = WEEKDAY_MAP.get(today_weekday, "")
    schedule_revision = int(getattr(settings_obj, "schedule_revision", 0) or 0) if settings_obj else 0

    SubModel = _get_subscription_model()
    subscription = None
    if SubModel is not None:
        school_subscriptions = SubModel.objects.filter(school=school).order_by("-starts_at", "-id")
        subscription = school_subscriptions.filter(
            Q(ends_at__isnull=True) | Q(ends_at__gte=billing_today),
            status="active", starts_at__lte=billing_today,
        ).first() or school_subscriptions.first()

    onboarding_context = _build_dashboard_onboarding_context(request, school, settings_obj=settings_obj)

    return render(
        request,
        "dashboard/index.html",
        {
            "occasion_suggestions": _occasion_suggestions_for(school, today=today),
            "stats": stats,
            "settings": settings_obj,
            "subscription": subscription,
            "today": today,
            "today_weekday_label": today_weekday_label,
            "day_snapshot": day_snapshot,
            "current_state": current_state,
            "current_period": current_period,
            "next_period": next_period,
            "schedule_revision": schedule_revision,
            "screen_hub": screen_hub,
            **onboarding_context,
        },
    )

@manager_required
def help_getting_started(request):
    SchoolSettings = SchoolSettingsModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    onboarding_context = _build_dashboard_onboarding_context(request, school, settings_obj=settings_obj)

    try:
        profile = request.user.profile
        if profile.needs_onboarding:
            profile.needs_onboarding = False
            profile.save(update_fields=["needs_onboarding"])
    except Exception:
        pass

    return render(
        request,
        "dashboard/help_getting_started.html",
        {
            "school": school,
            "settings": settings_obj,
            **onboarding_context,
        },
    )

@login_required
def select_school(request):
    profile = _get_or_create_profile(request.user)

    if getattr(request.user, "is_superuser", False):
        return redirect("dashboard:system_admin_dashboard")

    schools_qs = getattr(profile, "schools", None)
    if schools_qs is None:
        return render(request, "dashboard/no_school.html")

    schools = profile.schools.order_by("name", "id")
    if not schools.exists():
        return render(request, "dashboard/no_school.html")

    if request.method == "POST":
        sid = (request.POST.get("school_id") or "").strip()
        try:
            school = profile.schools.get(pk=int(sid))
        except Exception:
            messages.error(request, "المدرسة غير موجودة أو ليست ضمن صلاحياتك.")
            return redirect("dashboard:select_school")

        profile.active_school = school
        profile.save(update_fields=["active_school"])
        messages.success(request, f"تم اختيار المدرسة النشطة: {school.name}")

        return redirect(_safe_next_url(request, default_name="dashboard:index"))

    return render(
        request,
        "dashboard/select_school.html",
        {"schools": schools, "active_school_id": getattr(profile, "active_school_id", None)},
    )

@login_required
def schools_overview(request):
    """نظرة واحدة على كل مدارس المدير: الاشتراك والشاشات وحالة الاتصال.

    هذه الصفحة مستثناة من بوابة الاشتراك عمدًا: مدير لديه مدرسة متعثرة يجب أن
    يبقى قادرًا على رؤية بقية مدارسه والانتقال إليها بنقرة، بدل أن تحبسه
    المدرسة المتعثرة في صفحة الدفع.
    """

    if is_system_staff_user(request.user) and not getattr(request.user, "is_superuser", False):
        return redirect("dashboard:system_admin_dashboard")

    DisplayScreen = DisplayScreenModel()
    SubModel = _get_subscription_model()
    RequestModel = _get_subscription_request_model()

    profile = _get_or_create_profile(request.user)
    schools = list(profile.schools.order_by("name", "id"))
    if not schools:
        return render(request, "dashboard/no_school.html")

    school_ids = [school.pk for school in schools]
    today = timezone.localdate()
    now = timezone.now()

    screens_by_school: dict[int, list] = {school_id: [] for school_id in school_ids}
    for screen in DisplayScreen.objects.filter(school_id__in=school_ids):
        screens_by_school.setdefault(screen.school_id, []).append(screen)

    # أحدث اشتراك ساري لكل مدرسة. استعلام واحد بدل استعلام لكل مدرسة.
    subscription_by_school: dict[int, Any] = {}
    if SubModel is not None:
        current_qs = (
            SubModel.objects.filter(
                school_id__in=school_ids,
                status="active",
                starts_at__lte=today,
            )
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=today))
            .select_related("plan")
            .order_by("school_id", "-starts_at", "-id")
        )
        for sub in current_qs:
            subscription_by_school.setdefault(sub.school_id, sub)

    pending_request_school_ids: set[int] = set()
    if RequestModel is not None:
        try:
            pending_request_school_ids = set(
                RequestModel.objects.filter(
                    school_id__in=school_ids,
                    status__in=["submitted", "under_review"],
                ).values_list("school_id", flat=True)
            )
        except Exception:
            logger.exception("schools_overview pending_requests_failed user_id=%s", request.user.pk)

    active_school_id = getattr(profile, "active_school_id", None)
    cards = []
    totals = {"schools": len(schools), "screens": 0, "live": 0, "needs_attention": 0}

    for school in schools:
        screens = screens_by_school.get(school.pk, [])
        active_screens = [s for s in screens if bool(getattr(s, "is_active", False))]
        live_count = live_display_count(active_screens, now=now)
        subscription = subscription_by_school.get(school.pk)

        days_left = None
        if subscription is not None:
            ends_at = getattr(subscription, "ends_at", None)
            if ends_at:
                days_left = max(0, (ends_at - today).days)

        if subscription is None:
            status_key = "expired"
            status_label = "بانتظار الاشتراك" if not screens else "الاشتراك منتهي"
            status_hint = "لن تعرض شاشات هذه المدرسة أي محتوى حتى يتم تفعيل الاشتراك."
        elif days_left is not None and days_left <= 14:
            status_key = "expiring"
            status_label = f"ينتهي خلال {days_left} يوم"
            status_hint = "جدّد الاشتراك قبل انتهائه حتى لا تتوقف الشاشات."
        else:
            status_key = "active"
            status_label = "اشتراك ساري"
            status_hint = (
                f"متبقٍ {days_left} يوم" if days_left is not None else "اشتراك غير محدد المدة"
            )

        has_pending_request = school.pk in pending_request_school_ids
        offline_count = len(active_screens) - live_count
        needs_attention = status_key != "active" or (active_screens and live_count == 0)

        totals["screens"] += len(screens)
        totals["live"] += live_count
        if needs_attention:
            totals["needs_attention"] += 1

        logo_url = ""
        try:
            logo_url = school.logo.url if school.logo else ""
        except Exception:
            logo_url = ""

        cards.append(
            {
                "school": school,
                "logo_url": logo_url,
                "is_active_school": school.pk == active_school_id,
                "status_key": status_key,
                "status_label": status_label,
                "status_hint": status_hint,
                "plan_name": getattr(getattr(subscription, "plan", None), "name", ""),
                "days_left": days_left,
                "has_pending_request": has_pending_request,
                "screens_total": len(screens),
                "screens_active": len(active_screens),
                "screens_live": live_count,
                "screens_offline": max(offline_count, 0),
                "needs_attention": needs_attention,
                # وجهات التبديل: تُرسل عبر POST من القالب حتى يبقى تغيير
                # المدرسة النشطة عملية محمية بـ CSRF.
                "dashboard_next": reverse("dashboard:index"),
                "screens_next": reverse("dashboard:screen_list"),
                "subscription_next": reverse("dashboard:my_subscription"),
            }
        )

    # المدارس التي تحتاج تدخلاً أولاً؛ بعدها ترتيب أبجدي ثابت.
    cards.sort(key=lambda card: (not card["needs_attention"], card["school"].name))

    return render(
        request,
        "dashboard/schools_overview.html",
        {"cards": cards, "totals": totals},
    )

def _make_self_service_school_slug(school_name: str) -> str:
    School = SchoolModel()
    ascii_words = re.findall(r"[a-zA-Z0-9]+", (school_name or "").lower())
    base = "school"
    if ascii_words:
        base = "-".join(ascii_words[:4])[:38].strip("-") or "school"

    for _attempt in range(40):
        slug = f"{base}-{get_random_string(6).lower()}"
        if not School.objects.filter(slug=slug).exists():
            return slug
    return f"school-{get_random_string(16).lower()}"

@manager_required
def add_school(request):
    """Let a school manager create and pay for an additional tenant."""

    form = SelfServiceSchoolCreateForm(
        request.POST or None,
        request.FILES or None,
        user=request.user,
    )

    if request.method == "POST" and form.is_valid():
        School = SchoolModel()
        UserProfile = UserProfileModel()
        SchoolSettings = SchoolSettingsModel()
        plan = form.cleaned_data["plan"]

        with transaction.atomic():
            profile = UserProfile.objects.select_for_update().get(user=request.user)
            school = School.objects.create(
                name=form.cleaned_data["name"],
                slug=_make_self_service_school_slug(form.cleaned_data["name"]),
                logo=form.cleaned_data.get("logo"),
                school_type=form.cleaned_data["school_type"],
                is_active=False,
            )
            profile.schools.add(school)
            profile.active_school = school
            profile.save(update_fields=["active_school"])

            theme = "emerald" if school.school_type == "boys" else "rose"
            SchoolSettings.objects.create(
                school=school,
                name=school.name,
                theme=theme,
                timezone_name="Asia/Riyadh",
                refresh_interval_sec=30,
            )

        request.school = school
        request.session.pop("sub_blocked_once", None)
        # التبديل ضروري لأن صفحة الدفع وبوابات الدفع تعمل على المدرسة النشطة.
        # نوضّح ذلك صراحةً ونشير إلى الطريق الفوري للعودة لبقية المدارس.
        messages.success(
            request,
            f"تمت إضافة {school.name} وربطها بحسابك، وأصبحت هي المدرسة النشطة لإتمام الدفع. "
            f"يمكنك العودة إلى بقية مدارسك في أي وقت من صفحة «كل مدارسي».",
        )
        checkout_url = reverse("dashboard:my_subscription")
        checkout_url += "?" + urlencode(
            {"plan": plan.code, "source": "new_school"}
        )
        return redirect(f"{checkout_url}#renewal-section")

    available_plan_cards = plan_cards(form.fields["plan"].queryset)
    selected_screen_count = str(
        form["screen_count"].value()
        or form.initial.get("screen_count")
        or ""
    )
    selected_plan_id = str(
        form["plan"].value()
        or form.initial.get("plan")
        or ""
    )
    for details in available_plan_cards:
        details["selected"] = str(details.get("id") or "") == selected_plan_id
        details["screen_value"] = (
            "unlimited"
            if details.get("max_screens") is None
            else str(details["max_screens"])
        )

    return render(
        request,
        "dashboard/add_school.html",
        {
            "form": form,
            "available_plan_cards": available_plan_cards,
            "selected_screen_count": selected_screen_count,
        },
    )

@never_cache
@manager_required
def school_settings(request):
    SchoolSettings = SchoolSettingsModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    obj, _created = SchoolSettings.objects.get_or_create(
        school=school,
        defaults={"name": school.name},
    )

    # Preview: use latest active screen short_code if exists
    preview_url = None
    try:
        from core.models import DisplayScreen  # local import

        scr = (
            DisplayScreen.objects
            .filter(school=school, is_active=True)
            .exclude(short_code__isnull=True)
            .exclude(short_code__exact="")
            .order_by("-id")
            .first()
        )
        if scr:
            # ?preview=1 keeps this read-only: the manager's browser must never
            # take the screen's single device slot away from the TV.
            preview_url = f"/s/{scr.short_code}/?preview=1"
    except Exception:
        preview_url = None

    if request.method == "POST":
        form = SchoolSettingsForm(
            request.POST,
            request.FILES,
            instance=obj,
            user=request.user,
            mode="account",
        )
        if form.is_valid():
            form.save()
            
            # ✅ Invalidate display cache + WS broadcast so TV updates immediately
            _invalidate_display_cache(obj.school or school, force_bump=True)
            
            messages.success(request, "تم حفظ إعدادات المدرسة.")
            return redirect("dashboard:settings")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = SchoolSettingsForm(instance=obj, user=request.user, mode="account")

    initial_settings_tab = "runtime"
    if form.errors:
        error_fields = set(form.errors.keys())
        runtime_fields = {
            "screen_offline_alerts_enabled",
            "screen_offline_threshold_minutes",
            "screen_offline_school_hours_only",
            "screen_offline_grace_minutes",
            "screen_offline_cooldown_minutes",
            "screen_offline_max_alerts_per_day",
            "screen_recovery_notice_enabled",
        }
        contact_fields = {"email", "mobile"}
        if error_fields & contact_fields:
            initial_settings_tab = "contact"
        elif error_fields & runtime_fields:
            initial_settings_tab = "runtime"

    return render(
        request,
        "dashboard/settings.html",
        {
            "form": form,
            "display_preview_url": preview_url,
            "school": school,
            "two_factor_enabled": get_enabled_config(request.user) is not None,
            "initial_settings_tab": initial_settings_tab,
            "show_display_settings": False,
            "show_account_settings": True,
        },
    )

@manager_required
def school_data(request):
    SchoolSettings = SchoolSettingsModel()
    Subject = SubjectModel()
    Teacher = TeacherModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()

    classes = _classes_qs_from_settings(settings_obj).order_by("name")
    subjects = Subject.objects.filter(school=school).order_by("name")
    teachers = Teacher.objects.filter(school=school).order_by("name")

    return render(
        request,
        "dashboard/school_data.html",
        {"classes": classes, "subjects": subjects, "teachers": teachers},
    )

@manager_required
def school_data_import_template(request):
    response = HttpResponse(
        build_template_bytes(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="school-data-template.xlsx"'
    return response

@manager_required
def school_data_import(request):
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    parsed = None
    import_token = ""
    impact = None
    completed_import = None
    undo_ttl_seconds = 30 * 60
    latest_undo_key = f"school-data-import-latest:{request.user.pk}:{school.pk}"
    if request.method == "POST" and request.POST.get("action") == "preview":
        uploaded = request.FILES.get("excel_file")
        if not uploaded:
            messages.error(request, "اختر ملف Excel أولًا.")
        elif getattr(uploaded, "size", 0) > 5 * 1024 * 1024:
            messages.error(request, "حجم الملف يتجاوز 5 م.ب.")
        elif not (getattr(uploaded, "name", "") or "").lower().endswith(".xlsx"):
            messages.error(request, "صيغة الملف غير مدعومة. ارفع ملفًا بامتداد xlsx.")
        else:
            parsed = parse_workbook(uploaded)
            parsed["strategy"] = request.POST.get("strategy", "merge")
            if parsed["strategy"] not in ("merge", "replace"):
                parsed["errors"].append("اختر دمج التعديلات أو استبدال الجدول كاملًا.")
            if parsed["strategy"] == "replace" and not parsed["errors"]:
                from .document_import import parse_draft, prepare_draft
                draft = prepare_draft(school=school, extraction={
                    "entities": parsed["entities"], "warnings": [],
                    "rows": [{**row, "day": row["day_label"], "period_index": str(row["period_index"])} for row in parsed["rows"]],
                })
                parsed = parse_draft(school=school, draft=draft)
                parsed["strategy"] = "replace"
            parsed["file"] = {
                "name": (getattr(uploaded, "name", "") or "school-data.xlsx")[:180],
                "size": int(getattr(uploaded, "size", 0) or 0),
            }
            if not parsed["errors"]:
                impact = build_import_impact(school=school, parsed=parsed)
                if parsed["strategy"] == "replace":
                    parsed["replacement_fingerprint"] = impact["state_fingerprint"]
            import_token = get_random_string(40)
            cache.set(
                f"school-data-import:{request.user.pk}:{school.pk}:{import_token}",
                parsed,
                timeout=30 * 60,
            )
            if parsed["errors"]:
                messages.warning(request, "اكتملت المعاينة مع أخطاء. صحح الملف ثم ارفعه مجددًا.")
            else:
                messages.success(request, "المعاينة سليمة. يمكنك تنفيذ الاستيراد الآن.")
    elif request.method == "POST" and request.POST.get("action") == "import":
        import_token = (request.POST.get("import_token") or "").strip()
        key = f"school-data-import:{request.user.pk}:{school.pk}:{import_token}"
        parsed = cache.get(key)
        if not parsed:
            messages.error(request, "انتهت صلاحية المعاينة. ارفع الملف مرة أخرى.")
        elif parsed.get("errors"):
            messages.error(request, "لا يمكن استيراد ملف يحتوي أخطاء.")
        elif parsed.get("requires_document_review") and request.POST.get("review_confirmed") != "yes":
            messages.error(request, "أكد مراجعة بيانات الملف قبل اعتماد الاستيراد.")
        elif parsed.get("strategy") == "replace" and request.POST.get("replace_confirmed") != "yes":
            messages.error(request, "أكد مراجعة قائمة التغييرات والحذف واستبدال جدول المدرسة كاملًا قبل الاعتماد.")
        else:
            if parsed.get("requires_document_review") and parsed.get("school_timing_fingerprint"):
                from .document_import import school_timing_fingerprint
                if parsed["school_timing_fingerprint"] != school_timing_fingerprint(school):
                    cache.delete(key)
                    messages.error(request, "تغير توقيت حصص المدرسة بعد المعاينة. تحقق من التوقيت الجديد قبل الحفظ.")
                    return redirect(f"{reverse('dashboard:school_document_import')}?draft={parsed['document_draft_token']}")
            lock_key = f"school-data-import-running:{school.pk}"
            lock_token = get_random_string(40)
            if not cache.add(lock_key, lock_token, timeout=120):
                messages.warning(request, "يجري حفظ استيراد آخر لهذه المدرسة. انتظر قليلًا ثم أعد المحاولة.")
                return redirect("dashboard:school_data_import")
            try:
                # A second request may have read the preview before the first
                # import completed. Recheck under the school-wide lease.
                if not cache.get(key):
                    messages.warning(request, "تم استخدام المعاينة أو انتهت صلاحيتها. أعد المعاينة قبل الاستيراد.")
                    return redirect("dashboard:school_data_import")
                result = apply_import(school=school, parsed=parsed)
                cache.delete(key)
            except ValueError as exc:
                messages.error(request, str(exc))
                if parsed.get("strategy") == "replace":
                    cache.delete(key)
                if parsed.get("requires_document_review") and parsed.get("document_draft_token"):
                    return redirect(
                        f"{reverse('dashboard:school_document_import')}?draft={parsed['document_draft_token']}"
                    )
                return redirect("dashboard:school_data_import")
            finally:
                if cache.get(lock_key) == lock_token:
                    cache.delete(lock_key)
            undo_token = get_random_string(40)
            undo_payload = {
                "token": undo_token,
                "result": {key: value for key, value in result.items() if key != "undo"},
                "undo": result["undo"],
                "file": parsed.get("file") or {},
                "created_at": timezone.now().isoformat(),
            }
            cache.set(
                f"school-data-import-undo:{request.user.pk}:{school.pk}:{undo_token}",
                undo_payload,
                timeout=undo_ttl_seconds,
            )
            cache.set(latest_undo_key, undo_token, timeout=undo_ttl_seconds)
            _invalidate_display_cache(school, force_bump=True)
            messages.success(
                request,
                (
                    f"تم الاستيراد: {result['lessons']} حصة، "
                    f"نُقلت {result.get('moved_lessons', 0)} وحُذفت {result.get('deleted_lessons', 0)} حصة، "
                    f"{result['classes']} فصل، {result['subjects']} مادة، "
                    f"{result['teachers']} معلم/ـة."
                ),
            )
            return redirect(
                f"{reverse('dashboard:school_data_import')}?completed={undo_token}"
            )
    elif request.method == "POST" and request.POST.get("action") == "undo":
        undo_token = (request.POST.get("undo_token") or "").strip()
        undo_key = f"school-data-import-undo:{request.user.pk}:{school.pk}:{undo_token}"
        undo_payload = cache.get(undo_key)
        if not undo_payload:
            messages.error(request, "انتهت مهلة التراجع أو تم استخدامه مسبقًا.")
        elif cache.get(latest_undo_key) != undo_token:
            messages.error(request, "لا يمكن التراجع عن استيراد قديم بعد تنفيذ استيراد أحدث.")
        else:
            try:
                undo_result = undo_import(school=school, undo=undo_payload["undo"])
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("dashboard:school_data_import")
            cache.delete(undo_key)
            cache.delete(latest_undo_key)
            _invalidate_display_cache(school, force_bump=True)
            messages.success(
                request,
                (
                    "تم التراجع عن آخر استيراد بنجاح: "
                    f"استُعيد {undo_result['restored']} سجل وحُذف "
                    f"{undo_result['deleted']} سجل أنشأه الاستيراد."
                ),
            )
        return redirect("dashboard:school_data_import")

    completed_token = (request.GET.get("completed") or "").strip()
    if completed_token and cache.get(latest_undo_key) == completed_token:
        completed_import = cache.get(
            f"school-data-import-undo:{request.user.pk}:{school.pk}:{completed_token}"
        )
    if parsed and impact is None and not parsed.get("errors"):
        impact = build_import_impact(school=school, parsed=parsed)
    return render(
        request,
        "dashboard/school_data_import.html",
        {
            "parsed": parsed,
            "preview_rows": (parsed or {}).get("rows", [])[:200],
            "hidden_preview_rows": max(0, len((parsed or {}).get("rows", [])) - 200),
            "preview_issues": (parsed or {}).get("issues", [])[:200],
            "hidden_preview_issues": max(0, len((parsed or {}).get("issues", [])) - 200),
            "impact": impact,
            "import_token": import_token,
            "completed_import": completed_import,
            "undo_minutes": undo_ttl_seconds // 60,
        },
    )

# Setting up a school means typing dozens of class, subject and teacher names.
# One-at-a-time was a reload per name; the field now takes a pasted list too, so
# a column copied out of a spreadsheet lands in a single request.
_SCHOOL_DATA_NAME_SEPARATORS = re.compile(r"[\r\n,،;؛]+")
_SCHOOL_DATA_MAX_NAMES = 200
_SCHOOL_DATA_MAX_NAME_LENGTH = 120


def _parse_names(raw: str) -> tuple[list[str], bool]:
    """Split a pasted block into clean names.

    Returns the names (deduplicated, original order preserved) and whether the
    input had to be truncated, so the caller can say so instead of silently
    dropping the tail of a long paste.
    """
    seen: set[str] = set()
    names: list[str] = []
    for chunk in _SCHOOL_DATA_NAME_SEPARATORS.split(raw or ""):
        name = " ".join(chunk.split())[:_SCHOOL_DATA_MAX_NAME_LENGTH]
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= _SCHOOL_DATA_MAX_NAMES:
            return names, True
    return names, False


def _report_added(request, created: int, skipped: int, truncated: bool, *, singular: str, plural: str) -> None:
    if created == 1:
        messages.success(request, f"تمت إضافة {singular}.")
    elif created:
        messages.success(request, f"تمت إضافة {created} {plural}.")

    if skipped:
        messages.info(request, f"{skipped} من الأسماء موجودة مسبقًا ولم تُضف مرة أخرى.")
    if truncated:
        messages.warning(
            request,
            f"تمت معالجة أول {_SCHOOL_DATA_MAX_NAMES} اسم فقط. أضف الباقي في دفعة تالية.",
        )


def _validate_school_data_name_lengths(request, names, model) -> bool:
    maximum = model._meta.get_field("name").max_length
    if any(len(name) > maximum for name in names):
        messages.error(request, f"أحد الأسماء يتجاوز الحد الأقصى ({maximum} حرفًا). لم تُضف أي أسماء؛ صححها ثم حاول مجددًا.")
        return False
    return True


@manager_required
@require_POST
def add_class(request):
    SchoolSettings = SchoolSettingsModel()
    SchoolClass = SchoolClassModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    names, truncated = _parse_names(request.POST.get("name"))
    if not names:
        messages.error(request, "فضلاً أدخل اسم الفصل.")
        return redirect("dashboard:school_data")
    if not _validate_school_data_name_lengths(request, names, SchoolClass):
        return redirect("dashboard:school_data")

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if not settings_obj:
        messages.error(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:settings")

    created = 0
    for name in names:
        _, was_created = SchoolClass.objects.get_or_create(settings=settings_obj, name=name)
        created += int(was_created)

    _report_added(request, created, len(names) - created, truncated, singular="الفصل", plural="فصلاً")
    return redirect(f"{reverse('dashboard:school_data')}?added=classes")

@manager_required
@require_POST
def add_subject(request):
    Subject = SubjectModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    names, truncated = _parse_names(request.POST.get("name"))
    if not names:
        messages.error(request, "فضلاً أدخل اسم المادة.")
        return redirect("dashboard:school_data")
    if not _validate_school_data_name_lengths(request, names, Subject):
        return redirect("dashboard:school_data")

    created = 0
    for name in names:
        _, was_created = Subject.objects.get_or_create(school=school, name=name)
        created += int(was_created)

    _report_added(request, created, len(names) - created, truncated, singular="المادة", plural="مادة")
    return redirect(f"{reverse('dashboard:school_data')}?added=subjects")

@manager_required
@require_POST
def add_teacher(request):
    Teacher = TeacherModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    names, truncated = _parse_names(request.POST.get("name"))
    if not names:
        messages.error(request, "فضلاً أدخل اسم المعلم/ـة.")
        return redirect("dashboard:school_data")
    if not _validate_school_data_name_lengths(request, names, Teacher):
        return redirect("dashboard:school_data")

    created = 0
    for name in names:
        _, was_created = Teacher.objects.get_or_create(school=school, name=name)
        created += int(was_created)

    _report_added(request, created, len(names) - created, truncated, singular="المعلم/ـة", plural="معلمًا/ـة")
    return redirect(f"{reverse('dashboard:school_data')}?added=teachers")

@manager_required
@require_POST
def delete_class(request, pk: int):
    SchoolClass = SchoolClassModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    deleted, _ = SchoolClass.objects.filter(pk=pk, settings__school=school).delete()
    if deleted:
        messages.success(request, "تم حذف الفصل.")
    else:
        messages.error(request, "الفصل غير موجود.")
    return redirect("dashboard:school_data")

@manager_required
@require_POST
def delete_subject(request, pk: int):
    Subject = SubjectModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    deleted, _ = Subject.objects.filter(pk=pk, school=school).delete()
    if deleted:
        messages.success(request, "تم حذف المادة.")
    else:
        messages.error(request, "المادة غير موجودة.")
    return redirect("dashboard:school_data")

@manager_required
@require_POST
def delete_teacher(request, pk: int):
    Teacher = TeacherModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    deleted, _ = Teacher.objects.filter(pk=pk, school=school).delete()
    if deleted:
        messages.success(request, "تم حذف المعلم/ـة.")
    else:
        messages.error(request, "المعلم/ـة غير موجود.")
    return redirect("dashboard:school_data")

@login_required
def switch_school(request, school_id=None):
    profile = _get_or_create_profile(request.user)
    School = SchoolModel()
    selected_school_id = school_id or request.POST.get("school_id") or request.GET.get("school_id")

    try:
        selected_school_id = int(selected_school_id)
    except (TypeError, ValueError):
        messages.error(request, "اختر مدرسة صحيحة للتبديل إليها.")
        return redirect(_safe_next_url(request, default_name="dashboard:index"))

    try:
        if getattr(request.user, "is_superuser", False):
            school = School.objects.get(pk=selected_school_id)
            if not profile.schools.filter(pk=school.pk).exists():
                profile.schools.add(school)
        else:
            school = profile.schools.get(pk=selected_school_id)
    except School.DoesNotExist:
        messages.error(request, "المدرسة غير موجودة أو ليس لديك صلاحية الوصول إليها.")
        return redirect("dashboard:index")
    except Exception:
        messages.error(request, "المدرسة غير موجودة أو ليس لديك صلاحية الوصول إليها.")
        return redirect("dashboard:index")

    profile.active_school = school
    profile.save(update_fields=["active_school"])
    request.school = school
    request.session.pop("sub_blocked_once", None)
    messages.success(request, f"تم التبديل إلى مدرسة: {school.name}")
    return redirect(_safe_next_url(request, default_name="dashboard:index"))
