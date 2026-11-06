"""Platform administration: schools, users, employees and reports.

Access is gated by the system-permission decorators; the tenant boundary
itself lives in ``dashboard.middleware.SupportDashboardOnlyMiddleware``.
"""

from __future__ import annotations

from .view_helpers import *  # noqa: F401,F403  (shared view layer)


@system_permission_required("dashboard.view")
def system_admin_dashboard(request):
    School = SchoolModel()
    DisplayScreen = DisplayScreenModel()
    today = timezone.localdate()
    now = timezone.now()
    expiring_until = today + timedelta(days=30)
    global_query = (request.GET.get("q") or "").strip()
    can_view_schools = has_system_permission(request.user, "schools.view")
    can_view_users = has_system_permission(request.user, "users.view")
    can_view_subscriptions = has_system_permission(request.user, "subscriptions.view")

    school_count = School.objects.count()
    active_schools_count = School.objects.filter(is_active=True).count()
    inactive_schools_count = school_count - active_schools_count
    user_count = UserModel.objects.count()
    managers_count = UserModel.objects.filter(is_staff=False, is_superuser=False).count()
    unassigned_users_count = (
        UserModel.objects.filter(is_staff=False, is_superuser=False)
        .filter(Q(profile__isnull=True) | Q(profile__schools__isnull=True))
        .distinct()
        .count()
    )

    total_screens_count = DisplayScreen.objects.filter(is_active=True).count()
    active_since = now - timedelta(seconds=120)
    live_screens = DisplayScreen.objects.filter(is_active=True, last_seen__gte=active_since)
    if _model_has_field(DisplayScreen, "bound_device_id"):
        live_screens = live_screens.exclude(bound_device_id__isnull=True).exclude(bound_device_id="")
    live_screens_count = live_screens.count()

    SubModel = _get_subscription_model()
    subs_count = SubModel.objects.count() if SubModel is not None else 0

    active_subs = 0
    expiring_subs_count = 0
    expired_subs_count = 0
    revenue = 0
    active_qs = None
    expiring_subscriptions = []

    if SubModel is not None:
        active_qs = SubModel.objects.all()
        if _model_has_field(SubModel, "status"):
            active_qs = active_qs.filter(status="active")
        elif _model_has_field(SubModel, "is_active"):
            active_qs = active_qs.filter(is_active=True)
        # A subscription that has not started yet is not live revenue; the
        # reports page and the schools list both exclude it, so this overview
        # must too, otherwise the same school is counted differently per page.
        start_field = _admin_model_field_name(SubModel, "starts_at", "start_date")
        if start_field:
            active_qs = active_qs.filter(**{f"{start_field}__lte": today})
        end_field = _admin_model_field_name(SubModel, "ends_at", "end_date")
        if end_field:
            active_qs = active_qs.filter(Q(**{f"{end_field}__isnull": True}) | Q(**{f"{end_field}__gte": today}))
        active_subs = active_qs.count()
        revenue = active_qs.aggregate(total=Sum("plan__price"))["total"] or 0
        if end_field:
            expiring_qs = active_qs.filter(
                **{
                    f"{end_field}__isnull": False,
                    f"{end_field}__gte": today,
                    f"{end_field}__lte": expiring_until,
                }
            )
            expired_subs_count = SubModel.objects.filter(**{f"{end_field}__lt": today}).count()
            expiring_subs_count = expiring_qs.count()
            try:
                expiring_subscriptions = list(
                    expiring_qs.select_related("school", "plan").order_by(end_field)[:6]
                )
            except Exception:
                expiring_subscriptions = list(expiring_qs.order_by(end_field)[:6])

    active_school_ids = set()
    if active_qs is not None and _model_has_field(SubModel, "school"):
        active_school_ids = set(active_qs.values_list("school_id", flat=True))
    schools_without_subscription_count = max(school_count - len(active_school_ids), 0)

    free_trials_count = 0
    trial_conversion_rate = 0
    converted_trials_count = 0
    average_screens_per_school = round(total_screens_count / school_count, 2) if school_count else 0
    incomplete_payments_count = 0
    cancellation_reasons = []
    most_requested_plan = None
    if SubModel is not None:
        try:
            trial_qs = SubModel.objects.filter(plan__price=0)
            free_trials_count = trial_qs.count()
            trial_school_ids = set(trial_qs.values_list("school_id", flat=True))
            paid_school_ids = set(
                SubModel.objects.filter(plan__price__gt=0)
                .values_list("school_id", flat=True)
            )
            converted_trials_count = len(trial_school_ids & paid_school_ids)
            trial_conversion_rate = (
                round(converted_trials_count * 100 / len(trial_school_ids), 1)
                if trial_school_ids
                else 0
            )
            incomplete_payments_count += SubModel.objects.filter(status="pending").count()
            if _model_has_field(SubModel, "closure_reason"):
                reason_labels = dict(getattr(SubModel, "CLOSURE_REASON_CHOICES", ()))
                # A trial retired because the school upgraded is not churn, and
                # counting it here would quietly inflate the loss picture.
                reason_rows = (
                    SubModel.objects.filter(status__in=("cancelled", "expired"))
                    .exclude(closure_reason="upgraded")
                    .values("closure_reason")
                    .annotate(total=Count("id"))
                    .order_by("-total")
                )
                cancellation_reasons = [
                    {
                        "key": row["closure_reason"] or "unknown",
                        "label": reason_labels.get(row["closure_reason"], "لم يُسجل السبب"),
                        "total": row["total"],
                    }
                    for row in reason_rows
                ]
        except Exception:
            logger.exception("Failed to calculate subscription funnel metrics")

    Req = _get_subscription_request_model()
    open_requests = 0
    recent_requests = []
    if Req is not None:
        try:
            open_requests = Req.objects.filter(status__in=["submitted", "under_review"]).count()
            recent_requests = list(
                Req.objects.select_related("school", "plan")
                .filter(status__in=["submitted", "under_review"])
                .order_by("-created_at", "-id")[:5]
            )
        except Exception:
            open_requests = 0
            recent_requests = []
        try:
            most_requested_plan = (
                Req.objects.filter(plan__isnull=False)
                .values("plan__name")
                .annotate(total=Count("id"))
                .order_by("-total", "plan__name")
                .first()
            )
            incomplete_payments_count += Req.objects.filter(
                status__in=["submitted", "under_review"]
            ).count()
        except Exception:
            pass

    try:
        from subscriptions.models import SubscriptionScreenAddon

        incomplete_payments_count += SubscriptionScreenAddon.objects.filter(status="pending").count()
    except Exception:
        pass

    recent_schools = list(
        School.objects.annotate(
            managers_total=Count(
                "users",
                filter=Q(
                    users__user__is_staff=False,
                    users__user__is_superuser=False,
                ),
                distinct=True,
            ),
            screens_total=Count("screens", distinct=True),
        ).order_by("-created_at", "-id")[:5]
    )

    search_results = {"schools": [], "users": [], "subscriptions": []}
    if global_query and can_view_schools:
        search_results["schools"] = list(
            School.objects.filter(
                Q(name__icontains=global_query) | Q(slug__icontains=global_query)
            ).order_by("name")[:6]
        )
    if global_query and can_view_users:
        user_search_qs = UserModel.objects.all()
        if not getattr(request.user, "is_superuser", False):
            user_search_qs = user_search_qs.filter(is_staff=False, is_superuser=False)
        search_results["users"] = list(
            user_search_qs.filter(
                Q(username__icontains=global_query)
                | Q(email__icontains=global_query)
                | Q(first_name__icontains=global_query)
                | Q(last_name__icontains=global_query)
                | Q(profile__schools__name__icontains=global_query)
            )
            .distinct()
            .order_by("username")[:6]
        )
    if global_query and can_view_subscriptions and SubModel is not None:
        try:
            search_results["subscriptions"] = list(
                SubModel.objects.select_related("school", "plan")
                .filter(
                    Q(school__name__icontains=global_query)
                    | Q(plan__name__icontains=global_query)
                )
                .order_by("-id")[:6]
            )
        except Exception:
            search_results["subscriptions"] = []

    return render(
        request,
        "admin/dashboard.html",
        {
            "schools_count": school_count,
            "active_schools_count": active_schools_count,
            "inactive_schools_count": inactive_schools_count,
            "users_count": user_count,
            "managers_count": managers_count,
            "unassigned_users_count": unassigned_users_count,
            "total_screens_count": total_screens_count,
            "live_screens_count": live_screens_count,
            "subs_count": subs_count,
            "active_subs": active_subs,
            "subscriptions_count": active_subs,
            "expiring_subs_count": expiring_subs_count,
            "expired_subs_count": expired_subs_count,
            "schools_without_subscription_count": schools_without_subscription_count,
            "expiring_subscriptions": expiring_subscriptions,
            "revenue": revenue,
            "open_subscription_requests": open_requests,
            "recent_requests": recent_requests,
            "recent_schools": recent_schools,
            "free_trials_count": free_trials_count,
            "converted_trials_count": converted_trials_count,
            "trial_conversion_rate": trial_conversion_rate,
            "most_requested_plan": most_requested_plan,
            "average_screens_per_school": average_screens_per_school,
            "incomplete_payments_count": incomplete_payments_count,
            "cancellation_reasons": cancellation_reasons,
            "global_query": global_query,
            "search_results": search_results,
        },
    )

@system_permission_required("schools.view")
def system_schools_list(request):
    School = SchoolModel()
    DisplayScreen = DisplayScreenModel()
    SubModel = _get_subscription_model()
    today = timezone.localdate()

    q = (request.GET.get("q") or "").strip()
    state = (request.GET.get("state") or "").strip()
    screen_state = (request.GET.get("screen") or "").strip()
    plan_id = (request.GET.get("plan") or "").strip()
    subscription_state = (request.GET.get("subscription") or "").strip()

    total_schools_count = School.objects.count()
    active_schools_count = School.objects.filter(is_active=True).count()
    inactive_schools_count = total_schools_count - active_schools_count

    # School ids that hold a subscription which is active *today*.  The admin
    # overview counts "schools without an active subscription" from the same
    # set, so both screens must agree on the definition.
    subscribed_school_ids: set[int] = set()
    expiring_school_ids: set[int] = set()
    if SubModel is not None:
        try:
            live_subs = SubModel.objects.filter(status="active", starts_at__lte=today).filter(
                Q(ends_at__isnull=True) | Q(ends_at__gte=today)
            )
            subscribed_school_ids = set(live_subs.values_list("school_id", flat=True))
            expiring_school_ids = set(
                live_subs.filter(
                    ends_at__isnull=False, ends_at__lte=today + timedelta(days=30)
                ).values_list("school_id", flat=True)
            )
        except Exception:
            logger.exception("Failed to resolve school subscription coverage")
    schools_without_subscription_count = max(total_schools_count - len(subscribed_school_ids), 0)

    schools = School.objects.annotate(
        managers_count=Count(
            "users",
            filter=Q(
                users__user__is_staff=False,
                users__user__is_superuser=False,
            ),
            distinct=True,
        ),
        screens_count=Count("screens", distinct=True),
    )

    if q:
        schools = schools.filter(
            Q(name__icontains=q)
            | Q(slug__icontains=q)
            | Q(users__user__username__icontains=q)
            | Q(users__user__email__icontains=q)
        ).distinct()

    if state == "active":
        schools = schools.filter(is_active=True)
    elif state == "inactive":
        schools = schools.filter(is_active=False)

    if plan_id and plan_id.isdigit() and SubModel is not None:
        schools = schools.filter(subscriptions__plan_id=int(plan_id)).distinct()

    if subscription_state == "none":
        schools = schools.exclude(pk__in=subscribed_school_ids)
    elif subscription_state == "active":
        schools = schools.filter(pk__in=subscribed_school_ids)
    elif subscription_state == "expiring":
        schools = schools.filter(pk__in=expiring_school_ids)

    schools = _admin_order_by_existing(School, schools, "-created_at", "-id")

    # attach live stats
    try:
        schools = schools.select_related("schedule_settings")
    except Exception:
        pass

    active_window_seconds = 120
    active_since = timezone.now() - timedelta(seconds=active_window_seconds)
    screens_qs = DisplayScreen.objects.all()

    if _model_has_field(DisplayScreen, "is_active"):
        screens_qs = screens_qs.filter(is_active=True)
    last_seen_field = _admin_model_field_name(DisplayScreen, "last_seen_at", "last_seen")
    if last_seen_field:
        screens_qs = screens_qs.filter(**{f"{last_seen_field}__gte": active_since})
    if _model_has_field(DisplayScreen, "bound_device_id"):
        screens_qs = screens_qs.exclude(bound_device_id__isnull=True).exclude(bound_device_id="")

    active_counts = {
        row["school_id"]: row["c"]
        for row in screens_qs.values("school_id").annotate(c=Count("id"))
        if row.get("school_id")
    }
    online_school_ids = set(active_counts)

    if screen_state == "online":
        schools = schools.filter(pk__in=online_school_ids)
    elif screen_state == "offline":
        schools = schools.filter(screens_count__gt=0).exclude(pk__in=online_school_ids)
    elif screen_state == "none":
        schools = schools.filter(screens_count=0)

    if SubModel is not None:
        try:
            sub_qs = SubModel.objects.select_related("plan").order_by("-starts_at", "-id")
            schools = schools.prefetch_related(
                Prefetch("subscriptions", queryset=sub_qs, to_attr="admin_subscriptions")
            )
        except Exception:
            pass

    paginator = Paginator(schools, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    for s in page_obj.object_list:
        setattr(s, "active_screens_now", int(active_counts.get(s.id, 0) or 0))
        settings_obj = getattr(s, "schedule_settings", None)
        setattr(s, "refresh_interval_sec", getattr(settings_obj, "refresh_interval_sec", None))
        current_subscription = None
        subscriptions = getattr(s, "admin_subscriptions", [])
        for candidate in subscriptions:
            end_value = getattr(candidate, "ends_at", None)
            if getattr(candidate, "status", "") == "active" and (not end_value or end_value >= today):
                current_subscription = candidate
                break
        if current_subscription is None and subscriptions:
            current_subscription = subscriptions[0]
        setattr(s, "current_subscription", current_subscription)
        setattr(s, "current_plan", getattr(current_subscription, "plan", None))
        setattr(s, "subscription_end", getattr(current_subscription, "ends_at", None))

    return render(
        request,
        "admin/schools_list.html",
        {
            "schools": page_obj.object_list,
            "page_obj": page_obj,
            "q": q,
            "state": state,
            "screen_state": screen_state,
            "plan_id": plan_id,
            "subscription_state": subscription_state,
            "plans": SubscriptionPlan.objects.filter(is_active=True).order_by("sort_order", "price"),
            "total_schools_count": total_schools_count,
            "active_schools_count": active_schools_count,
            "inactive_schools_count": inactive_schools_count,
            "schools_without_subscription_count": schools_without_subscription_count,
            "filtered_schools_count": paginator.count,
            "active_window_seconds": active_window_seconds,
        },
    )

@system_permission_required("schools.manage")
def system_school_create(request):
    FormCls = _admin_school_form_class()

    if request.method == "POST":
        form = FormCls(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, "تمت إضافة المدرسة بنجاح.")
            return redirect("dashboard:system_schools_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = FormCls()

    return render(request, "admin/school_form.html", {"form": form, "title": "إضافة مدرسة"})

@system_permission_required("schools.manage")
def system_school_edit(request, pk: int):
    School = SchoolModel()
    FormCls = _admin_school_form_class()

    school = get_object_or_404(School, pk=pk)
    if request.method == "POST":
        form = FormCls(request.POST, request.FILES, instance=school)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تحديث بيانات المدرسة.")
            return redirect("dashboard:system_schools_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = FormCls(instance=school)

    return render(request, "admin/school_form.html", {"form": form, "title": "تعديل مدرسة", "edit": True})

@system_permission_required("schools.manage")
def system_school_delete(request, pk: int):
    School = SchoolModel()
    school = get_object_or_404(School, pk=pk)
    if request.method == "POST":
        school_name = school.name
        try:
            school.delete()
        except ProtectedError:
            # Payment checkouts reference the school with on_delete=PROTECT, so
            # any school that ever reached a payment gateway cannot be removed.
            # Deactivate it instead of returning a 500 to the operator.
            if not school.display_suspended:
                school.display_suspended = True
                school.save(update_fields=["display_suspended"])
            messages.warning(
                request,
                f"تعذر حذف «{school_name}» لارتباطها بسجلات مالية محفوظة؛ تم إيقاف العرض إداريًا مع الحفاظ على الاشتراك والبيانات. يمكنك استئنافه من تعديل المدرسة.",
            )
        else:
            messages.warning(request, f"تم حذف المدرسة: {school_name}")
        return redirect("dashboard:system_schools_list")
    return render(request, "admin/school_confirm_delete.html", {"school": school})

@system_permission_required("users.view")
def system_users_list(request):
    q = (request.GET.get("q") or "").strip()
    state = (request.GET.get("state") or "").strip()
    role = (request.GET.get("role") or "manager").strip()
    school_id = (request.GET.get("school") or "").strip()

    # Delegated staff may manage customer accounts, never enumerate platform
    # owners/employees through a crafted role query parameter.
    if not getattr(request.user, "is_superuser", False) and role not in {"manager", "unassigned"}:
        role = "manager"

    qs = UserModel.objects.all().order_by("-id")

    managers_base = UserModel.objects.filter(is_staff=False, is_superuser=False)
    managers_count = managers_base.count()
    inactive_managers_count = managers_base.filter(is_active=False).count()
    unassigned_managers_count = (
        managers_base.filter(Q(profile__isnull=True) | Q(profile__schools__isnull=True))
        .distinct()
        .count()
    )

    try:
        qs = qs.select_related("profile", "profile__active_school").prefetch_related("profile__schools")
    except FieldError:
        try:
            qs = qs.select_related("profile")
        except FieldError:
            pass

    if q:
        filters = Q()
        for key in ("username", "email", "first_name", "last_name"):
            try:
                UserModel._meta.get_field(key)
                filters |= Q(**{f"{key}__icontains": q})
            except Exception:
                continue

        try:
            filters |= Q(profile__active_school__name__icontains=q) | Q(profile__schools__name__icontains=q)
        except Exception:
            pass

        qs = qs.filter(filters).distinct()

    if role == "manager":
        qs = qs.filter(is_staff=False, is_superuser=False)
    elif role == "system":
        qs = qs.filter(Q(is_staff=True) | Q(is_superuser=True) | Q(groups__name="Support")).distinct()
    elif role == "unassigned":
        qs = qs.filter(is_staff=False, is_superuser=False).filter(
            Q(profile__isnull=True) | Q(profile__schools__isnull=True)
        ).distinct()

    if state == "active":
        qs = qs.filter(is_active=True)
    elif state == "inactive":
        qs = qs.filter(is_active=False)

    if school_id and school_id.isdigit():
        qs = qs.filter(profile__schools__id=int(school_id)).distinct()

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "admin/users_list.html",
        {
            "page_obj": page_obj,
            "q": q,
            "state": state,
            "role": role,
            "school_id": school_id,
            "schools_options": SchoolModel().objects.order_by("name"),
            "managers_count": managers_count,
            "inactive_managers_count": inactive_managers_count,
            "unassigned_managers_count": unassigned_managers_count,
            "filtered_users_count": paginator.count,
        },
    )

@superuser_required
def system_employees_list(request):
    """List platform employees; employee administration remains owner-only."""
    q = (request.GET.get("q") or "").strip()

    qs = (
        UserModel.objects.filter(
            Q(is_superuser=True)
            | Q(system_employee_profile__isnull=False)
            | Q(groups__name="Support")
        )
        .distinct()
        .order_by("-id")
    )
    qs = qs.select_related("system_employee_profile").prefetch_related("groups")

    if q:
        filters = Q()
        for key in ("username", "email", "first_name", "last_name"):
            try:
                UserModel._meta.get_field(key)
                filters |= Q(**{f"{key}__icontains": q})
            except Exception:
                continue
        filters |= Q(groups__name__icontains=q)
        filters |= Q(system_employee_profile__role__icontains=q)
        qs = qs.filter(filters).distinct()

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    for employee_user in page_obj.object_list:
        if employee_user.is_superuser:
            employee_user.platform_role_label = "مالك المنصة"
            employee_user.platform_permission_labels = ["وصول كامل"]
            employee_user.platform_permission_count = len(PERMISSION_LABELS)
            continue
        try:
            employee_profile = employee_user.system_employee_profile
            permission_keys = normalize_permission_keys(employee_profile.permission_keys)
            employee_user.platform_role_label = role_label(employee_profile.role)
        except SystemEmployeeProfile.DoesNotExist:
            permission_keys = list(ROLE_PRESETS["support"]["permissions"])
            employee_user.platform_role_label = "الدعم الفني (حساب قديم)"
        employee_user.platform_permission_labels = [
            PERMISSION_LABELS[key] for key in permission_keys if key in PERMISSION_LABELS
        ]
        employee_user.platform_permission_count = len(employee_user.platform_permission_labels)

    owners_count = UserModel.objects.filter(is_superuser=True).count()
    employees_count = SystemEmployeeProfile.objects.count()
    inactive_count = (
        UserModel.objects.filter(
            Q(is_superuser=True)
            | Q(system_employee_profile__isnull=False)
            | Q(groups__name="Support")
        )
        .filter(is_active=False)
        .distinct()
        .count()
    )

    return render(
        request,
        "admin/employees_list.html",
        {
            "page_obj": page_obj,
            "q": q,
            "owners_count": owners_count,
            "employees_count": employees_count,
            "inactive_count": inactive_count,
            "filtered_count": paginator.count,
        },
    )

def _employee_form_context(form, *, employee_user=None):
    if form.is_bound:
        selected_permission_keys = normalize_permission_keys(form.data.getlist("permissions"))
    else:
        selected_permission_keys = normalize_permission_keys(form.fields["permissions"].initial)
    role_presets = {
        role: {
            "label": details["label"],
            "description": details["description"],
            "permissions": normalize_permission_keys(details["permissions"]),
        }
        for role, details in ROLE_PRESETS.items()
    }
    return {
        "form": form,
        "employee_user": employee_user,
        "is_edit": employee_user is not None,
        "permission_groups": grouped_permission_definitions(),
        "selected_permission_keys": selected_permission_keys,
        "role_presets": role_presets,
    }

@superuser_required
def system_employee_create(request):
    if request.method == "POST":
        form = SystemEmployeeCreateForm(request.POST)
        if form.is_valid():
            employee = form.save(created_by=request.user)
            messages.success(
                request,
                f"تم إنشاء الموظف {employee.get_full_name() or employee.username} وتطبيق صلاحياته.",
            )
            return redirect("dashboard:system_employees_list")

        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = SystemEmployeeCreateForm()

    return render(request, "admin/employee_form.html", _employee_form_context(form))

@superuser_required
def system_employee_edit(request, pk: int):
    employee_user = get_object_or_404(UserModel, pk=pk, is_superuser=False)
    is_employee = SystemEmployeeProfile.objects.filter(user=employee_user).exists()
    is_legacy_support = employee_user.groups.filter(name="Support").exists()
    if not is_employee and not is_legacy_support:
        raise PermissionDenied("هذا الحساب ليس موظف منصة.")

    if request.method == "POST":
        form = SystemEmployeeUpdateForm(request.POST, instance=employee_user)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تحديث بيانات الموظف وصلاحياته فورًا.")
            return redirect("dashboard:system_employees_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = SystemEmployeeUpdateForm(instance=employee_user)

    return render(
        request,
        "admin/employee_form.html",
        _employee_form_context(form, employee_user=employee_user),
    )

@system_permission_required("users.manage")
def system_user_create(request):
    if request.method == "POST":
        form = SystemUserCreateForm(request.POST)
        if not getattr(request.user, "is_superuser", False):
            form.fields.pop("is_staff", None)
            form.fields.pop("is_superuser", None)
        if form.is_valid():
            form.save()
            messages.success(request, "تم إنشاء المستخدم بنجاح.")
            return redirect("dashboard:system_users_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        initial = {}
        requested_school_id = (request.GET.get("school") or "").strip()
        if requested_school_id.isdigit():
            requested_school = SchoolModel().objects.filter(pk=int(requested_school_id)).first()
            if requested_school is not None:
                initial = {
                    "schools": [requested_school.pk],
                    "active_school": requested_school.pk,
                }
        form = SystemUserCreateForm(initial=initial)
        if not getattr(request.user, "is_superuser", False):
            form.fields.pop("is_staff", None)
            form.fields.pop("is_superuser", None)

    return render(request, "admin/user_edit.html", {"form": form, "is_create": True})

@system_permission_required("users.manage")
def system_user_edit(request, pk: int):
    user = get_object_or_404(UserModel, pk=pk)

    # Delegated employees manage school accounts only, never platform identities.
    if not getattr(request.user, "is_superuser", False):
        is_platform_identity = bool(
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
            or SystemEmployeeProfile.objects.filter(user=user).exists()
            or user.groups.filter(name="Support").exists()
        )
        if is_platform_identity:
            raise PermissionDenied("إدارة موظفي المنصة محصورة بمالك النظام.")

    if request.method == "POST":
        form = SystemUserUpdateForm(request.POST, instance=user)
        if not getattr(request.user, "is_superuser", False):
            form.fields.pop("is_staff", None)
            form.fields.pop("is_superuser", None)
        if form.is_valid():
            from django.core.exceptions import ValidationError
            try:
                form.save()
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, "تم تحديث بيانات المستخدم بنجاح.")
                return redirect("dashboard:system_users_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = SystemUserUpdateForm(instance=user)
        if not getattr(request.user, "is_superuser", False):
            form.fields.pop("is_staff", None)
            form.fields.pop("is_superuser", None)

    return render(request, "admin/user_edit.html", {"form": form, "is_create": False, "user_obj": user})

@system_permission_required("users.manage")
def system_user_delete(request, pk: int):
    user = get_object_or_404(UserModel, pk=pk)

    if not getattr(request.user, "is_superuser", False):
        is_platform_identity = bool(
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
            or SystemEmployeeProfile.objects.filter(user=user).exists()
            or user.groups.filter(name="Support").exists()
        )
        if is_platform_identity:
            raise PermissionDenied("إدارة موظفي المنصة محصورة بمالك النظام.")

    # Locking yourself out, or removing the last owner, leaves the platform
    # without an administrator; the template hides these buttons but a crafted
    # request must be refused server-side too.
    if user.pk == request.user.pk:
        messages.error(request, "لا يمكنك حذف حسابك الخاص من لوحة إدارة النظام.")
        return redirect("dashboard:system_users_list")

    if getattr(user, "is_superuser", False):
        remaining_owners = (
            UserModel.objects.filter(is_superuser=True, is_active=True).exclude(pk=user.pk).count()
        )
        if remaining_owners == 0:
            messages.error(request, "لا يمكن حذف آخر حساب مالك للمنصة.")
            return redirect("dashboard:system_users_list")

    if request.method == "POST":
        with transaction.atomic():
            from core.owner_safety import protect_owner_change
            from django.core.exceptions import ValidationError
            try:
                protect_owner_change(user.pk)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
                return redirect("dashboard:system_users_list")
            user = UserModel.objects.select_for_update().get(pk=user.pk)
            username = getattr(user, "username", str(user.pk))
            try:
                user.delete()
            except ProtectedError:
                # Audit trails (emergency alerts, payment operations) keep a PROTECT
                # reference to their author.  Deactivate rather than crash.
                if user.is_active:
                    user.is_active = False
                    user.save(update_fields=["is_active"])
                messages.warning(
                    request,
                    f"تعذر حذف {username} لارتباطه بسجلات تشغيلية محفوظة؛ تم إيقاف الحساب بدلاً من حذفه.",
                )
            else:
                messages.success(request, f"تم حذف المستخدم {username}.")
            return redirect("dashboard:system_users_list")
    return render(request, "admin/user_delete_confirm.html", {"user_obj": user})

@system_permission_required("reports.view")
def system_reports(request):
    School = SchoolModel()
    SubModel = _get_subscription_model()

    schools_count = School.objects.count()
    total_revenue = 0
    active_subscriptions_count = 0
    total_subscriptions_count = 0
    avg_revenue_per_active = 0
    revenue_by_plan = []
    schools_distribution = []
    monthly_growth = []

    today = timezone.localdate()

    def _recent_months(count: int = 6):
        out = []
        for offset in range(count - 1, -1, -1):
            year = today.year
            month = today.month - offset
            while month <= 0:
                month += 12
                year -= 1
            out.append((year, month))
        return out

    month_keys = _recent_months(6)

    if SubModel is not None:
        all_subs = SubModel.objects.all()
        total_subscriptions_count = all_subs.count()

        active_subs = all_subs
        if _model_has_field(SubModel, "status"):
            active_subs = active_subs.filter(status="active")
        elif _model_has_field(SubModel, "is_active"):
            active_subs = active_subs.filter(is_active=True)

        if _model_has_field(SubModel, "starts_at"):
            active_subs = active_subs.filter(starts_at__lte=today)
        elif _model_has_field(SubModel, "start_date"):
            active_subs = active_subs.filter(start_date__lte=today)

        if _model_has_field(SubModel, "ends_at"):
            active_subs = active_subs.filter(Q(ends_at__isnull=True) | Q(ends_at__gte=today))
        elif _model_has_field(SubModel, "end_date"):
            active_subs = active_subs.filter(Q(end_date__isnull=True) | Q(end_date__gte=today))

        active_subscriptions_count = active_subs.count()
        total_revenue = active_subs.aggregate(total=Sum("plan__price")).get("total") or 0
        if active_subscriptions_count:
            avg_revenue_per_active = (total_revenue or 0) / active_subscriptions_count

        revenue_rows = (
            active_subs.values("plan__name")
            .annotate(total_revenue=Sum("plan__price"), count=Count("id"))
            .order_by("-total_revenue")
        )
        for row in revenue_rows:
            plan_name = (row.get("plan__name") or "غير محدد").strip()
            row_revenue = row.get("total_revenue") or 0
            share_percent = (float(row_revenue) / float(total_revenue) * 100) if total_revenue else 0
            revenue_by_plan.append(
                {
                    "plan_name": plan_name,
                    "count": int(row.get("count") or 0),
                    "total_revenue": row_revenue,
                    "share_percent": round(share_percent, 1),
                }
            )

        month_field = None
        for candidate in ("starts_at", "start_date", "created_at"):
            if _model_has_field(SubModel, candidate):
                month_field = candidate
                break

        monthly_map = {}
        if month_field:
            try:
                monthly_rows = (
                    all_subs.filter(**{f"{month_field}__isnull": False})
                    .annotate(month=TruncMonth(month_field))
                    .values("month")
                    .annotate(count=Count("id"))
                    .order_by("month")
                )
                for row in monthly_rows:
                    month_value = row.get("month")
                    if not month_value:
                        continue
                    monthly_map[(int(month_value.year), int(month_value.month))] = int(row.get("count") or 0)
            except Exception:
                monthly_map = {}

        monthly_growth = [
            {
                "label": f"{month:02d}/{year}",
                "count": int(monthly_map.get((year, month), 0)),
            }
            for year, month in month_keys
        ]

    if not monthly_growth:
        monthly_growth = [{"label": f"{month:02d}/{year}", "count": 0} for year, month in month_keys]

    if _model_has_field(School, "school_type"):
        try:
            type_field = School._meta.get_field("school_type")
            choices = {str(k): str(v) for k, v in (type_field.choices or [])}
            rows = School.objects.values("school_type").annotate(count=Count("id")).order_by("-count")
            for row in rows:
                school_type = row.get("school_type")
                school_type_str = "" if school_type is None else str(school_type)
                label = choices.get(school_type_str) or school_type_str or "غير محدد"
                schools_distribution.append(
                    {
                        "label": label,
                        "count": int(row.get("count") or 0),
                    }
                )
        except Exception:
            schools_distribution = []

    if not schools_distribution and schools_count:
        schools_distribution = [{"label": "إجمالي المدارس", "count": int(schools_count)}]

    schools_distribution_total = sum(int(item.get("count") or 0) for item in schools_distribution)
    for item in schools_distribution:
        count = int(item.get("count") or 0)
        item["percent"] = round((count / schools_distribution_total) * 100, 1) if schools_distribution_total else 0

    max_monthly_subscriptions = 0
    if monthly_growth:
        max_monthly_subscriptions = max(int(item.get("count") or 0) for item in monthly_growth)

    context = {
        "schools_count": schools_count,
        "total_revenue": total_revenue,
        "active_subscriptions_count": active_subscriptions_count,
        "total_subscriptions_count": total_subscriptions_count,
        "avg_revenue_per_active": avg_revenue_per_active,
        "revenue_by_plan": revenue_by_plan,
        "monthly_growth": monthly_growth,
        "max_monthly_subscriptions": max_monthly_subscriptions,
        "schools_distribution": schools_distribution,
        "schools_distribution_total": schools_distribution_total,
    }
    return render(request, "admin/reports.html", context)
