from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import MoyasarCheckout, SchoolSubscription, SubscriptionPaymentOperation
from .plan_terms import activate_gateway_plan


logger = logging.getLogger(__name__)

WORKER_HEARTBEAT_KEY = "subscriptions:moyasar:reconciliation:heartbeat"
WORKER_HEARTBEAT_TTL_SECONDS = 300

# Statuses that still deserve a follow-up call to Moyasar.
_OPEN_STATUSES = ("initiated", "authorized")

# Checkouts with no payment id recorded yet (blank or NULL).
_PAYMENT_ID_MISSING = Q(payment_id__isnull=True) | Q(payment_id="")


class MoyasarVerificationError(RuntimeError):
    pass


def amount_to_minor_units(value: Decimal | int | str) -> int:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MoyasarVerificationError("مبلغ عملية الدفع غير صالح.") from exc
    return int(amount * 100)


def _payment_metadata(details: dict) -> dict:
    metadata = details.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _validate_details(checkout: MoyasarCheckout, details: dict) -> tuple[str, str]:
    payment_id = str(details.get("id") or "").strip()
    status = str(details.get("status") or "").strip().lower()
    if not payment_id or len(payment_id) > 80:
        raise MoyasarVerificationError("رقم دفعة ميسر غير صالح.")
    if checkout.payment_id and checkout.payment_id != payment_id:
        raise MoyasarVerificationError("دفعة ميسر لا تطابق الطلب المحلي.")

    amount = details.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise MoyasarVerificationError("تعذر التحقق من مبلغ دفعة ميسر.")
    if amount != amount_to_minor_units(checkout.amount):
        raise MoyasarVerificationError("مبلغ دفعة ميسر لا يطابق قيمة الاشتراك.")
    if str(details.get("currency") or "").upper() != checkout.currency:
        raise MoyasarVerificationError("عملة دفعة ميسر لا تطابق الطلب.")

    metadata = _payment_metadata(details)
    if str(metadata.get("merchant_reference") or "") != checkout.merchant_reference:
        raise MoyasarVerificationError("مرجع دفعة ميسر لا يطابق الطلب.")

    # Moyasar's current Fetch/List Payments response schema does not include a
    # ``live`` member on the payment object.  The API environment is selected
    # by the secret key used for the request, while webhook notifications put
    # ``live`` on the event envelope.  Older responses did include it, so keep
    # validating it when present and otherwise validate the checkout against
    # the configured key environment.
    live_value = details.get("live")
    if live_value is not None:
        if not isinstance(live_value, bool) or live_value != checkout.live_mode:
            raise MoyasarVerificationError("بيئة دفعة ميسر لا تطابق إعدادات الطلب.")
    elif checkout.live_mode != bool(getattr(settings, "MOYASAR_LIVE_MODE", False)):
        raise MoyasarVerificationError("بيئة دفعة ميسر لا تطابق إعدادات الطلب.")
    return payment_id, status


def _current_subscription_for(checkout: MoyasarCheckout):
    """The live subscription a screens-only purchase attaches to.

    A school upgrading mid-trial briefly holds both rows, and the trial can
    outlast the paid term on paper. Attaching the add-on to the trial would
    quietly void screens the customer paid for the moment the trial closes, so
    a paid base plan always wins the tie.
    """
    from .utils import school_active_paid_subscription

    paid = school_active_paid_subscription(checkout.school_id)
    if paid is not None:
        return paid

    today = timezone.localdate()
    return (
        SchoolSubscription.objects.filter(
            school=checkout.school,
            status="active",
            starts_at__lte=today,
        )
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=today))
        .order_by("-ends_at", "-starts_at", "-id")
        .first()
    )


def _grant_purchased_screens(checkout: MoyasarCheckout, subscription) -> None:
    """Turn paid-for extra screens into an effective screen-limit increase."""
    screens = int(getattr(checkout, "extra_screens", 0) or 0)
    if screens <= 0:
        return

    from .access import invalidate_school_subscription_cache
    from .models import SubscriptionScreenAddon

    # Mid-term top-ups run from the purchase date; screens bought alongside a
    # plan run for the whole term.
    starts_at = checkout.starts_at if checkout.request_type != "screens" else timezone.localdate()

    ends_at = checkout.screen_addon_ends_at or subscription.ends_at
    validity_days = getattr(checkout, "screen_addon_validity_days", None)

    SubscriptionScreenAddon.objects.create(
        subscription=subscription,
        screens_added=screens,
        # The customer was charged a fixed figure at checkout; never let the
        # model recompute it into a different number after the fact.
        pricing_strategy="manual_bundle",
        bundle_price=_charged_screen_price(checkout, subscription, starts_at=starts_at),
        starts_at=starts_at,
        validity_days=validity_days,
        ends_at=ends_at,
        status="paid",
        notes=f"Purchased with Moyasar {checkout.merchant_reference}",
    )
    invalidate_school_subscription_cache(checkout.school_id)


def _charged_screen_price(checkout: MoyasarCheckout, subscription, *, starts_at):
    """The screen portion of what the customer actually paid."""
    from .pricing import prorated_screen_addon_price, screen_addon_price

    screens = int(getattr(checkout, "extra_screens", 0) or 0)
    if checkout.request_type == "screens":
        return prorated_screen_addon_price(
            screens,
            plan=checkout.plan,
            starts_at=starts_at,
            ends_at=checkout.screen_addon_ends_at or subscription.ends_at,
        )
    return screen_addon_price(
        screens,
        duration_days=getattr(checkout.plan, "duration_days", None),
    )


def _audit_payment(checkout: MoyasarCheckout, subscription, *, event_type: str) -> None:
    from .audit import record

    reconciled = event_type.startswith("reconcile")
    record(
        "payment_reconciled" if reconciled else "payment_recorded",
        school=checkout.school,
        subscription=subscription,
        actor=checkout.created_by,
        amount=checkout.amount,
        summary=(
            f"تفعيل اشتراك عبر ميسر ({'تسوية آلية' if reconciled else 'دفع مباشر'})"
        ),
        context={
            "gateway": "moyasar",
            "merchant_reference": checkout.merchant_reference,
            "payment_id": checkout.payment_id or "",
            "event": event_type,
            "live_mode": bool(checkout.live_mode),
            "request_type": checkout.request_type,
            "extra_screens": int(getattr(checkout, "extra_screens", 0) or 0),
            "screen_addon_validity_days": int(getattr(checkout, "screen_addon_validity_days", 0) or 0),
            "screen_addon_ends_at": str(getattr(checkout, "screen_addon_ends_at", "") or ""),
        },
    )


def _record_gateway_refund(checkout: MoyasarCheckout, details: dict) -> None:
    """Mirror a refund issued inside Moyasar into our own books.

    Money can leave the merchant account from Moyasar's dashboard without ever
    touching this codebase. Recording it keeps the platform's ledger
    authoritative; revoking access stays a deliberate human decision, exactly
    as :mod:`subscriptions.refunds` describes.
    """
    operation = checkout.payment_operation
    if operation is None:
        return

    refunded_minor = details.get("refunded")
    if isinstance(refunded_minor, bool) or not isinstance(refunded_minor, int) or refunded_minor <= 0:
        # Moyasar did not report a figure; fall back to the full charge.
        remote_total = Decimal(str(checkout.amount))
    else:
        remote_total = (Decimal(refunded_minor) / Decimal("100")).quantize(Decimal("0.01"))

    from .refunds import record_refund, refunded_total

    already = refunded_total(operation)
    delta = remote_total - already
    if delta <= 0:
        return  # already mirrored (webhook retry, or a second reconciliation pass)

    try:
        record_refund(
            operation,
            amount=delta,
            reason="other",
            status="completed",
            notes="استرداد مسجَّل آليًا من ميسر.",
            gateway_reference=str(details.get("id") or checkout.payment_id or "")[:120],
            revoke_access=False,
            actor=None,
        )
    except Exception:
        # A bookkeeping failure must never roll back the payment state itself.
        logger.exception(
            "moyasar_refund_record_failed reference=%s payment_id=%s",
            checkout.merchant_reference,
            checkout.payment_id,
        )


def _activate_paid_checkout(checkout: MoyasarCheckout, *, status: str, event_type: str) -> MoyasarCheckout:
    may_activate = checkout.live_mode or bool(getattr(settings, "MOYASAR_ACTIVATE_TEST_PAYMENTS", False))
    if may_activate:
        operation = checkout.payment_operation
        if operation is not None:
            subscription = operation.subscription
        elif checkout.request_type == "screens":
            # Tops up the running term; never creates or extends a subscription.
            subscription = _current_subscription_for(checkout)
            if subscription is None:
                raise MoyasarVerificationError(
                    "لا يوجد اشتراك ساري لإضافة الشاشات إليه."
                )
        else:
            subscription = activate_gateway_plan(
                school_id=checkout.school_id,
                plan=checkout.plan,
                requested_start=checkout.starts_at,
                notes=f"Paid via Moyasar {checkout.merchant_reference}",
            )

        if operation is None:
            operation = SubscriptionPaymentOperation.objects.create(
                school=checkout.school,
                subscription=subscription,
                plan=checkout.plan,
                amount=checkout.amount,
                method="moyasar",
                source="request",
                created_by=checkout.created_by,
                note=f"Moyasar {checkout.merchant_reference} / {checkout.payment_id}",
            )
            _grant_purchased_screens(checkout, subscription)
            _audit_payment(checkout, subscription, event_type=event_type)
        checkout.subscription = subscription
        checkout.payment_operation = operation

        # كود الخصم يُستهلك فقط عند دفعة مكتملة (idempotent عبر OneToOne).
        try:
            from .discounts import record_redemption

            record_redemption(checkout)
        except Exception:
            logger.exception(
                "discount_redemption_record_failed checkout=%s",
                checkout.merchant_reference,
            )

    checkout.status = status
    checkout.last_event = event_type[:40]
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


def apply_payment_details(checkout_id: int, details: dict, *, event_type: str) -> MoyasarCheckout:
    """Verify amount, currency, reference and environment before changing access."""
    with transaction.atomic():
        checkout = (
            # Lock only the checkout row. ``payment_operation`` is nullable, so
            # PostgreSQL rejects a blanket FOR UPDATE across its outer join.
            MoyasarCheckout.objects.select_for_update(of=("self",))
            .select_related("school", "plan", "created_by", "payment_operation")
            .get(pk=checkout_id)
        )
        payment_id, remote_status = _validate_details(checkout, details)

        other_checkout = MoyasarCheckout.objects.exclude(pk=checkout.pk).filter(payment_id=payment_id).exists()
        if other_checkout:
            raise MoyasarVerificationError("دفعة ميسر مرتبطة بطلب آخر.")
        if checkout.payment_id != payment_id:
            checkout.payment_id = payment_id
            checkout.save(update_fields=["payment_id", "updated_at"])

        if remote_status in {"paid", "captured"}:
            if checkout.payment_operation_id and checkout.status in {"paid", "captured"}:
                return checkout
            return _activate_paid_checkout(checkout, status=remote_status, event_type=event_type)

        if remote_status in {"initiated", "authorized", "failed", "refunded", "voided"}:
            if remote_status == "refunded":
                # The charge was already activated; money has now gone back.
                # Book it before we overwrite the checkout status.
                _record_gateway_refund(checkout, details)

            checkout.status = remote_status
            checkout.last_event = event_type[:40]
            checkout.processed_at = timezone.now() if remote_status != "initiated" else None
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

        raise MoyasarVerificationError("حالة دفعة ميسر غير معروفة.")


@dataclass(frozen=True)
class ReconciliationResult:
    checked: int = 0
    activated: int = 0
    matched: int = 0
    expired: int = 0
    failed: int = 0
    # True only when the ledger sweep ran to completion. An abandoned attempt
    # may not be written off on the strength of a sweep that errored out.
    scan_complete: bool = False

    @property
    def touched(self) -> int:
        return self.checked + self.matched + self.expired + self.failed


def _lookback_window():
    hours = int(getattr(settings, "MOYASAR_RECONCILIATION_LOOKBACK_HOURS", 72) or 72)
    return timezone.now() - timedelta(hours=hours)


def _apply_and_count(checkout_id: int, details: dict, *, event_type: str) -> tuple[bool, bool]:
    """Apply remote details to a checkout. Returns (applied, activated)."""
    try:
        checkout = apply_payment_details(checkout_id, details, event_type=event_type)
    except MoyasarVerificationError as exc:
        # A mismatch here means the remote payment does not belong to this
        # order. Never silently activate; surface it for manual review.
        logger.warning(
            "moyasar_reconciliation_verification_failed checkout_id=%s error=%s",
            checkout_id,
            exc,
        )
        MoyasarCheckout.objects.filter(pk=checkout_id).update(
            last_event="reconcile_mismatch",
            error_message=str(exc)[:300],
            updated_at=timezone.now(),
        )
        return False, False
    return True, bool(checkout.payment_operation_id)


def _reconcile_known_payments(client, checkout_ids: list[int]) -> ReconciliationResult:
    """Re-verify checkouts that already carry a Moyasar payment id."""
    checked = activated = failed = 0
    for checkout_id, payment_id in (
        MoyasarCheckout.objects.filter(pk__in=checkout_ids)
        .exclude(payment_id="")
        .exclude(payment_id=None)
        .values_list("id", "payment_id")
    ):
        try:
            details = client.fetch_payment(payment_id)
        except Exception:
            failed += 1
            logger.warning("moyasar_reconciliation_fetch_failed checkout_id=%s", checkout_id)
            continue
        applied, was_activated = _apply_and_count(checkout_id, details, event_type="reconcile:fetch")
        checked += int(applied)
        activated += int(was_activated)
        failed += int(not applied)
    return ReconciliationResult(checked=checked, activated=activated, failed=failed)


def _reconcile_orphan_payments(client, checkout_ids: list[int]) -> ReconciliationResult:
    """Match paid Moyasar payments back to checkouts that never got a callback.

    This is the case that costs real money: the customer paid, then closed the
    browser before the return URL, and the webhook never arrived.
    """
    rows = list(
        MoyasarCheckout.objects.filter(pk__in=checkout_ids)
        .filter(_PAYMENT_ID_MISSING)
        .values_list("merchant_reference", "id", "created_at")
    )
    if not rows:
        # Nothing to look for, so the ledger question is answered by definition.
        return ReconciliationResult(scan_complete=True)

    pending = {reference: checkout_id for reference, checkout_id, _created in rows}

    max_pages = max(1, min(50, int(getattr(settings, "MOYASAR_RECONCILIATION_MAX_PAGES", 5) or 5)))
    # Reach back far enough to cover the oldest attempt we are about to judge.
    # A fixed window would skip anything the worker missed while it was down,
    # and that attempt would then be voided without ever being checked.
    oldest = min(created for _reference, _id, created in rows)
    created_after = min(_lookback_window(), oldest - timedelta(minutes=5))

    matched = activated = failed = 0
    scan_complete = False
    page = 1
    while pending and page <= max_pages:
        try:
            payments, has_next = client.list_payments(created_after=created_after, page=page)
        except Exception:
            logger.warning("moyasar_reconciliation_list_failed page=%s", page)
            failed += 1
            break

        for details in payments:
            metadata = _payment_metadata(details)
            reference = str(metadata.get("merchant_reference") or "").strip()
            checkout_id = pending.pop(reference, None)
            if checkout_id is None:
                continue
            applied, was_activated = _apply_and_count(
                checkout_id,
                details,
                event_type="reconcile:scan",
            )
            matched += int(applied)
            activated += int(was_activated)
            failed += int(not applied)

        if not has_next:
            # Reached the end of the ledger: every remaining reference really
            # has no payment behind it.
            scan_complete = True
            break
        page += 1
    else:
        # Loop ended without `break`: either everything matched, or we ran out
        # of pages. Only the former is a conclusive answer.
        scan_complete = not pending

    return ReconciliationResult(
        matched=matched,
        activated=activated,
        failed=failed,
        scan_complete=scan_complete,
    )


def _expire_stale_checkouts(scanned_ids: list[int] | None = None) -> int:
    """Close abandoned attempts so they stop being polled forever.

    Only attempts the scan just examined may be closed. Voiding is terminal —
    ``_OPEN_STATUSES`` never revisits it — so closing an attempt the gateway
    was never asked about would strand a real payment permanently.
    """
    if not scanned_ids:
        return 0
    cutoff = _lookback_window()
    return MoyasarCheckout.objects.filter(
        pk__in=scanned_ids,
        status="initiated",
        created_at__lt=cutoff,
    ).filter(_PAYMENT_ID_MISSING).update(
        status="voided",
        last_event="reconcile_expired",
        processed_at=timezone.now(),
        updated_at=timezone.now(),
    )


def reconcile_pending_checkouts(*, limit: int | None = None) -> ReconciliationResult:
    """Bring local checkout state in line with Moyasar's record of truth."""
    if not getattr(settings, "MOYASAR_ENABLED", False):
        return ReconciliationResult()

    from .moyasar import MoyasarClient

    batch_size = max(
        1,
        min(100, int(limit or getattr(settings, "MOYASAR_RECONCILIATION_BATCH_SIZE", 20) or 20)),
    )
    # Oldest-touched first, with no age ceiling: an attempt the worker missed
    # during an outage must still get its turn instead of ageing out unchecked.
    checkout_ids = list(
        MoyasarCheckout.objects.filter(status__in=_OPEN_STATUSES)
        .order_by("updated_at", "id")
        .values_list("id", flat=True)[:batch_size]
    )
    if not checkout_ids:
        return ReconciliationResult()

    try:
        client = MoyasarClient()
    except Exception:
        logger.warning("moyasar_reconciliation_client_unavailable")
        return ReconciliationResult(failed=1)

    known = _reconcile_known_payments(client, checkout_ids)
    orphans = _reconcile_orphan_payments(client, checkout_ids)

    # Expire last, only within what we just asked Moyasar about, and only when
    # the ledger sweep actually finished.
    expired = _expire_stale_checkouts(checkout_ids if orphans.scan_complete else None)

    return ReconciliationResult(
        checked=known.checked,
        matched=orphans.matched,
        activated=known.activated + orphans.activated,
        expired=expired,
        failed=known.failed + orphans.failed,
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
