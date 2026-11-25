from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .invoicing import build_invoice_from_operation
from .models import (
    SubscriptionInvoice,
    SubscriptionPaymentOperation,
    TamaraCheckout,
)
from .plan_terms import activate_gateway_plan
from .tamara import TamaraAPIError, TamaraClient, TamaraConfigurationError


logger = logging.getLogger(__name__)

WORKER_HEARTBEAT_KEY = "subscriptions:tamara-reconciliation-worker:heartbeat"
WORKER_HEARTBEAT_TTL_SECONDS = 90

_CAPTURED_STATUSES = {"captured", "fully_captured", "partially_captured"}
_AUTHORISED_STATUSES = {"authorised", "authorized"}
_TERMINAL_STATUS_MAP = {
    "declined": "declined",
    "canceled": "canceled",
    "cancelled": "canceled",
    "expired": "expired",
    "fully_refunded": "refunded",
    "partially_refunded": "refunded",
    "refunded": "refunded",
}


class TamaraVerificationError(Exception):
    """Raised when Tamara's order does not match the local checkout."""


@dataclass(frozen=True)
class ReconciliationResult:
    checked: int = 0
    activated: int = 0
    captured: int = 0
    failed: int = 0


def normalize_remote_status(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _verify_remote_order(checkout: TamaraCheckout, details: dict) -> None:
    order_id = str(details.get("order_id") or "").strip()
    if order_id != str(checkout.tamara_order_id or "").strip():
        raise TamaraVerificationError("رقم طلب تمارا لا يطابق عملية الدفع المحلية.")

    reference = str(details.get("order_reference_id") or "").strip()
    if reference != checkout.merchant_reference:
        raise TamaraVerificationError("مرجع طلب تمارا لا يطابق عملية الدفع المحلية.")

    total = details.get("total_amount") or {}
    try:
        amount = Decimal(str(total.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise TamaraVerificationError("تعذر التحقق من مبلغ طلب تمارا.") from None
    if amount != Decimal(checkout.amount).quantize(Decimal("0.01")):
        raise TamaraVerificationError("مبلغ طلب تمارا لا يطابق المبلغ المطلوب.")

    currency = str(total.get("currency") or "").strip().upper()
    if currency != str(checkout.currency or "").strip().upper():
        raise TamaraVerificationError("عملة طلب تمارا لا تطابق العملة المطلوبة.")


def activate_checkout(checkout_id: int, *, status: str, event_type: str) -> TamaraCheckout:
    """Activate exactly once after Tamara confirms an authorised/captured order."""
    with transaction.atomic():
        checkout = (
            # Lock only the checkout row. ``payment_operation`` is nullable, so
            # PostgreSQL rejects a blanket FOR UPDATE across its outer join.
            TamaraCheckout.objects.select_for_update(of=("self",))
            .select_related("school", "plan", "created_by", "payment_operation")
            .get(pk=checkout_id)
        )
        # A success callback and webhook can reconcile the same checkout almost
        # simultaneously. Refresh after the row lock so the second worker sees
        # the payment_operation saved by the first one.
        checkout.refresh_from_db()

        note = f"Tamara {checkout.merchant_reference} / {checkout.tamara_order_id}"
        operation = checkout.payment_operation
        if operation is None:
            # Older payments can exist before the checkout stores their operation.
            operation = (
                SubscriptionPaymentOperation.objects.filter(
                    school_id=checkout.school_id,
                    plan_id=checkout.plan_id,
                    subscription__school_id=checkout.school_id,
                    subscription__plan_id=checkout.plan_id,
                    subscription__starts_at=checkout.starts_at,
                    amount=checkout.amount,
                    method="tamara",
                    source="request",
                    note=note,
                )
                .order_by("id")
                .first()
            )
        if operation is not None:
            subscription = operation.subscription
        else:
            subscription = activate_gateway_plan(
                school_id=checkout.school_id,
                plan=checkout.plan,
                requested_start=checkout.starts_at,
                notes=f"Paid via Tamara {checkout.merchant_reference}",
            )
            operation = SubscriptionPaymentOperation.objects.create(
                school=checkout.school,
                subscription=subscription,
                plan=checkout.plan,
                amount=checkout.amount,
                method="tamara",
                source="request",
                created_by=checkout.created_by,
                note=note,
            )

            from .audit import record

            record(
                "payment_recorded",
                school=checkout.school,
                subscription=subscription,
                actor=checkout.created_by,
                amount=checkout.amount,
                summary="تفعيل اشتراك عبر تمارا",
                context={
                    "gateway": "tamara",
                    "merchant_reference": checkout.merchant_reference,
                    "order_id": checkout.tamara_order_id or "",
                    "event": event_type,
                    "request_type": checkout.request_type,
                },
            )

        if not SubscriptionInvoice.objects.filter(operation=operation).exists():
            build_invoice_from_operation(operation)

        checkout.subscription = subscription
        checkout.payment_operation = operation
        checkout.status = status
        checkout.last_event = event_type
        checkout.processed_at = timezone.now()
        checkout.error_message = ""
        checkout.save(
            update_fields=[
                "subscription",
                "payment_operation",
                "status",
                "last_event",
                "processed_at",
                "error_message",
                "updated_at",
            ]
        )
        return checkout


def mark_terminal_checkout(checkout_id: int, *, status: str, event_type: str) -> TamaraCheckout:
    with transaction.atomic():
        checkout = TamaraCheckout.objects.select_for_update().get(pk=checkout_id)
        checkout.status = status
        checkout.last_event = event_type
        checkout.processed_at = timezone.now()
        checkout.error_message = ""
        checkout.save(
            update_fields=[
                "status",
                "last_event",
                "processed_at",
                "error_message",
                "updated_at",
            ]
        )
        return checkout


def reconcile_checkout(checkout_id: int) -> TamaraCheckout:
    """Pull the authoritative Tamara status and progress a checkout safely."""
    checkout = TamaraCheckout.objects.select_related("plan").get(pk=checkout_id)
    if not checkout.tamara_order_id:
        return checkout
    if checkout.status == "captured" and checkout.payment_operation_id:
        return checkout

    client = TamaraClient()
    details = client.get_order(checkout.tamara_order_id)
    _verify_remote_order(checkout, details)
    remote_status = normalize_remote_status(details.get("status"))

    if remote_status == "approved":
        result = client.authorise_order(checkout.tamara_order_id)
        remote_status = normalize_remote_status(result.get("status"))
        if remote_status not in _CAPTURED_STATUSES | _AUTHORISED_STATUSES:
            details = client.get_order(checkout.tamara_order_id)
            remote_status = normalize_remote_status(details.get("status"))

    if remote_status in _CAPTURED_STATUSES:
        return activate_checkout(
            checkout.pk,
            status="captured",
            event_type=f"pull:{remote_status}",
        )

    if remote_status in _AUTHORISED_STATUSES:
        checkout = activate_checkout(
            checkout.pk,
            status="authorised",
            event_type=f"pull:{remote_status}",
        )
        if not getattr(settings, "TAMARA_CAPTURE_DIGITAL_ORDERS", True):
            return checkout
        try:
            result = client.capture_order(checkout)
        except TamaraAPIError as exc:
            if exc.status_code == 409:
                try:
                    details = client.get_order(checkout.tamara_order_id)
                    _verify_remote_order(checkout, details)
                except (TamaraConfigurationError, TamaraAPIError, TamaraVerificationError):
                    logger.exception(
                        "tamara_capture_status_check_failed reference=%s order_id=%s",
                        checkout.merchant_reference,
                        checkout.tamara_order_id,
                    )
                    return checkout
                else:
                    captured_status = normalize_remote_status(details.get("status"))
                    if captured_status in _CAPTURED_STATUSES:
                        return activate_checkout(
                            checkout.pk,
                            status="captured",
                            event_type=f"pull:{captured_status}",
                        )
                    if captured_status in _AUTHORISED_STATUSES:
                        return checkout
            logger.exception(
                "tamara_capture_failed reference=%s order_id=%s",
                checkout.merchant_reference,
                checkout.tamara_order_id,
            )
            return checkout
        except (TamaraConfigurationError, TamaraVerificationError):
            logger.exception(
                "tamara_capture_failed reference=%s order_id=%s",
                checkout.merchant_reference,
                checkout.tamara_order_id,
            )
            return checkout
        captured_status = normalize_remote_status(result.get("status"))
        if captured_status not in _CAPTURED_STATUSES:
            details = client.get_order(checkout.tamara_order_id)
            captured_status = normalize_remote_status(details.get("status"))
        if captured_status in _CAPTURED_STATUSES:
            return activate_checkout(
                checkout.pk,
                status="captured",
                event_type=f"pull:{captured_status}",
            )
        return checkout

    local_terminal = _TERMINAL_STATUS_MAP.get(remote_status)
    if local_terminal:
        return mark_terminal_checkout(
            checkout.pk,
            status=local_terminal,
            event_type=f"pull:{remote_status}",
        )

    if remote_status:
        # Move still-open orders to the back of the reconciliation queue. A
        # permanently-new checkout must not starve later customers forever.
        TamaraCheckout.objects.filter(pk=checkout.pk).update(
            last_event=f"pull:{remote_status}"[:40],
            error_message="",
            updated_at=timezone.now(),
        )
    checkout.refresh_from_db()
    return checkout


def reconcile_pending_checkouts(*, limit: int | None = None) -> ReconciliationResult:
    if not getattr(settings, "TAMARA_ENABLED", False):
        return ReconciliationResult()

    batch_size = max(1, min(100, int(limit or settings.TAMARA_RECONCILIATION_BATCH_SIZE)))
    checkout_ids = list(
        TamaraCheckout.objects.filter(
            status__in=("new", "approved", "authorised"),
        )
        .order_by("updated_at", "id")
        .values_list("id", flat=True)[:batch_size]
    )
    checked = activated = captured = failed = 0
    for checkout_id in checkout_ids:
        try:
            checkout = reconcile_checkout(checkout_id)
        except TamaraVerificationError as exc:
            failed += 1
            logger.warning(
                "tamara_reconciliation_verification_failed checkout_id=%s error=%s",
                checkout_id,
                exc,
            )
            TamaraCheckout.objects.filter(pk=checkout_id).update(
                last_event="reconcile_mismatch",
                error_message=str(exc)[:300],
                updated_at=timezone.now(),
            )
            continue
        except (TamaraConfigurationError, TamaraAPIError):
            failed += 1
            logger.warning("tamara_reconciliation_failed checkout_id=%s", checkout_id)
            TamaraCheckout.objects.filter(pk=checkout_id).update(
                last_event="reconcile_failed",
                updated_at=timezone.now(),
            )
            continue
        checked += 1
        activated += int(bool(checkout.payment_operation_id))
        captured += int(checkout.status == "captured")
    return ReconciliationResult(
        checked=checked,
        activated=activated,
        captured=captured,
        failed=failed,
    )


def touch_worker_heartbeat() -> None:
    cache.set(
        WORKER_HEARTBEAT_KEY,
        {"timestamp": timezone.now().timestamp()},
        timeout=WORKER_HEARTBEAT_TTL_SECONDS,
    )


def worker_is_alive() -> bool:
    heartbeat = cache.get(WORKER_HEARTBEAT_KEY)
    if not isinstance(heartbeat, dict):
        return False
    try:
        age = timezone.now().timestamp() - float(heartbeat["timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    return age <= WORKER_HEARTBEAT_TTL_SECONDS
