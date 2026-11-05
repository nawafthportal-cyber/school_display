from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .client import TelegramAPIError, TelegramClient
from .models import TelegramAlert

logger = logging.getLogger(__name__)

WORKER_HEARTBEAT_KEY = "telegram_alerts:worker:heartbeat"
WORKER_HEARTBEAT_TTL_SECONDS = 180


@dataclass(frozen=True)
class DeliveryResult:
    sent: int = 0
    retried: int = 0
    failed: int = 0


def alerts_enabled() -> bool:
    return bool(getattr(settings, "TELEGRAM_ALERTS_ENABLED", False))


def configuration_errors(*, require_chat_id: bool = True) -> list[str]:
    errors: list[str] = []
    if not str(getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip():
        errors.append("TELEGRAM_BOT_TOKEN is missing")
    if require_chat_id and not str(
        getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", "") or ""
    ).strip():
        errors.append("TELEGRAM_ADMIN_CHAT_ID is missing")
    return errors


def queue_alert(
    *,
    event_type: str,
    dedupe_key: str,
    message: str,
    action_url: str = "",
    action_label: str = "",
) -> tuple[TelegramAlert | None, bool]:
    """Persist an alert exactly once.

    Alerts are only queued when the feature is enabled. Telegram credentials are
    intentionally not required here so a temporary API outage or token rotation
    cannot lose business events.
    """
    if not alerts_enabled():
        return None, False

    alert, created = TelegramAlert.objects.get_or_create(
        dedupe_key=str(dedupe_key)[:255],
        defaults={
            "event_type": str(event_type)[:64],
            "message": str(message),
            "action_url": str(action_url)[:1000],
            "action_label": str(action_label)[:80],
        },
    )
    return alert, created


def enqueue_expiry_reminders(*, on_date: date | None = None) -> int:
    if not alerts_enabled():
        return 0

    from subscriptions.models import SchoolSubscription

    today = on_date or timezone.localdate()
    reminder_days = tuple(getattr(settings, "TELEGRAM_ALERT_EXPIRY_DAYS", ()) or ())
    if not reminder_days:
        return 0

    target_dates = [today + timedelta(days=int(days)) for days in reminder_days]
    subscriptions = (
        SchoolSubscription.objects.filter(
            status="active",
            ends_at__in=target_dates,
        )
        .select_related("school", "plan")
        .order_by("ends_at", "id")
    )

    queued = 0
    for subscription in subscriptions.iterator():
        days_left = (subscription.ends_at - today).days
        if days_left not in reminder_days:
            continue

        if days_left == 0:
            timing = "ينتهي اليوم"
            icon = "🔴"
        elif days_left == 1:
            timing = "متبقي يوم واحد"
            icon = "⚠️"
        else:
            timing = f"متبقي {days_left} يومًا"
            icon = "⏳"

        from .signals import admin_action_url, html_value

        message = (
            f"<b>{icon} اشتراك يقترب من الانتهاء</b>\n\n"
            f"🏫 المدرسة: <b>{html_value(subscription.school)}</b>\n"
            f"📦 الباقة: {html_value(subscription.plan)}\n"
            f"📅 تاريخ الانتهاء: <code>{subscription.ends_at.isoformat()}</code>\n"
            f"⏱ الحالة: <b>{timing}</b>"
        )
        _, created = queue_alert(
            event_type="subscription_expiry",
            dedupe_key=(
                f"subscription-expiry:{subscription.pk}:"
                f"{subscription.ends_at.isoformat()}:{days_left}"
            ),
            message=message,
            action_url=admin_action_url(
                "dashboard:system_subscription_edit",
                kwargs={"pk": subscription.pk},
            ),
            action_label="إدارة الاشتراك",
        )
        queued += int(created)
    return queued


def reset_stale_processing(*, older_than_seconds: int = 300) -> int:
    cutoff = timezone.now() - timedelta(seconds=max(60, older_than_seconds))
    return TelegramAlert.objects.filter(
        status=TelegramAlert.Status.PROCESSING,
        locked_at__lt=cutoff,
    ).update(
        status=TelegramAlert.Status.PENDING,
        locked_at=None,
        available_at=timezone.now(),
        last_error="Recovered after a stale worker lock.",
    )


def _claim_next_alert() -> int | None:
    with transaction.atomic():
        alert = (
            TelegramAlert.objects.select_for_update(skip_locked=True)
            .filter(
                status=TelegramAlert.Status.PENDING,
                available_at__lte=timezone.now(),
            )
            .order_by("created_at", "id")
            .first()
        )
        if alert is None:
            return None

        alert.status = TelegramAlert.Status.PROCESSING
        alert.locked_at = timezone.now()
        alert.attempts += 1
        alert.save(
            update_fields=(
                "status",
                "locked_at",
                "attempts",
                "updated_at",
            )
        )
        return int(alert.pk)


def _retry_delay_seconds(attempts: int) -> int:
    return min(3600, 15 * (2 ** max(0, min(8, attempts - 1))))


def _deliver_claimed(alert_id: int, client: TelegramClient) -> str:
    alert = TelegramAlert.objects.get(pk=alert_id)
    try:
        result = client.send_message(
            message=alert.message,
            action_url=alert.action_url,
            action_label=alert.action_label,
        )
    except TelegramAPIError as exc:
        max_attempts = int(settings.TELEGRAM_ALERT_MAX_ATTEMPTS)
        final_failure = alert.attempts >= max_attempts
        TelegramAlert.objects.filter(
            pk=alert.pk,
            status=TelegramAlert.Status.PROCESSING,
        ).update(
            status=(
                TelegramAlert.Status.FAILED
                if final_failure
                else TelegramAlert.Status.PENDING
            ),
            locked_at=None,
            available_at=timezone.now()
            + timedelta(seconds=_retry_delay_seconds(alert.attempts)),
            last_error=str(exc)[:2000],
        )
        logger.warning(
            "telegram_alert_delivery_failed alert_id=%s attempts=%s final=%s error=%s",
            alert.pk,
            alert.attempts,
            int(final_failure),
            str(exc)[:300],
        )
        return "failed" if final_failure else "retried"

    message_id = result.get("message_id")
    TelegramAlert.objects.filter(
        pk=alert.pk,
        status=TelegramAlert.Status.PROCESSING,
    ).update(
        status=TelegramAlert.Status.SENT,
        locked_at=None,
        sent_at=timezone.now(),
        telegram_message_id=message_id if isinstance(message_id, int) else None,
        last_error="",
    )
    return "sent"


def process_pending_alerts(*, limit: int | None = None) -> DeliveryResult:
    if not alerts_enabled() or configuration_errors():
        return DeliveryResult()

    batch_size = max(1, min(100, int(limit or settings.TELEGRAM_ALERT_BATCH_SIZE)))
    client = TelegramClient()
    sent = retried = failed = 0
    for _ in range(batch_size):
        alert_id = _claim_next_alert()
        if alert_id is None:
            break
        outcome = _deliver_claimed(alert_id, client)
        sent += int(outcome == "sent")
        retried += int(outcome == "retried")
        failed += int(outcome == "failed")
    return DeliveryResult(sent=sent, retried=retried, failed=failed)


def touch_worker_heartbeat(*, worker_id: str, state: str = "running") -> None:
    try:
        cache.set(
            WORKER_HEARTBEAT_KEY,
            {
                "ts": time.time(),
                "worker_id": str(worker_id),
                "state": str(state),
                "enabled": alerts_enabled(),
                "configured": not bool(configuration_errors()),
            },
            timeout=WORKER_HEARTBEAT_TTL_SECONDS,
        )
    except Exception:
        logger.exception("telegram_alert_heartbeat_write_failed")


def worker_status() -> dict[str, object]:
    try:
        payload = cache.get(WORKER_HEARTBEAT_KEY)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return {
            "alive": False,
            "age_sec": None,
            "worker_id": None,
            "enabled": alerts_enabled(),
            "configured": not bool(configuration_errors()),
        }
    try:
        age_sec = max(0.0, time.time() - float(payload.get("ts") or 0.0))
    except (TypeError, ValueError):
        age_sec = None
    return {
        **payload,
        "alive": bool(
            age_sec is not None and age_sec <= WORKER_HEARTBEAT_TTL_SECONDS
        ),
        "age_sec": round(age_sec, 3) if age_sec is not None else None,
    }


def delivery_health_errors() -> list[str]:
    """Do not mistake a live polling loop for successful delivery."""
    errors = []
    now = timezone.now()
    if TelegramAlert.objects.filter(status=TelegramAlert.Status.FAILED).exists():
        errors.append("Telegram alerts have terminal delivery failures; inspect and retry them.")
    if TelegramAlert.objects.filter(
        status=TelegramAlert.Status.PENDING, available_at__lt=now - timedelta(minutes=5)
    ).exists():
        errors.append("Telegram alerts are overdue by more than five minutes.")
    if TelegramAlert.objects.filter(
        status=TelegramAlert.Status.PROCESSING, locked_at__lt=now - timedelta(minutes=5)
    ).exists():
        errors.append("Telegram alert processing is stalled.")
    return errors
