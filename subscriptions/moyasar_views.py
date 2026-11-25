from __future__ import annotations

import hmac
import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from core.email_verification import user_email_is_verified
from core.models import SubscriptionPlan
from core.tenant_access import authorized_active_school

from .discounts import DiscountError, quote_discount
from .models import MoyasarCheckout, SchoolSubscription
from .moyasar import MoyasarAPIError, MoyasarClient, MoyasarConfigurationError
from .plan_terms import paid_plan_start_date
from .pricing import (
    checkout_total,
    normalize_extra_screens,
    normalize_screen_addon_days,
    prorated_screen_addon_price,
)
from .moyasar_processing import (
    MoyasarVerificationError,
    amount_to_minor_units,
    apply_payment_details,
)
from .utils import school_active_paid_subscription


logger = logging.getLogger(__name__)
_PAYMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def _active_school(request):
    try:
        profile = request.user.profile
    except Exception:
        return None
    return authorized_active_school(profile, user=request.user)


def _configured() -> bool:
    return bool(
        getattr(settings, "MOYASAR_ENABLED", False)
        and getattr(settings, "MOYASAR_PUBLISHABLE_KEY", "")
        and getattr(settings, "MOYASAR_SECRET_KEY", "")
    )


def _user_may_checkout(user) -> bool:
    if not _configured():
        return False
    return bool(
        getattr(settings, "MOYASAR_LIVE_MODE", False)
        or getattr(settings, "DEBUG", False)
        or getattr(user, "is_superuser", False)
    )


def _active_subscription(school):
    """The running paid subscription a screens-only purchase attaches to."""
    return school_active_paid_subscription(getattr(school, "pk", None))


def _screen_addon_window(*, starts_at: date, subscription, requested_days) -> tuple[int, date]:
    """Return the billable add-on duration, capped to the running subscription."""
    plan_days = int(getattr(getattr(subscription, "plan", None), "duration_days", 0) or 0)
    subscription_ends_at = getattr(subscription, "ends_at", None)
    if subscription_ends_at:
        remaining_days = max(1, (subscription_ends_at - starts_at).days + 1)
        max_days = remaining_days
    else:
        remaining_days = plan_days if plan_days > 0 else 30
        max_days = plan_days if plan_days > 0 else 365

    days = normalize_screen_addon_days(
        requested_days,
        default_days=remaining_days,
        max_days=max_days,
    )
    ends_at = starts_at + timedelta(days=days - 1)
    if subscription_ends_at and ends_at > subscription_ends_at:
        ends_at = subscription_ends_at
        days = max(1, (ends_at - starts_at).days + 1)
    return days, ends_at


@login_required(login_url="dashboard:login")
@require_POST
def moyasar_start(request):
    if not _user_may_checkout(request.user):
        messages.error(request, "الدفع الإلكتروني بانتظار التفعيل حاليًا.")
        return redirect("dashboard:my_subscription")

    school = _active_school(request)
    if school is None:
        messages.error(request, "اختر مدرسة صالحة قبل بدء الدفع.")
        return redirect("dashboard:my_subscription")

    # An unverified address means the invoice we are about to issue may never
    # reach the customer. Prove it works before taking money, not after.
    if not user_email_is_verified(request.user):
        messages.error(
            request,
            "الرجاء تأكيد بريدك الإلكتروني قبل الدفع لضمان وصول الفاتورة. "
            "أرسلنا لك رابط التأكيد، ويمكنك طلب رابط جديد من صفحة اشتراكي.",
        )
        return redirect("dashboard:my_subscription")

    request_type = (request.POST.get("request_type") or "").strip()
    if request_type not in {"new", "renewal", "screens"}:
        messages.error(request, "نوع الاشتراك غير صالح.")
        return redirect("dashboard:my_subscription")

    extra_screens = normalize_extra_screens(request.POST.get("extra_screens"))

    # Screen add-ons are a post-subscription operation. Never allow a crafted
    # first-subscription/renewal request to bundle them before the base plan is
    # active.
    if request_type != "screens" and extra_screens > 0:
        messages.error(
            request,
            "أكمل تفعيل الاشتراك الأساسي أولًا، ثم أضف الشاشات من صفحة اشتراكي.",
        )
        return redirect("dashboard:my_subscription")

    if request_type == "screens":
        current = _active_subscription(school)
        if current is None:
            messages.error(request, "يلزم وجود اشتراك مدفوع وسارٍ قبل شراء شاشات إضافية.")
            return redirect("dashboard:my_subscription")
        if extra_screens <= 0:
            messages.error(request, "حدد عدد الشاشات الإضافية المطلوبة.")
            return redirect("dashboard:my_subscription")

        plan = current.plan
        starts_at = timezone.localdate()
        addon_days, addon_ends_at = _screen_addon_window(
            starts_at=starts_at,
            subscription=current,
            requested_days=request.POST.get("addon_duration_days"),
        )
        amount = prorated_screen_addon_price(
            extra_screens,
            plan=plan,
            starts_at=starts_at,
            ends_at=addon_ends_at,
        )
        if amount <= 0:
            messages.error(request, "تعذر احتساب قيمة الشاشات الإضافية لهذه المدة.")
            return redirect("dashboard:my_subscription")
    else:
        plan = get_object_or_404(
            SubscriptionPlan.objects.filter(is_active=True),
            pk=request.POST.get("plan_id"),
        )
        amount = checkout_total(plan, extra_screens)
        if amount <= 0:
            messages.error(request, "هذه الخطة مجانية ولا تحتاج إلى بوابة دفع.")
            return redirect("dashboard:my_subscription")

        if request_type == "renewal" and not SchoolSubscription.objects.filter(school=school, plan=plan).exists():
            messages.error(request, "لا يمكن تجديد خطة غير مرتبطة بمدرستك.")
            return redirect("dashboard:my_subscription")
        starts_at = paid_plan_start_date(school.pk, plan.pk)
        addon_days = None
        addon_ends_at = None

    # ✅ كود الخصم: يُتحقق منه ويُخصم من الإجمالي قبل إنشاء جلسة الدفع، لأن
    # _validate_details يقارن مبلغ البوابة بمبلغ الجلسة المخزن حرفياً.
    discount_quote = None
    raw_discount = (request.POST.get("discount_code") or "").strip()
    if raw_discount:
        if request_type == "screens":
            messages.error(request, "كود الخصم متاح لاشتراكات الباقات فقط.")
            return redirect("dashboard:my_subscription")
        try:
            discount_quote = quote_discount(raw_discount, school, amount, plan=plan)
        except DiscountError as exc:
            messages.error(request, str(exc))
            return redirect("dashboard:my_subscription")
        if discount_quote.final_total < Decimal("1.00"):
            messages.error(
                request,
                "قيمة الطلب بعد الخصم أقل من الحد الأدنى للدفع الإلكتروني (ريال واحد).",
            )
            return redirect("dashboard:my_subscription")
        amount = discount_quote.final_total

    live_mode = bool(getattr(settings, "MOYASAR_LIVE_MODE", False))
    recent = (
        MoyasarCheckout.objects.filter(
            school=school,
            created_by=request.user,
            plan=plan,
            request_type=request_type,
            extra_screens=extra_screens,
            screen_addon_validity_days=addon_days,
            screen_addon_ends_at=addon_ends_at,
            amount=amount,
            discount_code=discount_quote.code if discount_quote else None,
            status="initiated",
            live_mode=live_mode,
            created_at__gte=timezone.now() - timedelta(minutes=20),
        )
        .order_by("-created_at")
        .first()
    )
    checkout = recent or MoyasarCheckout.objects.create(
        school=school,
        created_by=request.user,
        plan=plan,
        request_type=request_type,
        starts_at=starts_at,
        extra_screens=extra_screens,
        screen_addon_validity_days=addon_days,
        screen_addon_ends_at=addon_ends_at,
        amount=amount,
        discount_code=discount_quote.code if discount_quote else None,
        discount_amount=discount_quote.discount_amount if discount_quote else Decimal("0.00"),
        currency="SAR",
        live_mode=live_mode,
    )
    return redirect("subscriptions:moyasar_checkout", reference=checkout.merchant_reference)


@login_required(login_url="dashboard:login")
@require_GET
def moyasar_checkout(request, reference: str):
    if not _user_may_checkout(request.user):
        messages.error(request, "الدفع الإلكتروني بانتظار التفعيل حاليًا.")
        return redirect("dashboard:my_subscription")
    school = _active_school(request)
    checkout = get_object_or_404(
        MoyasarCheckout.objects.select_related("school", "plan"),
        merchant_reference=reference,
        school=school,
        created_by=request.user,
    )
    if checkout.status != "initiated":
        messages.info(request, "هذه المحاولة عولجت سابقًا. يمكنك مراجعة حالتها في سجل المدفوعات.")
        return redirect("dashboard:my_subscription")

    callback_base = str(getattr(settings, "MOYASAR_CALLBACK_BASE_URL", "") or "").rstrip("/")
    # Keep our reference in the path. Moyasar appends its own query string
    # (id/status/message), so a query parameter here is needlessly fragile.
    callback_path = reverse(
        "subscriptions:moyasar_return_for_checkout",
        kwargs={"reference": checkout.merchant_reference},
    )
    callback_url = f"{callback_base}{callback_path}"
    config = {
        "amount": amount_to_minor_units(checkout.amount),
        "currency": checkout.currency,
        "description": f"School Display - {checkout.plan.name} - {checkout.merchant_reference}",
        "publishable_api_key": str(settings.MOYASAR_PUBLISHABLE_KEY),
        "callback_url": callback_url,
        "supported_networks": list(
            getattr(settings, "MOYASAR_SUPPORTED_NETWORKS", ["mada", "visa", "mastercard"])
        ),
        "methods": list(getattr(settings, "MOYASAR_PAYMENT_METHODS", ["creditcard"])),
        "fixed_width": False,
        "metadata": {
            "merchant_reference": checkout.merchant_reference,
            "school_id": str(checkout.school_id),
            "plan_id": str(checkout.plan_id),
        },
        # Removed by our JavaScript before Moyasar.init(). Used by
        # ``on_completed`` to persist/verify the payment id before 3DS.
        "payment_sync_url": reverse(
            "subscriptions:moyasar_sync",
            kwargs={"reference": checkout.merchant_reference},
        ),
    }
    if "applepay" in config["methods"]:
        config["apple_pay"] = {
            "country": str(getattr(settings, "MOYASAR_APPLE_PAY_COUNTRY", "SA") or "SA").strip().upper() or "SA",
            "label": str(getattr(settings, "MOYASAR_APPLE_PAY_LABEL", "لوحة العرض الذكية") or "لوحة العرض الذكية").strip()
            or "لوحة العرض الذكية",
            "validate_merchant_url": str(
                getattr(
                    settings,
                    "MOYASAR_APPLE_PAY_VALIDATE_MERCHANT_URL",
                    "https://api.moyasar.com/v1/applepay/initiate",
                )
                or "https://api.moyasar.com/v1/applepay/initiate"
            ).strip().rstrip("/"),
        }
    if "googlepay" in config["methods"]:
        google_pay_merchant_id = str(getattr(settings, "MOYASAR_GOOGLE_PAY_MERCHANT_ID", "") or "").strip()
        if google_pay_merchant_id:
            config["google_pay"] = {
                "merchant_id": google_pay_merchant_id,
                "country": str(getattr(settings, "MOYASAR_GOOGLE_PAY_COUNTRY", "SA") or "SA").strip().upper() or "SA",
                "label": str(getattr(settings, "MOYASAR_GOOGLE_PAY_LABEL", "لوحة العرض الذكية") or "لوحة العرض الذكية").strip()
                or "لوحة العرض الذكية",
                "environment": str(
                    getattr(settings, "MOYASAR_GOOGLE_PAY_ENVIRONMENT", "PRODUCTION") or "PRODUCTION"
                ).strip().upper()
                or "PRODUCTION",
            }
        else:
            config["methods"] = [method for method in config["methods"] if method != "googlepay"]
    return render(
        request,
        "subscriptions/moyasar_checkout.html",
        {"checkout": checkout, "moyasar_config": config},
    )


@login_required(login_url="dashboard:login")
@require_POST
def moyasar_cancel(request, reference: str):
    school = _active_school(request)
    checkout = get_object_or_404(
        MoyasarCheckout,
        merchant_reference=reference,
        school=school,
        created_by=request.user,
    )
    canceled = MoyasarCheckout.objects.filter(
        pk=checkout.pk,
        status="initiated",
    ).update(
        status="voided",
        last_event="customer_canceled",
        processed_at=timezone.now(),
        error_message="",
        updated_at=timezone.now(),
    )
    if canceled:
        messages.info(request, "تم إلغاء محاولة الدفع، ويمكنك بدء محاولة جديدة متى شئت.")
    else:
        messages.info(request, "تعذّر إلغاء المحاولة لأنها عولجت مسبقًا.")
    return redirect("dashboard:my_subscription")


@login_required(login_url="dashboard:login")
@require_GET
def moyasar_return(request, reference: str | None = None):
    school = _active_school(request)
    reference = (reference or request.GET.get("reference") or "").strip()
    payment_id = (request.GET.get("id") or "").strip()
    checkout = MoyasarCheckout.objects.filter(
        merchant_reference=reference,
        school=school,
        created_by=request.user,
    ).first()
    if checkout is None or not _PAYMENT_ID_RE.fullmatch(payment_id):
        messages.error(request, "تعذر العثور على عملية الدفع الإلكتروني المرتبطة بالحساب.")
        return redirect("dashboard:my_subscription")

    try:
        details = MoyasarClient().fetch_payment(payment_id)
        checkout = apply_payment_details(checkout.pk, details, event_type="return")
    except (MoyasarConfigurationError, MoyasarAPIError, MoyasarVerificationError):
        logger.warning("moyasar_return_verification_failed reference=%s", reference)
        messages.error(request, "تعذر تأكيد عملية الدفع الإلكتروني. لم يتم تفعيل الاشتراك.")
        return redirect("dashboard:my_subscription")

    if checkout.status in {"paid", "captured"}:
        if checkout.payment_operation_id and checkout.request_type == "screens":
            messages.success(request, "اكتمل الدفع الإلكتروني وتمت إضافة الشاشات.")
        elif checkout.payment_operation_id and checkout.subscription_id and checkout.subscription.starts_at > timezone.localdate():
            starts_at = checkout.subscription.starts_at.isoformat()
            messages.success(request, f"اكتمل الدفع الإلكتروني. تبدأ مدة الاشتراك في {starts_at}.")
        elif checkout.payment_operation_id:
            messages.success(request, "اكتمل الدفع الإلكتروني وتم تفعيل الاشتراك.")
        else:
            # Only reachable in test mode, which is restricted to staff accounts.
            messages.warning(
                request,
                "وضع اختبار: نجحت عملية الدفع التجريبية ولم يُفعَّل اشتراك حقيقي.",
            )
    elif checkout.status == "initiated":
        messages.info(request, "عملية الدفع الإلكتروني ما زالت قيد التحقق. سنحدّث حالتها تلقائيًا.")
    else:
        messages.error(request, "لم تكتمل عملية الدفع الإلكتروني، ولم يتم تفعيل الاشتراك.")
    return redirect("dashboard:my_subscription")


@login_required(login_url="dashboard:login")
@require_POST
def moyasar_sync(request, reference: str):
    """Persist and verify the payment created by Moyasar before 3DS redirect."""
    school = _active_school(request)
    checkout = MoyasarCheckout.objects.filter(
        merchant_reference=reference,
        school=school,
        created_by=request.user,
    ).first()
    if checkout is None:
        return JsonResponse({"detail": "checkout_not_found"}, status=404)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"detail": "invalid_json"}, status=400)
    payment_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
    if not _PAYMENT_ID_RE.fullmatch(payment_id):
        return JsonResponse({"detail": "invalid_payment_id"}, status=400)

    try:
        details = MoyasarClient().fetch_payment(payment_id)
        checkout = apply_payment_details(checkout.pk, details, event_type="on_completed")
    except (MoyasarConfigurationError, MoyasarAPIError):
        logger.warning("moyasar_sync_fetch_failed reference=%s", reference)
        return JsonResponse({"detail": "gateway_unavailable"}, status=503)
    except MoyasarVerificationError:
        logger.warning("moyasar_sync_verification_failed reference=%s", reference)
        return JsonResponse({"detail": "verification_failed"}, status=409)

    return JsonResponse(
        {
            "ok": True,
            "status": checkout.status,
            "activated": bool(checkout.payment_operation_id),
        }
    )


@csrf_exempt
@require_POST
def moyasar_webhook(request):
    if not getattr(settings, "MOYASAR_ENABLED", False):
        return JsonResponse({"detail": "disabled"}, status=503)
    expected_secret = str(getattr(settings, "MOYASAR_WEBHOOK_SECRET", "") or "")
    if not expected_secret:
        return JsonResponse({"detail": "webhook_secret_missing"}, status=503)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"detail": "invalid_json"}, status=400)
    if not isinstance(payload, dict) or not hmac.compare_digest(
        str(payload.get("secret_token") or ""),
        expected_secret,
    ):
        return JsonResponse({"detail": "forbidden"}, status=403)

    details = payload.get("data")
    if not isinstance(details, dict):
        return JsonResponse({"detail": "missing_payment"}, status=400)
    details = dict(details)
    envelope_live = payload.get("live")
    detail_live = details.get("live")
    if envelope_live is not None and not isinstance(envelope_live, bool):
        return JsonResponse({"detail": "invalid_environment"}, status=400)
    if isinstance(envelope_live, bool):
        if detail_live is not None and detail_live != envelope_live:
            return JsonResponse({"detail": "environment_mismatch"}, status=409)
        details["live"] = envelope_live
    metadata = details.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    reference = str(metadata.get("merchant_reference") or "").strip()
    checkout = MoyasarCheckout.objects.filter(merchant_reference=reference).first()
    if checkout is None:
        return JsonResponse({"detail": "checkout_not_found"}, status=404)
    try:
        apply_payment_details(
            checkout.pk,
            details,
            event_type=str(payload.get("type") or "webhook"),
        )
    except MoyasarVerificationError:
        logger.warning("moyasar_webhook_verification_failed reference=%s", reference)
        return JsonResponse({"detail": "verification_failed"}, status=409)
    return JsonResponse({"ok": True})
