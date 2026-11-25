from __future__ import annotations

import json
import logging
from datetime import timedelta

import jwt
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from core.models import SubscriptionPlan
from core.tenant_access import authorized_active_school
from core.email_verification import user_email_is_verified

from .models import (
    SchoolSubscription,
    TamaraCheckout,
)
from .plan_terms import paid_plan_start_date
from .tamara import (
    TamaraAPIError,
    TamaraClient,
    TamaraConfigurationError,
    build_checkout_payload,
)
from .tamara_processing import (
    mark_terminal_checkout,
    reconcile_checkout,
    TamaraVerificationError,
)


logger = logging.getLogger(__name__)


def _active_school(request):
    try:
        profile = request.user.profile
    except Exception:
        return None
    return authorized_active_school(profile, user=request.user)


def _mark_checkout_error(checkout: TamaraCheckout, message: str) -> None:
    checkout.status = "error"
    checkout.error_message = str(message)[:300]
    checkout.save(update_fields=["status", "error_message", "updated_at"])


@login_required(login_url="dashboard:login")
@require_POST
def tamara_start(request):
    if not getattr(settings, "TAMARA_ENABLED", False):
        messages.error(request, "الدفع عبر تمارا غير مفعّل حاليًا.")
        return redirect("dashboard:my_subscription")

    school = _active_school(request)
    if school is None:
        messages.error(request, "اختر مدرسة صالحة قبل بدء الدفع.")
        return redirect("dashboard:my_subscription")

    if not user_email_is_verified(request.user):
        messages.error(
            request,
            "الرجاء تأكيد بريدك الإلكتروني قبل الدفع لضمان وصول الفاتورة.",
        )
        return redirect("dashboard:my_subscription")

    request_type = (request.POST.get("request_type") or "").strip()
    if request_type not in {"new", "renewal"}:
        messages.error(request, "نوع الاشتراك غير صالح.")
        return redirect("dashboard:my_subscription")

    plan = get_object_or_404(
        SubscriptionPlan.objects.filter(is_active=True),
        pk=request.POST.get("plan_id"),
    )
    amount = getattr(plan, "price", 0) or 0
    if amount <= 0:
        messages.error(request, "هذه الخطة مجانية ولا تحتاج إلى دفع عبر تمارا.")
        return redirect("dashboard:my_subscription")

    if request_type == "renewal" and not SchoolSubscription.objects.filter(school=school, plan=plan).exists():
        messages.error(request, "لا يمكن تجديد خطة غير مرتبطة بمدرستك.")
        return redirect("dashboard:my_subscription")
    starts_at = paid_plan_start_date(school.pk, plan.pk)

    recent = (
        TamaraCheckout.objects.filter(
            school=school,
            created_by=request.user,
            plan=plan,
            request_type=request_type,
            status="new",
            created_at__gte=timezone.now() - timedelta(minutes=20),
        )
        .exclude(checkout_url="")
        .first()
    )
    if recent:
        return redirect(recent.checkout_url)

    checkout = TamaraCheckout.objects.create(
        school=school,
        created_by=request.user,
        plan=plan,
        request_type=request_type,
        starts_at=starts_at,
        amount=amount,
        currency="SAR",
    )

    try:
        user_agent = (request.headers.get("User-Agent") or "").lower()
        is_mobile = any(marker in user_agent for marker in ("android", "iphone", "ipad", "mobile"))
        payload = build_checkout_payload(checkout, request.user, is_mobile=is_mobile)
        client = TamaraClient()
        eligible = client.is_eligible(
            amount=checkout.amount,
            phone_number=payload["consumer"]["phone_number"],
            email=payload["consumer"]["email"],
        )
        if eligible is False:
            checkout.status = "declined"
            checkout.last_event = "pre_checkout_ineligible"
            checkout.processed_at = timezone.now()
            checkout.save(
                update_fields=["status", "last_event", "processed_at", "updated_at"]
            )
            messages.error(request, "تمارا غير متاحة لهذا الطلب حاليًا. يمكنك اختيار وسيلة دفع أخرى.")
            return redirect("dashboard:my_subscription")
        data = client.create_checkout(payload)
    except (TamaraConfigurationError, TamaraAPIError) as exc:
        _mark_checkout_error(checkout, str(exc))
        messages.error(request, str(exc))
        return redirect("dashboard:my_subscription")

    checkout.tamara_order_id = str(data["order_id"])
    checkout.checkout_id = str(data["checkout_id"])
    checkout.checkout_url = str(data["checkout_url"])
    checkout.status = "new"
    checkout.error_message = ""
    checkout.save(
        update_fields=[
            "tamara_order_id",
            "checkout_id",
            "checkout_url",
            "status",
            "error_message",
            "updated_at",
        ]
    )
    return redirect(checkout.checkout_url)


def _notification_jwt(request) -> str:
    header = (request.headers.get("Authorization") or "").strip()
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    return (request.GET.get("tamaraToken") or "").strip()


def _valid_notification(request) -> bool:
    signing_secret = str(getattr(settings, "TAMARA_NOTIFICATION_TOKEN", "") or "").strip()
    encoded = _notification_jwt(request)
    if not signing_secret or not encoded:
        return False
    try:
        jwt.decode(
            encoded,
            signing_secret,
            algorithms=["HS256"],
            issuer="Tamara",
            options={"require": ["exp", "iat"]},
        )
    except jwt.PyJWTError:
        return False
    return True


@csrf_exempt
@require_POST
def tamara_webhook(request):
    if not getattr(settings, "TAMARA_ENABLED", False):
        return JsonResponse({"detail": "disabled"}, status=503)
    if not getattr(settings, "TAMARA_NOTIFICATION_TOKEN", ""):
        return JsonResponse({"detail": "notification_token_missing"}, status=503)
    if not _valid_notification(request):
        return JsonResponse({"detail": "forbidden"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"detail": "invalid_json"}, status=400)

    reference = str(payload.get("order_reference_id") or "").strip()
    order_id = str(payload.get("order_id") or "").strip()
    event_type = str(payload.get("event_type") or "").strip()
    if not reference or not order_id or not event_type:
        return JsonResponse({"detail": "missing_fields"}, status=400)

    checkout = TamaraCheckout.objects.filter(merchant_reference=reference).first()
    if checkout is None:
        # Webhooks are registered for the merchant account, which is shared
        # with other applications. Acknowledge their signed events so Tamara
        # does not retry them, while still surfacing missing School Display orders.
        if not reference.startswith("TM-"):
            return JsonResponse({"ok": True, "ignored": "other_platform"})
        return JsonResponse({"detail": "unknown_order"}, status=404)
    if checkout.tamara_order_id and checkout.tamara_order_id != order_id:
        logger.warning("tamara_webhook_order_mismatch reference=%s", reference)
        return JsonResponse({"detail": "order_mismatch"}, status=409)

    if (
        event_type in {"order_approved", "order_authorised"}
        and checkout.status in {"authorised", "captured"}
        and checkout.payment_operation_id
    ):
        return JsonResponse({"ok": True, "duplicate": True})

    terminal_statuses = {
        "order_declined": "declined",
        "order_canceled": "canceled",
        "order_cancelled": "canceled",
        "order_expired": "expired",
        "order_refunded": "refunded",
    }
    if event_type in terminal_statuses:
        mark_terminal_checkout(
            checkout.pk,
            status=terminal_statuses[event_type],
            event_type=event_type,
        )
        return JsonResponse({"ok": True})

    if event_type == "order_approved":
        try:
            reconcile_checkout(checkout.pk)
        except (TamaraConfigurationError, TamaraAPIError, TamaraVerificationError):
            logger.warning("tamara_authorise_failed reference=%s", reference)
            return JsonResponse({"detail": "authorise_failed"}, status=502)
        return JsonResponse({"ok": True})

    if event_type in {"order_authorised", "order_captured"}:
        try:
            reconcile_checkout(checkout.pk)
        except (TamaraConfigurationError, TamaraAPIError, TamaraVerificationError):
            logger.warning("tamara_webhook_reconciliation_failed reference=%s", reference)
            return JsonResponse({"detail": "reconciliation_failed"}, status=502)
        return JsonResponse({"ok": True})

    return JsonResponse({"ok": True, "ignored": True})


@login_required(login_url="dashboard:login")
@require_GET
def tamara_return(request, outcome: str):
    school = _active_school(request)
    reference = (request.GET.get("reference") or "").strip()
    checkout = None
    if school is not None and reference:
        checkout = TamaraCheckout.objects.filter(
            school=school,
            created_by=request.user,
            merchant_reference=reference,
        ).first()

    if checkout is None:
        messages.error(request, "تعذر العثور على عملية تمارا المرتبطة بالحساب.")
    elif outcome == "success":
        if checkout.status not in {"authorised", "captured"} and checkout.tamara_order_id:
            try:
                checkout = reconcile_checkout(checkout.pk)
            except (TamaraConfigurationError, TamaraAPIError, TamaraVerificationError):
                logger.warning(
                    "tamara_return_reconciliation_failed reference=%s",
                    checkout.merchant_reference,
                )
        if checkout.status in {"authorised", "captured"}:
            if checkout.subscription_id and checkout.subscription.starts_at > timezone.localdate():
                starts_at = checkout.subscription.starts_at.isoformat()
                messages.success(request, f"اكتمل دفع تمارا. تبدأ مدة الاشتراك في {starts_at}.")
            else:
                messages.success(request, "اكتمل دفع تمارا وتم تفعيل الاشتراك.")
        else:
            messages.info(request, "استلمنا عودتك من تمارا، وجارٍ تأكيد الدفع تلقائيًا.")
    elif outcome == "cancel":
        messages.warning(request, "تم إلغاء صفحة دفع تمارا، ولم يتم تفعيل الاشتراك.")
    else:
        messages.error(request, "لم تكتمل عملية تمارا. يمكنك المحاولة مرة أخرى.")
    return redirect("dashboard:my_subscription")
