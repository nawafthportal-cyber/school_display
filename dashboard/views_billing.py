"""Subscriptions, plans, invoices, screen add-ons and payment requests.

Covers both the platform-side administration of billing and the
school-facing ``my_subscription`` page.
"""

from __future__ import annotations

from .view_helpers import *  # noqa: F401,F403  (shared view layer)

# After the star import on purpose, so the shared view layer can never shadow
# these with something else of the same name.
from decimal import Decimal, InvalidOperation  # noqa: E402


@system_permission_required("subscriptions.view")
def system_subscriptions_list(request):
    SubModel = _get_subscription_model_robust()

    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    plan_id = (request.GET.get("plan") or "").strip()

    today = timezone.localdate()
    expiring_until = today + timedelta(days=30)

    rows = []
    page_obj = None

    active_count = 0
    inactive_count = 0
    expiring_count = 0
    expired_count = 0
    pending_count = 0
    total_count = 0
    active_revenue = 0

    if SubModel is None:
        return render(
            request,
            "admin/subscriptions_list.html",
            {
                "rows": rows,
                "page_obj": page_obj,
                "q": q,
                "status": status,
                "active_count": 0,
                "inactive_count": 0,
                "expiring_count": 0,
                "expired_count": 0,
                "pending_count": 0,
                "total_count": 0,
                "active_revenue": 0,
                "plans": SubscriptionPlan.objects.order_by("sort_order", "price"),
            },
        )

    # ---------- بناء QuerySet مرن حسب الحقول ----------
    qs = SubModel.objects.all()

    # روابط شائعة
    has_school = True
    has_plan = True
    try:
        SubModel._meta.get_field("school")
    except Exception:
        has_school = False
    try:
        SubModel._meta.get_field("plan")
    except Exception:
        has_plan = False

    if has_school:
        try:
            qs = qs.select_related("school")
        except Exception:
            pass
    if has_plan:
        try:
            qs = qs.select_related("plan")
        except Exception:
            pass

    # فلترة البحث
    if q:
        filters = Q()
        if has_school:
            filters |= Q(school__name__icontains=q)
            # email في School غير موجود عندك في core/models.py → لن نفلتر عليه
        if has_plan:
            filters |= Q(plan__name__icontains=q)
        qs = qs.filter(filters).distinct()

    if plan_id and plan_id.isdigit() and has_plan:
        qs = qs.filter(plan_id=int(plan_id))

    # ---------- توحيد منطق "نشط" بين النظامين ----------
    # New subscriptions عادة: starts_at/ends_at + status
    # Legacy core: start_date/end_date + is_active
    def _is_active_obj(sub) -> bool:
        # boolean مباشر لو موجود
        if hasattr(sub, "is_active"):
            try:
                if not bool(sub.is_active):
                    return False
            except Exception:
                pass

        # status لو موجود
        if hasattr(sub, "status"):
            try:
                if str(sub.status) != "active":
                    return False
            except Exception:
                pass

        # ends_at / end_date
        end_val = getattr(sub, "ends_at", None)
        if end_val is None:
            end_val = getattr(sub, "end_date", None)

        if end_val:
            try:
                return end_val >= today
            except Exception:
                return True

        return True

    def _starts_at(sub):
        v = getattr(sub, "starts_at", None)
        if v is None:
            v = getattr(sub, "start_date", None)
        return v

    def _ends_at(sub):
        v = getattr(sub, "ends_at", None)
        if v is None:
            v = getattr(sub, "end_date", None)
        return v

    def _state_obj(sub) -> str:
        raw_status = str(getattr(sub, "status", "") or "").lower()
        if raw_status == "pending":
            return "pending"
        if raw_status == "cancelled":
            return "cancelled"
        end_value = _ends_at(sub)
        if end_value and end_value < today:
            return "expired"
        if _is_active_obj(sub):
            if end_value and today <= end_value <= expiring_until:
                return "expiring"
            return "active"
        return "inactive"

    # ترتيب
    # new: -starts_at, -id | legacy: -start_date, -id
    qs = _admin_order_by_existing(SubModel, qs, "-starts_at", "-start_date", "-id")

    # نحسب الحالة على كامل النتائج قبل الترقيم حتى لا تظهر صفحات فارغة بعد الفلترة.
    # الحالة تُحسب مرة واحدة لكل اشتراك وتُحفظ بجانبه، بدل إعادة حسابها في كل
    # مرحلة (العدادات، الإيراد، الفلترة، ثم بناء الصفوف).
    try:
        all_subscriptions = [(sub, _state_obj(sub)) for sub in qs]
    except Exception:
        all_subscriptions = []

    total_count = len(all_subscriptions)
    state_counts = {
        "active": 0,
        "expiring": 0,
        "expired": 0,
        "pending": 0,
        "cancelled": 0,
        "inactive": 0,
    }
    active_revenue = 0
    for sub, state_key in all_subscriptions:
        state_counts[state_key] = state_counts.get(state_key, 0) + 1
        if state_key in {"active", "expiring"}:
            try:
                active_revenue += getattr(getattr(sub, "plan", None), "price", 0) or 0
            except Exception:
                pass
    active_count = state_counts["active"] + state_counts["expiring"]
    expiring_count = state_counts["expiring"]
    expired_count = state_counts["expired"]
    pending_count = state_counts["pending"]
    inactive_count = total_count - active_count

    if status == "active":
        display_subscriptions = [item for item in all_subscriptions if item[1] in {"active", "expiring"}]
    elif status in {"expiring", "expired", "pending", "cancelled", "inactive"}:
        display_subscriptions = [item for item in all_subscriptions if item[1] == status]
    else:
        display_subscriptions = all_subscriptions

    paginator = Paginator(display_subscriptions, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    # بناء rows جاهزة للقالب
    filtered_rows = []
    for sub, state_key in page_obj.object_list:
        is_active = _is_active_obj(sub)

        school_obj = getattr(sub, "school", None)
        plan_obj = getattr(sub, "plan", None)
        end_value = _ends_at(sub)
        days_left = (end_value - today).days if end_value else None

        filtered_rows.append(
            {
                "id": sub.pk,
                "school_id": getattr(school_obj, "pk", None) if school_obj else None,
                "school_name": getattr(school_obj, "name", "") if school_obj else "—",
                "school_email": getattr(school_obj, "email", "") if school_obj else "",
                "plan_name": getattr(plan_obj, "name", "") if plan_obj else "—",
                "plan_price": getattr(plan_obj, "price", 0) if plan_obj else 0,
                "starts_at": _starts_at(sub),
                "ends_at": end_value,
                "days_left": days_left,
                "days_overdue": abs(days_left) if days_left is not None and days_left < 0 else 0,
                "is_active": is_active,
                "state_key": state_key,
            }
        )

    return render(
        request,
        "admin/subscriptions_list.html",
        {
            "rows": filtered_rows,
            "page_obj": page_obj,
            "q": q,
            "status": status,
            "plan_id": plan_id,
            "plans": SubscriptionPlan.objects.order_by("sort_order", "price"),
            "active_count": active_count,
            "inactive_count": inactive_count,
            "expiring_count": expiring_count,
            "expired_count": expired_count,
            "pending_count": pending_count,
            "total_count": total_count,
            "active_revenue": active_revenue,
        },
    )

@system_permission_required("subscriptions.manage")
def system_subscription_create(request):
    SubModel = _get_subscription_model()
    if SubModel is None:
        messages.error(request, "نظام الاشتراكات غير مثبت.")
        return redirect("dashboard:system_subscriptions_list")

    plan_durations = {p.id: p.duration_days for p in SubscriptionPlan.objects.all().only("id", "duration_days")}
    plan_prices = {p.id: str(p.price) for p in SubscriptionPlan.objects.all().only("id", "price")}

    if request.method == "POST":
        form = SchoolSubscriptionForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            # احسب تاريخ النهاية تلقائيًا من مدة الخطة إذا كانت النهاية غير محددة.
            try:
                if not getattr(obj, "ends_at", None) and getattr(obj, "starts_at", None) and getattr(obj, "plan", None):
                    days = getattr(obj.plan, "duration_days", None)
                    if days is not None:
                        days_int = int(days)
                        if days_int > 0:
                            obj.ends_at = obj.starts_at + timedelta(days=days_int)
            except Exception:
                pass
            obj.save()

            # سجل طريقة الدفع عند إنشاء اشتراك مدفوع يدويًا (لا تشمل الباقة المجانية).
            #
            # عملية الدفع هي أول حلقة في سلسلة: عملية ← فاتورة (بإشارة post_save)
            # ← إدراج الرسالة في طابور البريد ← إرسالها للعميل. لذلك لا يجوز أن
            # يفشل إنشاؤها بصمت: النموذج يضمن وجود طريقة دفع لكل باقة مدفوعة،
            # وأي فشل بعد ذلك يجب أن يراه المسؤول لا أن يُخفى خلف رسالة نجاح.
            billing_failed = False
            plan_obj = getattr(obj, "plan", None)
            method = (form.cleaned_data.get("payment_method") or "").strip()
            try:
                price = Decimal(str(getattr(plan_obj, "price", 0) or 0))
            except (InvalidOperation, TypeError, ValueError):
                price = Decimal("0")

            if plan_obj is not None and method and price > 0:
                from subscriptions.models import SubscriptionPaymentOperation

                try:
                    SubscriptionPaymentOperation.objects.create(
                        school=getattr(obj, "school", None),
                        subscription=obj,
                        plan=plan_obj,
                        amount=price,
                        method=method,
                        source="admin_manual",
                        created_by=request.user,
                    )
                except Exception:
                    billing_failed = True
                    logger.exception(
                        "Failed to record the payment operation for subscription id=%s; "
                        "no invoice will be issued or emailed until this is resolved.",
                        getattr(obj, "id", None),
                    )

            if billing_failed:
                messages.warning(
                    request,
                    "تم إنشاء الاشتراك، لكن تعذّر تسجيل عملية الدفع فلم تُصدر الفاتورة "
                    "ولم تُرسل للعميل. راجع سجل الأخطاء ثم أعد حفظ طريقة الدفع.",
                )
            else:
                messages.success(request, "تم إنشاء الاشتراك بنجاح.")
            return redirect("dashboard:system_subscriptions_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = SchoolSubscriptionForm()

    return render(
        request,
        "admin/subscription_form.html",
        {"form": form, "title": "إضافة اشتراك", "plan_durations": plan_durations, "plan_prices": plan_prices},
    )

@system_permission_required("subscriptions.manage")
def system_subscription_edit(request, pk: int):
    SubModel = _get_subscription_model()
    if SubModel is None:
        messages.error(request, "نظام الاشتراكات غير مثبت.")
        return redirect("dashboard:system_subscriptions_list")

    plan_durations = {p.id: p.duration_days for p in SubscriptionPlan.objects.all().only("id", "duration_days")}
    plan_prices = {p.id: str(p.price) for p in SubscriptionPlan.objects.all().only("id", "price")}

    obj = get_object_or_404(SubModel, pk=pk)

    if request.method == "POST":
        from django.core.exceptions import ValidationError
        from subscriptions.models import SubscriptionInvoice, SubscriptionPaymentOperation
        from subscriptions.invoicing import build_invoice_from_operation

        try:
            with transaction.atomic():
                obj = SubModel.objects.select_for_update().get(pk=pk)
                form = SchoolSubscriptionForm(request.POST, instance=obj)
                if form.is_valid():
                    obj2 = form.save()
                    # Existing payments and invoice snapshots are historical records.
                    if not SubscriptionPaymentOperation.objects.filter(subscription=obj2).exists():
                        method = form.cleaned_data.get("payment_method")
                        if method and obj2.plan.price > 0:
                            op = SubscriptionPaymentOperation.objects.create(
                                school=obj2.school, subscription=obj2, plan=obj2.plan,
                                amount=obj2.plan.price, method=method, source="admin_manual",
                                created_by=request.user, note="Payment recorded via edit",
                            )
                            if not SubscriptionInvoice.objects.filter(operation=op).exists():
                                build_invoice_from_operation(op)
                    messages.success(request, "تم تحديث الاشتراك مع الحفاظ على المدفوعات والفواتير السابقة.")
                    return redirect("dashboard:system_subscriptions_list")
        except ValidationError as exc:
            form.add_error(None, exc)
        except Exception:
            logger.exception("Subscription edit failed subscription_id=%s", pk)
            form.add_error(None, "تعذر إكمال الحفظ؛ لم تتغير بيانات الاشتراك أو سجلاته المالية.")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = SchoolSubscriptionForm(instance=obj)

    return render(
        request,
        "admin/subscription_form.html",
        {"form": form, "title": "تعديل اشتراك", "edit": True, "plan_durations": plan_durations, "plan_prices": plan_prices},
    )

@system_permission_required("subscriptions.manage")
@require_POST
def system_subscription_delete(request, pk: int):
    SubModel = _get_subscription_model()
    if SubModel is None:
        messages.error(request, "نظام الاشتراكات غير مثبت.")
        return redirect("dashboard:system_subscriptions_list")

    obj = get_object_or_404(SubModel, pk=pk)
    try:
        subscription_label = str(obj)
        obj.delete()
    except ProtectedError:
        if getattr(obj, "status", None) != "cancelled":
            obj.status = "cancelled"
            obj.save(update_fields=["status"])
        try:
            from subscriptions.audit import record as record_subscription_audit

            record_subscription_audit(
                "subscription_cancelled",
                subscription=obj,
                request=request,
                summary="تعذر حذف الاشتراك لارتباطه بسجلات مالية؛ تم إلغاؤه بدلاً من الحذف.",
                context={"reason": "protected_delete"},
            )
        except Exception:
            logger.exception("Failed to audit protected subscription delete for %s", getattr(obj, "pk", None))
        messages.warning(
            request,
            "تعذر حذف الاشتراك لارتباطه بفواتير أو سجلات دفع محفوظة؛ تم إلغاؤه بدلاً من الحذف.",
        )
    else:
        messages.warning(request, f"تم حذف الاشتراك «{subscription_label}».")
    return redirect("dashboard:system_subscriptions_list")

@system_permission_required("subscriptions.view")
def system_subscription_invoice_view(request, pk: int):
    """عرض الفاتورة (HTML snapshot) لعمليات الدفع."""
    from django.http import HttpResponse

    from subscriptions.models import SubscriptionInvoice

    invoice = get_object_or_404(SubscriptionInvoice.objects.select_related("school", "subscription", "plan"), pk=pk)
    html = (invoice.html_snapshot or "").strip()

    # كحل احتياطي: إن لم تُحفظ النسخة لأي سبب، نعيد توليدها.
    if not html:
        try:
            from django.template.loader import render_to_string

            from subscriptions.invoicing import _get_seller_info

            html = render_to_string(
                "invoices/subscription_invoice.html",
                {
                    "invoice": invoice,
                    "seller": _get_seller_info(),
                    "school": invoice.school,
                    "subscription": invoice.subscription,
                    "plan": invoice.plan,
                },
            )
            invoice.html_snapshot = html
            invoice.save(update_fields=["html_snapshot"])
        except Exception:
            html = ""

    if not html:
        messages.error(request, "تعذر عرض الفاتورة حاليًا.")
        return redirect("dashboard:system_subscriptions_list")

    return HttpResponse(html, content_type="text/html; charset=utf-8")

@system_permission_required("subscription_requests.view")
def system_subscription_requests_list(request):
    Req = _get_subscription_request_model()
    if Req is None:
        messages.error(request, "نظام طلبات الاشتراك غير مثبت.")
        return redirect("dashboard:system_admin_dashboard")

    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    req_type = (request.GET.get("type") or "").strip()

    try:
        qs = Req.objects.all()
        try:
            qs = qs.select_related("school", "plan", "created_by", "processed_by")
        except Exception:
            pass

        if q:
            qs = qs.filter(
                Q(school__name__icontains=q)
                | Q(school__slug__icontains=q)
                | Q(plan__name__icontains=q)
            )

        counts_qs = qs

        if req_type:
            counts_qs = counts_qs.filter(request_type=req_type)

        open_count = counts_qs.filter(status__in=["submitted", "under_review"]).count()
        approved_count = counts_qs.filter(status="approved").count()
        rejected_count = counts_qs.filter(status="rejected").count()

        if status:
            qs = qs.filter(status=status)
        if req_type:
            qs = qs.filter(request_type=req_type)

        qs = qs.order_by("-created_at", "-id")

        paginator = Paginator(qs, 25)
        page_obj = paginator.get_page(request.GET.get("page") or 1)
    except (ProgrammingError, OperationalError):
        # غالباً: لم يتم تشغيل migrate في بيئة الإنتاج بعد نشر الكود.
        messages.error(request, "قاعدة البيانات غير محدثة (جدول طلبات الاشتراك غير موجود). نفّذ migrate ثم أعد المحاولة.")
        open_count = 0
        approved_count = 0
        rejected_count = 0
        page_obj = Paginator([], 25).get_page(1)

    return render(
        request,
        "admin/subscription_requests_list.html",
        {
            "page_obj": page_obj,
            "q": q,
            "status": status,
            "request_type": req_type,
            "open_count": open_count,
            "approved_count": approved_count,
            "rejected_count": rejected_count,
        },
    )

@system_permission_required("subscription_requests.view")
def system_subscription_request_detail(request, pk: int):
    Req = _get_subscription_request_model()
    if Req is None:
        messages.error(request, "نظام طلبات الاشتراك غير مثبت.")
        return redirect("dashboard:system_admin_dashboard")

    try:
        obj = get_object_or_404(Req, pk=pk)
    except (ProgrammingError, OperationalError):
        messages.error(request, "قاعدة البيانات غير محدثة (جدول طلبات الاشتراك غير موجود). نفّذ migrate ثم أعد المحاولة.")
        return redirect("dashboard:system_subscription_requests_list")

    if request.method == "POST":
        if not has_system_permission(request.user, "subscription_requests.manage"):
            raise PermissionDenied("لا تملك صلاحية معالجة طلبات الاشتراك.")
        from django.core.exceptions import ValidationError
        from subscriptions.request_processing import process_request

        try:
            _obj, changed = process_request(
                request_id=pk, action=(request.POST.get("action") or "").strip(),
                actor=request.user, admin_note=(request.POST.get("admin_note") or "").strip(),
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        except Exception:
            logger.exception("Subscription request transition failed request_id=%s", pk)
            messages.error(request, "تعذر إكمال العملية؛ لم تُحفظ تغييرات الاعتماد أو الدفعة. راجع سجل الأخطاء ثم أعد المحاولة.")
        else:
            if changed:
                messages.success(request, "تم حفظ حالة الطلب وربط الدفعة والفاتورة عند الاعتماد.")
            else:
                messages.info(request, "هذا الطلب مُعالج بالفعل.")
        return redirect("dashboard:system_subscription_request_detail", pk=pk)

    return render(
        request,
        "admin/subscription_request_detail.html",
        {
            "obj": obj,
            "can_manage_subscription_requests": has_system_permission(
                request.user, "subscription_requests.manage"
            ),
        },
    )

@manager_required
def my_subscription(request):
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    SubModel = _get_subscription_model()
    today = timezone.localdate()

    current_subscription = None
    upcoming_subscription = None
    primary_subscription = None
    primary_label = ""

    current_status_code = "none"
    current_status_label = "لا يوجد اشتراك"
    current_status_badge_class = "bg-rose-50 text-rose-700"

    DisplayScreen = DisplayScreenModel()
    screens = list(DisplayScreen.objects.filter(school=school))
    screens_total_count = len(screens)
    active_screens = [screen for screen in screens if bool(getattr(screen, "is_active", False))]
    screens_active_count = len(active_screens)
    screens_live_count = live_display_count(active_screens)
    screens_never_connected_count = sum(
        1
        for screen in active_screens
        if latest_display_presence(screen) is None
        and not (getattr(screen, "bound_device_id", "") or "").strip()
    )

    if screens_total_count == 0:
        screen_status_label = "لا توجد شاشات مسجلة"
        screen_status_class = "text-slate-600"
    elif screens_active_count == 0:
        screen_status_label = "جميع الشاشات معطلة"
        screen_status_class = "text-rose-600"
    elif screens_live_count == screens_active_count:
        screen_status_label = f"كل الشاشات متصلة الآن ({screens_live_count}/{screens_active_count})"
        screen_status_class = "text-emerald-700"
    elif screens_live_count > 0:
        screen_status_label = f"{screens_live_count} من {screens_active_count} شاشة متصلة الآن"
        screen_status_class = "text-amber-700"
    elif screens_never_connected_count == screens_active_count:
        screen_status_label = f"بانتظار أول اتصال ({screens_active_count} شاشة)"
        screen_status_class = "text-amber-700"
    else:
        screen_status_label = f"لا توجد شاشة متصلة حاليًا (0/{screens_active_count})"
        screen_status_class = "text-rose-600"

    def _field_exists(model_cls, field_name: str) -> bool:
        try:
            model_cls._meta.get_field(field_name)
            return True
        except Exception:
            return False

    def _get_attr(obj, name: str, default=None):
        try:
            return getattr(obj, name)
        except Exception:
            return default

    def _compute_display_ends_at(sub, start_field: str | None, end_field: str | None):
        if sub is None:
            return None
        end_value = _get_attr(sub, end_field) if end_field else None
        if end_value:
            return end_value

        plan = _get_attr(sub, "plan")
        start_value = _get_attr(sub, start_field) if start_field else None
        if plan is not None and start_value:
            try:
                days = _get_attr(plan, "duration_days")
                if days is not None:
                    days_int = int(days)
                    if days_int > 0:
                        return start_value + timedelta(days=days_int)
            except Exception:
                pass

        return None

    def _attach_display_fields(sub, start_field: str | None, end_field: str | None):
        if sub is None:
            return
        display_starts_at = _get_attr(sub, start_field) if start_field else None
        display_ends_at = _compute_display_ends_at(sub, start_field, end_field)
        try:
            setattr(sub, "display_starts_at", display_starts_at)
            setattr(sub, "display_ends_at", display_ends_at)
            if display_ends_at:
                setattr(sub, "display_days_left", max(0, int((display_ends_at - today).days)))
            else:
                setattr(sub, "display_days_left", None)
        except Exception:
            pass

    def _status_for(sub, start_field: str | None, end_field: str | None, status_field: str | None, is_active_field: str | None):
        if sub is None:
            return "none", "لا يوجد اشتراك", "bg-rose-50 text-rose-700"

        raw_status = _get_attr(sub, status_field) if status_field else None
        is_active_flag = _get_attr(sub, is_active_field) if is_active_field else None

        start_value = _get_attr(sub, start_field) if start_field else None
        end_value = _compute_display_ends_at(sub, start_field, end_field)

        if raw_status == "cancelled" or (raw_status is None and is_active_flag is False):
            return "cancelled", "ملغى", "bg-rose-50 text-rose-700"

        if raw_status == "pending":
            return "pending", "قيد الإعداد", "bg-amber-50 text-amber-700"

        if raw_status == "active" or (raw_status is None and is_active_flag is True):
            if start_value and start_value > today:
                return "upcoming", "لم يبدأ بعد", "bg-amber-50 text-amber-700"
            if end_value and end_value < today:
                return "expired", "منتهي", "bg-rose-50 text-rose-700"
            return "active", "سارية", "bg-emerald-50 text-emerald-700"

        if isinstance(raw_status, str) and raw_status:
            return raw_status, "غير معروف", "bg-slate-100 text-slate-700"

        return "unknown", "غير معروف", "bg-slate-100 text-slate-700"

    if SubModel is not None:
        start_field = "starts_at" if _field_exists(SubModel, "starts_at") else ("start_date" if _field_exists(SubModel, "start_date") else None)
        end_field = "ends_at" if _field_exists(SubModel, "ends_at") else ("end_date" if _field_exists(SubModel, "end_date") else None)
        status_field = "status" if _field_exists(SubModel, "status") else None
        is_active_field = "is_active" if _field_exists(SubModel, "is_active") else None

        qs = SubModel.objects.filter(school=school)

        # الاشتراك الحالي (ساري ضمن اليوم)
        current_qs = qs
        if status_field:
            current_qs = current_qs.filter(status="active")
        elif is_active_field:
            current_qs = current_qs.filter(is_active=True)
        if start_field:
            current_qs = current_qs.filter(**{f"{start_field}__lte": today})
        if end_field:
            current_qs = current_qs.filter(Q(**{f"{end_field}__isnull": True}) | Q(**{f"{end_field}__gte": today}))
        if start_field:
            current_qs = current_qs.order_by(f"-{start_field}", "-id")
        else:
            current_qs = current_qs.order_by("-id")
        current_subscription = current_qs.first()

        # الاشتراك القادم (أقرب اشتراك يبدأ في المستقبل)
        upcoming_qs = qs
        if status_field:
            upcoming_qs = upcoming_qs.filter(status__in=["active", "pending"])
        elif is_active_field:
            upcoming_qs = upcoming_qs.filter(is_active=True)
        if start_field:
            upcoming_qs = upcoming_qs.filter(**{f"{start_field}__gt": today}).order_by(start_field, "id")
            upcoming_subscription = upcoming_qs.first()

        _attach_display_fields(current_subscription, start_field, end_field)
        _attach_display_fields(upcoming_subscription, start_field, end_field)

        current_status_code, current_status_label, current_status_badge_class = _status_for(
            current_subscription,
            start_field,
            end_field,
            status_field,
            is_active_field,
        )

        primary_subscription = current_subscription or upcoming_subscription
        primary_label = "الاشتراك الحالي" if current_subscription else ("الاشتراك القادم" if upcoming_subscription else "")

    # ==========================
    # طلبات الاشتراك/التجديد
    # ==========================
    RequestModel = _get_subscription_request_model()
    renew_form = SubscriptionRenewalRequestForm(prefix="renew")
    new_form = SubscriptionNewRequestForm(prefix="new")

    def _shorten_receipt_filename(file_obj, *, prefix: str) -> Any:
        """Keep the stored ImageField path short (DB safety).

        Some production DBs may still have receipt_image as varchar(100) until migrations run.
        By shortening the uploaded filename we avoid 500s even before the ALTER migration.
        """
        if not file_obj:
            return file_obj
        original_name = (getattr(file_obj, "name", "") or "").strip()
        _base, ext = os.path.splitext(original_name)
        ext = (ext or ".jpg").lower()
        # keep extension sane
        if len(ext) > 10:
            ext = ext[:10]
        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        token = get_random_string(6)
        try:
            file_obj.name = f"{prefix}_{stamp}_{token}{ext}"
        except Exception:
            pass
        return file_obj

    # خطط الاشتراك (لإظهار تفاصيل الخطة في الواجهة)
    try:
        available_plans = list(
            SubscriptionPlan.objects.filter(is_active=True).order_by("sort_order", "price", "id")
        )
    except Exception:
        available_plans = []

    # تأكيد أن نموذج الاشتراك الجديد يستخدم نفس القائمة حتى عند POST
    try:
        new_form.fields["plan"].queryset = SubscriptionPlan.objects.filter(is_active=True).order_by(
            "sort_order", "price", "id"
        )
    except Exception:
        pass

    def _plan_details(plan_obj):
        if plan_obj is None:
            return None
        try:
            return plan_card(plan_obj)
        except Exception:
            return None

    available_plan_cards = plan_cards(available_plans)
    plans_map = {
        str(details["id"]): details
        for details in available_plan_cards
        if details.get("id") is not None
    }

    requested_plan = None
    requested_plan_code = (request.GET.get("plan") or "").strip()
    if request.method == "GET" and requested_plan_code:
        requested_plan = next(
            (
                plan
                for plan in available_plans
                if getattr(plan, "code", "") == requested_plan_code
                and (getattr(plan, "price", 0) or 0) > 0
            ),
            None,
        )
        if requested_plan is not None:
            new_form.initial["plan"] = requested_plan.pk

    renewal_source_plan = None
    if current_subscription is not None:
        renewal_source_plan = getattr(current_subscription, "plan", None)
    elif upcoming_subscription is not None:
        renewal_source_plan = getattr(upcoming_subscription, "plan", None)
    elif primary_subscription is not None:
        renewal_source_plan = getattr(primary_subscription, "plan", None)

    renewal_requires_new_plan = bool(
        renewal_source_plan is not None
        and not bool(getattr(renewal_source_plan, "is_active", False))
    )
    renewal_plan_obj = None if renewal_requires_new_plan else renewal_source_plan

    active_request_tab = (
        "new"
        if requested_plan is not None
        else ("renewal" if renewal_plan_obj is not None else "new")
    )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action in {"renewal", "new"}:
            active_request_tab = action

        if RequestModel is None:
            messages.error(request, "ميزة طلبات الاشتراك غير متاحة حالياً.")
            return redirect("dashboard:my_subscription")

        if action not in {"renewal", "new"}:
            messages.error(request, "طلب غير صالح.")
            return redirect("dashboard:my_subscription")

        has_open = RequestModel.objects.filter(
            school=school,
            status__in=["submitted", "under_review"],
        ).exists()
        if has_open:
            messages.warning(request, "لديكم طلب قيد المراجعة بالفعل. الرجاء انتظار الرد قبل إرسال طلب جديد.")
            return redirect("dashboard:my_subscription")

        if action == "renewal":
            renew_form = SubscriptionRenewalRequestForm(request.POST, request.FILES, prefix="renew")
            if renew_form.is_valid():
                plan_obj = renewal_plan_obj
                if plan_obj is None:
                    messages.error(
                        request,
                        "الخطة الحالية لم تعد معروضة للتجديد. اختر إحدى الباقات الجديدة.",
                    )
                    active_request_tab = "new"
                else:
                    RequestModel.objects.create(
                        school=school,
                        created_by=request.user,
                        request_type="renewal",
                        plan=plan_obj,
                        requested_starts_at=timezone.localdate(),
                        amount=getattr(plan_obj, "price", 0) or 0,
                        receipt_image=_shorten_receipt_filename(
                            renew_form.cleaned_data["receipt_image"],
                            prefix="renewal_receipt",
                        ),
                        transfer_note=renew_form.cleaned_data.get("transfer_note", "") or "",
                        status="submitted",
                    )
                    messages.success(request, "تم إرسال طلب التجديد بنجاح. سيتم مراجعته من الإدارة.")
                    return redirect("dashboard:my_subscription")
            else:
                messages.error(request, "الرجاء تصحيح الأخطاء في نموذج التجديد.")

        if action == "new":
            new_form = SubscriptionNewRequestForm(request.POST, request.FILES, prefix="new")
            try:
                new_form.fields["plan"].queryset = SubscriptionPlan.objects.filter(is_active=True).order_by(
                    "sort_order", "price", "id"
                )
            except Exception:
                pass
            if new_form.is_valid():
                plan_obj = new_form.cleaned_data["plan"]
                RequestModel.objects.create(
                    school=school,
                    created_by=request.user,
                    request_type="new",
                    plan=plan_obj,
                    requested_starts_at=timezone.localdate(),
                    amount=getattr(plan_obj, "price", 0) or 0,
                    receipt_image=_shorten_receipt_filename(
                        new_form.cleaned_data["receipt_image"],
                        prefix="new_receipt",
                    ),
                    transfer_note=new_form.cleaned_data.get("transfer_note", "") or "",
                    status="submitted",
                )
                messages.success(request, "تم إرسال طلب الاشتراك الجديد بنجاح. سيتم مراجعته من الإدارة.")
                return redirect("dashboard:my_subscription")
            messages.error(request, "الرجاء تصحيح الأخطاء في نموذج الاشتراك الجديد.")

    subscription_requests = []
    subscription_history = []
    if RequestModel is not None:
        try:
            subscription_requests = list(
                RequestModel.objects.filter(school=school)
                .select_related("plan", "processed_by", "approved_subscription")
                .order_by("-created_at", "-id")[:10]
            )
        except Exception:
            subscription_requests = []

    # ==========================
    # سجل العمليات: دمج الطلبات + الاشتراكات اليدوية
    # ==========================
    try:
        approved_sub_ids = set()
        if RequestModel is not None:
            approved_sub_ids = set(
                RequestModel.objects.filter(
                    school=school,
                    approved_subscription__isnull=False,
                ).values_list("approved_subscription_id", flat=True)
            )

        manual_subscriptions = []
        if SubModel is not None:
            manual_subscriptions = list(
                SubModel.objects.filter(school=school)
                .exclude(id__in=approved_sub_ids)
                .select_related("plan")
                .order_by("-created_at", "-id")[:10]
            )

        payment_ops_by_sub_id = {}
        try:
            from subscriptions.models import SubscriptionPaymentOperation

            sub_ids = [getattr(s, "pk", None) for s in manual_subscriptions if getattr(s, "pk", None) is not None]
            for r in subscription_requests:
                sid = getattr(r, "approved_subscription_id", None)
                if sid is not None:
                    sub_ids.append(sid)
            sub_ids = list({s for s in sub_ids if s is not None})

            if sub_ids:
                ops = (
                    SubscriptionPaymentOperation.objects.filter(
                        school=school,
                        subscription_id__in=sub_ids,
                    )
                    .prefetch_related("invoice")
                    .order_by("-created_at", "-id")
                )
                for op in ops:
                    sid = getattr(op, "subscription_id", None)
                    if sid and sid not in payment_ops_by_sub_id:
                        payment_ops_by_sub_id[sid] = op
        except Exception:
            payment_ops_by_sub_id = {}

        def _invoice_url_for_subscription_id(subscription_id: int | None) -> str | None:
            if not subscription_id:
                return None
            op = payment_ops_by_sub_id.get(subscription_id)
            if op is None:
                return None
            try:
                inv = getattr(op, "invoice", None)
            except Exception:
                inv = None
            if inv is None:
                return None
            try:
                return reverse("dashboard:subscription_invoice_view", kwargs={"pk": getattr(inv, "pk", None)})
            except Exception:
                return None

        for r in subscription_requests:
            receipt_url = None
            try:
                ri = getattr(r, "receipt_image", None)
                if ri and getattr(ri, "name", ""):
                    receipt_url = ri.url
            except Exception:
                receipt_url = None

            # طلبات الاشتراك الحالية تعتمد على رفع إيصال (تحويل بنكي)
            payment_method_label = "تحويل" if receipt_url else "—"
            try:
                amt = getattr(r, "amount", 0) or 0
                if float(amt) <= 0:
                    payment_method_label = "مجاني"
            except Exception:
                pass
            subscription_history.append(
                {
                    "date": getattr(r, "created_at", None),
                    "type_label": getattr(r, "get_request_type_display", lambda: "طلب")(),
                    "payment_method_label": payment_method_label,
                    "plan_name": getattr(getattr(r, "plan", None), "name", "—"),
                    "amount": getattr(r, "amount", None),
                    "status_code": getattr(r, "status", ""),
                    "status_label": getattr(r, "get_status_display", lambda: "")(),
                    "receipt_url": receipt_url,
                    "invoice_url": _invoice_url_for_subscription_id(getattr(r, "approved_subscription_id", None)),
                }
            )

        for s in manual_subscriptions:
            plan_obj = getattr(s, "plan", None)

            plan_price = 0
            try:
                plan_price = getattr(plan_obj, "price", 0) or 0
            except Exception:
                plan_price = 0

            payment_method_label = "غير محددة"
            try:
                if float(plan_price) <= 0:
                    payment_method_label = "مجاني"
                else:
                    op = payment_ops_by_sub_id.get(getattr(s, "pk", None))
                    if op is not None:
                        payment_method_label = (
                            "دفع إلكتروني"
                            if getattr(op, "method", "") == "moyasar"
                            else getattr(op, "get_method_display", lambda: "غير محددة")()
                        )
            except Exception:
                pass

            subscription_history.append(
                {
                    "date": getattr(s, "created_at", None),
                    "type_label": "تفعيل يدوي",
                    "payment_method_label": payment_method_label,
                    "plan_name": getattr(plan_obj, "name", "—"),
                    "amount": plan_price,
                    "status_code": getattr(s, "status", ""),
                    "status_label": getattr(s, "get_status_display", lambda: "")(),
                    "receipt_url": None,
                    "invoice_url": _invoice_url_for_subscription_id(getattr(s, "pk", None)),
                }
            )

        safe_min_dt = timezone.make_aware(datetime(1970, 1, 1))
        subscription_history.sort(
            key=lambda x: (x.get("date") or safe_min_dt),
            reverse=True,
        )
        subscription_history = subscription_history[:10]
    except Exception:
        subscription_history = []

    tamara_checkouts = []
    try:
        from subscriptions.models import TamaraCheckout

        tamara_checkouts = list(
            TamaraCheckout.objects.filter(school=school, created_by=request.user)
            .select_related("plan", "subscription", "payment_operation")
            .order_by("-created_at", "-id")[:5]
        )
    except Exception:
        tamara_checkouts = []

    tamara_available = bool(
        getattr(dj_settings, "TAMARA_ENABLED", False)
        and getattr(dj_settings, "TAMARA_API_TOKEN", "")
        and getattr(dj_settings, "TAMARA_NOTIFICATION_TOKEN", "")
    )
    tamara_environment = (
        "sandbox"
        if "sandbox" in str(getattr(dj_settings, "TAMARA_API_BASE_URL", "")).lower()
        else "production"
    )

    moyasar_checkouts = []
    try:
        from subscriptions.models import MoyasarCheckout

        moyasar_checkouts = list(
            MoyasarCheckout.objects.filter(school=school, created_by=request.user)
            .select_related("plan", "subscription", "payment_operation")
            .order_by("-created_at", "-id")[:5]
        )
    except Exception:
        moyasar_checkouts = []

    moyasar_configured = bool(
        getattr(dj_settings, "MOYASAR_ENABLED", False)
        and getattr(dj_settings, "MOYASAR_PUBLISHABLE_KEY", "")
        and getattr(dj_settings, "MOYASAR_SECRET_KEY", "")
    )
    moyasar_live_mode = bool(getattr(dj_settings, "MOYASAR_LIVE_MODE", False))
    # Test checkout is intentionally limited to the system owner. Test payments
    # can never activate production access (enforced again in processing).
    moyasar_available = bool(
        moyasar_configured
        and (moyasar_live_mode or dj_settings.DEBUG or request.user.is_superuser)
    )
    moyasar_pending_activation = bool(moyasar_configured and not moyasar_live_mode)

    # Checkout requires a proven address, so surface the gap before the
    # customer discovers it at the moment they try to pay.
    email_verified = user_email_is_verified(request.user)

    # A trial includes its bundled screen, but screen add-ons are only offered
    # after a paid base plan is active.
    screen_addon_offer = None
    try:
        from subscriptions.utils import school_active_paid_subscription

        screen_addon_subscription = school_active_paid_subscription(
            getattr(school, "pk", None),
            on_date=today,
        )
    except Exception:
        screen_addon_subscription = None

    if screen_addon_subscription is not None and moyasar_available:
        subscription_ends_at = getattr(screen_addon_subscription, "ends_at", None)
        if subscription_ends_at:
            remaining_days = max(1, (subscription_ends_at - today).days + 1)
        else:
            remaining_days = int(getattr(screen_addon_subscription.plan, "duration_days", 0) or 365)
        raw_options = [30, 90, 182, 365, remaining_days]
        duration_options = []
        price_matrix = {}
        seen_days = set()
        for days in raw_options:
            try:
                days_int = int(days or 0)
            except Exception:
                continue
            if days_int <= 0:
                continue
            days_int = min(days_int, remaining_days)
            if days_int in seen_days:
                continue
            seen_days.add(days_int)
            ends_at = today + timedelta(days=days_int - 1)
            if subscription_ends_at and ends_at > subscription_ends_at:
                ends_at = subscription_ends_at
            unit_price = prorated_screen_addon_price(
                1,
                plan=screen_addon_subscription.plan,
                starts_at=today,
                ends_at=ends_at,
            )
            if unit_price <= 0:
                continue
            price_matrix[str(days_int)] = {
                str(count): str(
                    prorated_screen_addon_price(
                        count,
                        plan=screen_addon_subscription.plan,
                        starts_at=today,
                        ends_at=ends_at,
                    )
                )
                for count in range(1, MAX_EXTRA_SCREENS + 1)
            }
            if days_int >= remaining_days:
                label = "حتى نهاية الاشتراك"
            elif days_int == 30:
                label = "شهر"
            elif days_int == 90:
                label = "3 أشهر"
            elif days_int == 182:
                label = "نصف سنة"
            elif days_int == 365:
                label = "سنة"
            else:
                label = f"{days_int} يوم"
            duration_options.append(
                {
                    "days": days_int,
                    "label": label,
                    "ends_at": ends_at,
                    "unit_price": unit_price,
                }
            )

        if duration_options:
            screen_addon_offer = {
                "max_screens": MAX_EXTRA_SCREENS,
                "ends_at": subscription_ends_at,
                "plan_name": screen_addon_subscription.plan.name,
                "duration_options": duration_options,
                "default_option": duration_options[0],
                "price_matrix": price_matrix,
            }

    return render(
        request,
        "dashboard/my_subscription.html",
        {
            "school": school,
            "subscription": primary_subscription,
            "primary_label": primary_label,

            "email_verified": email_verified,
            "account_email": (getattr(request.user, "email", "") or "").strip(),
            "screen_addon_offer": screen_addon_offer,

            "current_subscription": current_subscription,
            "current_status_code": current_status_code,
            "current_status_label": current_status_label,
            "current_status_badge_class": current_status_badge_class,

            "upcoming_subscription": upcoming_subscription,
            "today": today,

            "renew_form": renew_form,
            "new_form": new_form,
            "subscription_requests": subscription_requests,
            "subscription_history": subscription_history,
            "tamara_checkouts": tamara_checkouts,
            "tamara_available": tamara_available,
            "tamara_environment": tamara_environment,
            "moyasar_checkouts": moyasar_checkouts,
            "moyasar_available": moyasar_available,
            "moyasar_live_mode": moyasar_live_mode,
            "moyasar_pending_activation": moyasar_pending_activation,

            "active_request_tab": active_request_tab,
            "renewal_plan_details": _plan_details(renewal_plan_obj),
            "renewal_requires_new_plan": renewal_requires_new_plan,
            "plans_map": plans_map,
            "available_plan_cards": available_plan_cards,
            "requested_plan": requested_plan,
            "screens_total_count": screens_total_count,
            "screens_active_count": screens_active_count,
            "screens_live_count": screens_live_count,
            "screen_status_label": screen_status_label,
            "screen_status_class": screen_status_class,
        },
    )

@manager_required
def subscription_invoice_view(request, pk: int):
    """عرض الفاتورة للعميل (حسب المدرسة النشطة)."""
    from django.http import HttpResponse

    from subscriptions.models import SubscriptionInvoice

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    invoice_qs = SubscriptionInvoice.objects.select_related("school", "subscription", "plan")
    if not request.user.is_superuser:
        # Tenant isolation belongs in the query itself so another school's
        # invoice is indistinguishable from a non-existent invoice.
        invoice_qs = invoice_qs.filter(school=school)
    invoice = get_object_or_404(invoice_qs, pk=pk)

    try:
        from django.template.loader import render_to_string

        from subscriptions.invoicing import _get_seller_info, _get_school_contact_info
    except Exception:
        render_to_string = None
        _get_seller_info = None
        _get_school_contact_info = None

    # للمدارس: نعرض الفاتورة ببيانات المستخدم الحالي حتى لا تظهر بيانات الآدمن.
    if not request.user.is_superuser and render_to_string and _get_seller_info and _get_school_contact_info:
        contact_name, contact_mobile = _get_school_contact_info(invoice.school, preferred_user=request.user)
        html = render_to_string(
            "invoices/subscription_invoice.html",
            {
                "invoice": invoice,
                "seller": _get_seller_info(),
                "school": invoice.school,
                "subscription": invoice.subscription,
                "plan": invoice.plan,
                "contact_name": contact_name,
                "contact_mobile": contact_mobile,
            },
        )
    else:
        html = (invoice.html_snapshot or "").strip()
        if not html and render_to_string and _get_seller_info and _get_school_contact_info:
            try:
                contact_name, contact_mobile = _get_school_contact_info(invoice.school)
                html = render_to_string(
                    "invoices/subscription_invoice.html",
                    {
                        "invoice": invoice,
                        "seller": _get_seller_info(),
                        "school": invoice.school,
                        "subscription": invoice.subscription,
                        "plan": invoice.plan,
                        "contact_name": contact_name,
                        "contact_mobile": contact_mobile,
                    },
                )
                invoice.html_snapshot = html
                invoice.save(update_fields=["html_snapshot"])
            except Exception:
                html = ""

    if not html:
        messages.error(request, "تعذر عرض الفاتورة حاليًا.")
        return redirect("dashboard:my_subscription")

    return HttpResponse(html, content_type="text/html; charset=utf-8")

@system_permission_required("plans.view")
def system_plans_list(request):
    q = (request.GET.get("q") or "").strip()
    state = (request.GET.get("state") or "").strip()
    today = timezone.localdate()

    plans = SubscriptionPlan.objects.annotate(
        subscriptions_total=Count("subscriptions", distinct=True),
        active_subscriptions=Count(
            "subscriptions",
            filter=Q(subscriptions__status="active", subscriptions__starts_at__lte=today)
            & (Q(subscriptions__ends_at__isnull=True) | Q(subscriptions__ends_at__gte=today)),
            distinct=True,
        ),
    )
    if q:
        plans = plans.filter(Q(name__icontains=q) | Q(code__icontains=q))
    if state == "active":
        plans = plans.filter(is_active=True)
    elif state == "inactive":
        plans = plans.filter(is_active=False)

    plans = list(plans.order_by("sort_order", "price", "id"))
    for plan in plans:
        plan.catalog = plan_card(plan)

    all_plans = SubscriptionPlan.objects.all()
    context = {
        "plans": plans,
        "q": q,
        "state": state,
        "plans_count": all_plans.count(),
        "active_plans_count": all_plans.filter(is_active=True).count(),
        "paid_plans_count": all_plans.filter(price__gt=0).count(),
    }
    return render(request, "admin/plans_list.html", context)

@system_permission_required("plans.manage")
def system_plan_create(request):
    if request.method == "POST":
        form = SubscriptionPlanForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f"تمت إضافة باقة «{form.instance.name}» بنجاح.")
            return redirect("dashboard:system_plans_list")
    else:
        form = SubscriptionPlanForm()
    return render(
        request,
        "admin/plan_form.html",
        {"form": form, "title": "إضافة باقة جديدة", "submit_label": "إضافة الباقة"},
    )

@system_permission_required("plans.manage")
def system_plan_edit(request, pk):
    plan = get_object_or_404(SubscriptionPlan, pk=pk)
    if request.method == "POST":
        form = SubscriptionPlanForm(request.POST, instance=plan)
        if form.is_valid():
            form.save()
            messages.success(request, f"تم تحديث باقة «{plan.name}» بنجاح.")
            return redirect("dashboard:system_plans_list")
    else:
        form = SubscriptionPlanForm(instance=plan)
    return render(
        request,
        "admin/plan_form.html",
        {"form": form, "title": f"تعديل باقة «{plan.name}»", "plan": plan, "submit_label": "حفظ التعديلات"},
    )

@login_required
@system_permission_required("plans.manage")
@require_POST
def system_plan_toggle(request, pk):
    plan = get_object_or_404(SubscriptionPlan, pk=pk)
    plan.is_active = not plan.is_active
    plan.save(update_fields=["is_active"])
    if plan.is_active:
        messages.success(request, f"تم تفعيل باقة «{plan.name}» وأصبحت متاحة للاشتراك.")
    else:
        messages.warning(request, f"تم إيقاف باقة «{plan.name}» ولن تظهر للاشتراكات الجديدة.")
    return redirect("dashboard:system_plans_list")

@system_permission_required("plans.manage")
def system_plan_delete(request, pk):
    plan = get_object_or_404(SubscriptionPlan, pk=pk)
    linked_counts = {
        "subscriptions": plan.subscriptions.count(),
        "requests": plan.subscription_requests.count(),
        "payments": plan.payment_operations.count(),
        "invoices": plan.subscription_invoices.count(),
    }
    protected_count = sum(linked_counts.values())

    if request.method == "POST":
        if protected_count:
            if plan.is_active:
                plan.is_active = False
                plan.save(update_fields=["is_active"])
            messages.warning(
                request,
                f"تعذر حذف باقة «{plan.name}» لارتباطها بسجلات مالية أو اشتراكات؛ تم إيقافها بدلاً من حذفها.",
            )
            return redirect("dashboard:system_plans_list")
        try:
            plan_name = plan.name
            plan.delete()
        except ProtectedError:
            plan.is_active = False
            plan.save(update_fields=["is_active"])
            messages.warning(
                request,
                f"تعذر حذف باقة «{plan.name}» لوجود سجلات مرتبطة؛ تم إيقافها بدلاً من ذلك.",
            )
        else:
            messages.success(request, f"تم حذف باقة «{plan_name}» نهائياً.")
        return redirect("dashboard:system_plans_list")
    return render(
        request,
        "admin/plan_confirm_delete.html",
        {
            "plan": plan,
            "linked_counts": linked_counts,
            "protected_count": protected_count,
        },
    )

@system_permission_required("screen_addons.view")
def system_screen_addons_list(request):
    Addon = _get_screen_addon_model()
    if Addon is None:
        messages.error(request, "نظام زيادات الشاشات غير مثبت.")
        return redirect("dashboard:system_subscriptions_list")

    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    status_choices = list(getattr(Addon, "STATUS_CHOICES", ()) or ())
    cycle_labels = dict(getattr(Addon, "PRICING_CYCLE_CHOICES", ()) or ())
    if status and status not in {code for code, _label in status_choices}:
        status = ""

    qs = Addon.objects.all()
    try:
        qs = qs.select_related("subscription", "subscription__school", "subscription__plan")
    except Exception:
        pass

    if q:
        qs = qs.filter(
            Q(subscription__school__name__icontains=q)
            | Q(subscription__school__slug__icontains=q)
            | Q(subscription__plan__name__icontains=q)
        )

    counters = Addon.objects.all()
    status_counts = {
        code: counters.filter(status=code).count() for code, _label in status_choices
    }

    if status:
        qs = qs.filter(status=status)

    qs = qs.order_by("-created_at", "-id")

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    rows = []
    total_screens_added = 0
    for obj in page_obj.object_list:
        sub = getattr(obj, "subscription", None)
        school_obj = getattr(sub, "school", None) if sub else None
        plan_obj = getattr(sub, "plan", None) if sub else None
        cycle_key = getattr(obj, "pricing_cycle", "inherit") or "inherit"
        screens_added = getattr(obj, "screens_added", 0) or 0
        total_screens_added += int(screens_added)
        rows.append(
            {
                "id": obj.pk,
                "subscription_id": getattr(sub, "pk", None) if sub else None,
                "school_name": getattr(school_obj, "name", "—") if school_obj else "—",
                "plan_name": getattr(plan_obj, "name", "—") if plan_obj else "—",
                "screens_added": screens_added,
                "pricing_cycle": cycle_key,
                # Raw enum keys ("inherit", "semiannual") leaked into the table;
                # show the Arabic label the model already defines.
                "pricing_cycle_label": cycle_labels.get(cycle_key, cycle_key),
                "validity_days": getattr(obj, "validity_days", None),
                "starts_at": getattr(obj, "starts_at", None),
                "ends_at": getattr(obj, "ends_at", None),
                "status": getattr(obj, "status", ""),
                "total_price": getattr(obj, "total_price", None),
            }
        )

    return render(
        request,
        "admin/screen_addons_list.html",
        {
            "rows": rows,
            "page_obj": page_obj,
            "q": q,
            "status": status,
            "status_choices": status_choices,
            "paid_count": status_counts.get("paid", 0),
            "pending_count": status_counts.get("pending", 0),
            "cancelled_count": status_counts.get("cancelled", 0),
            "refunded_count": status_counts.get("refunded", 0),
            "total_count": counters.count(),
            "filtered_count": paginator.count,
            "total_screens_added": total_screens_added,
        },
    )

@system_permission_required("screen_addons.manage")
def system_screen_addon_create(request):
    Addon = _get_screen_addon_model()
    if Addon is None:
        messages.error(request, "نظام زيادات الشاشات غير مثبت.")
        return redirect("dashboard:system_screen_addons_list")

    if request.method == "POST":
        form = SubscriptionScreenAddonForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "تم إنشاء زيادة الشاشات بنجاح.")
            return redirect("dashboard:system_screen_addons_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        # Allow "add screens" links on the subscriptions list to preselect the
        # subscription instead of making the operator find it again.
        initial = {}
        requested_subscription = (request.GET.get("subscription") or "").strip()
        if requested_subscription.isdigit():
            SubModel = _get_subscription_model()
            if SubModel is not None and SubModel.objects.filter(pk=int(requested_subscription)).exists():
                initial["subscription"] = int(requested_subscription)
        form = SubscriptionScreenAddonForm(initial=initial)

    return render(request, "admin/screen_addon_form.html", {"form": form, "title": "إضافة زيادة شاشات"})

@system_permission_required("screen_addons.manage")
def system_screen_addon_edit(request, pk: int):
    Addon = _get_screen_addon_model()
    if Addon is None:
        messages.error(request, "نظام زيادات الشاشات غير مثبت.")
        return redirect("dashboard:system_screen_addons_list")

    obj = get_object_or_404(Addon, pk=pk)

    if request.method == "POST":
        form = SubscriptionScreenAddonForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "تم تحديث زيادة الشاشات.")
            return redirect("dashboard:system_screen_addons_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = SubscriptionScreenAddonForm(instance=obj)

    return render(
        request,
        "admin/screen_addon_form.html",
        {"form": form, "title": "تعديل زيادة شاشات", "edit": True, "obj": obj},
    )

@system_permission_required("screen_addons.manage")
def system_screen_addon_delete(request, pk: int):
    Addon = _get_screen_addon_model()
    if Addon is None:
        messages.error(request, "نظام زيادات الشاشات غير مثبت.")
        return redirect("dashboard:system_screen_addons_list")

    obj = get_object_or_404(Addon, pk=pk)
    if request.method == "POST":
        obj.delete()
        messages.warning(request, "تم حذف زيادة الشاشات.")
        return redirect("dashboard:system_screen_addons_list")

    return render(request, "admin/screen_addon_confirm_delete.html", {"obj": obj})

