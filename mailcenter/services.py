from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone as datetime_timezone
from email.utils import parseaddr
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import strip_tags

from .client import ResendAPIError, retrieve_email
from .models import MailMessage, MailWebhookEvent


SENSITIVE_SUBJECT_MARKERS = (
    "استعادة كلمة المرور",
    "إعادة تعيين كلمة المرور",
    "تأكيد بريدك الإلكتروني",
    "رمز تفعيل حسابك",
    "password reset",
    "verify your email",
)

EVENT_STATUS = {
    "email.sent": MailMessage.Status.SENT,
    "email.delivered": MailMessage.Status.DELIVERED,
    "email.delivery_delayed": MailMessage.Status.DELIVERY_DELAYED,
    "email.bounced": MailMessage.Status.BOUNCED,
    "email.failed": MailMessage.Status.FAILED,
    "email.complained": MailMessage.Status.COMPLAINED,
    "email.suppressed": MailMessage.Status.SUPPRESSED,
    "email.received": MailMessage.Status.RECEIVED,
}


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _normalise_tags(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict) and item.get("name"):
                result[str(item["name"])] = str(item.get("value") or "")
        return result
    return {}


def _is_sensitive(subject: str) -> bool:
    folded = (subject or "").casefold()
    return any(marker.casefold() in folded for marker in SENSITIVE_SUBJECT_MARKERS)


def _safe_preview(text_body: str, html_body: str, *, sensitive: bool) -> str:
    if sensitive:
        return "تم حجب محتوى هذه الرسالة الأمنية لحماية روابط التحقق والاستعادة."
    raw = text_body or strip_tags(html_body or "")
    return re.sub(r"\s+", " ", raw).strip()[:500]


def _find_local_outbound(data: dict[str, Any], occurred_at: datetime | None):
    """Match a dashboard-composed message to the first provider event."""
    subject = str(data.get("subject") or "")
    recipients = {item.casefold() for item in _as_list(data.get("to"))}
    if not subject or not recipients:
        return None
    cutoff = (occurred_at or timezone.now()) - timedelta(minutes=20)
    candidates = MailMessage.objects.select_for_update().filter(
        provider_id__isnull=True,
        direction=MailMessage.Direction.OUTBOUND,
        subject=subject,
        created_at__gte=cutoff,
    ).order_by("created_at")[:10]
    for candidate in candidates:
        if {str(item).casefold() for item in candidate.to_addresses} == recipients:
            return candidate
    return None


def _apply_provider_content(message: MailMessage, content: dict[str, Any]) -> None:
    subject = str(content.get("subject") or message.subject or "")[:998]
    sensitive = _is_sensitive(subject)
    text_body = str(content.get("text") or "")
    html_body = str(content.get("html") or "")
    message.subject = subject
    message.from_address = str(content.get("from") or message.from_address or "")[:500]
    message.to_addresses = _as_list(content.get("to")) or message.to_addresses
    message.cc_addresses = _as_list(content.get("cc")) or message.cc_addresses
    message.bcc_addresses = _as_list(content.get("bcc")) or message.bcc_addresses
    message.reply_to_addresses = _as_list(content.get("reply_to")) or message.reply_to_addresses
    message.internet_message_id = str(
        content.get("message_id") or message.internet_message_id or ""
    )[:500]
    message.attachments = content.get("attachments") if isinstance(content.get("attachments"), list) else message.attachments
    message.tags = _normalise_tags(content.get("tags")) or message.tags
    message.is_sensitive = sensitive
    message.preview = _safe_preview(text_body, html_body, sensitive=sensitive)
    if sensitive:
        message.text_body = ""
        message.html_body = ""
    else:
        message.text_body = text_body
        message.html_body = html_body
    message.content_fetch_error = ""
    if message.direction == MailMessage.Direction.OUTBOUND:
        event = str(content.get("last_event") or "").removeprefix("email.")
        status = EVENT_STATUS.get("email." + event)
        if event in {"opened", "clicked"}:
            status = MailMessage.Status.DELIVERED
        terminal = {MailMessage.Status.DELIVERED, MailMessage.Status.BOUNCED, MailMessage.Status.FAILED,
                    MailMessage.Status.COMPLAINED, MailMessage.Status.SUPPRESSED}
        if status and not (message.status in terminal and status in {MailMessage.Status.SENT, MailMessage.Status.QUEUED, MailMessage.Status.DELIVERY_DELAYED}):
            message.status = status
        message.delivery_checked_at = timezone.now()
    message.save()


def sync_message_content(message: MailMessage) -> bool:
    if not message.provider_id:
        return False
    started_at = timezone.now()
    try:
        content = retrieve_email(
            message.provider_id,
            inbound=message.direction == MailMessage.Direction.INBOUND,
        )
    except ResendAPIError as exc:
        message.content_fetch_error = str(exc)[:500]
        message.save(update_fields=("content_fetch_error", "updated_at"))
        return False
    with transaction.atomic():
        message = MailMessage.objects.select_for_update().get(pk=message.pk)
        if message.last_event_at and message.last_event_at > started_at:
            content = dict(content)
            content.pop("last_event", None)
        _apply_provider_content(message, content)
    return True


def process_resend_event(event: dict[str, Any], *, event_id: str) -> tuple[MailMessage, bool]:
    event_type = str(event.get("type") or "").strip()
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    provider_id = str(data.get("email_id") or data.get("id") or "").strip()
    if not event_type.startswith("email.") or not provider_id:
        raise ValueError("Unsupported Resend event")

    occurred_at = _as_datetime(event.get("created_at")) or timezone.now()
    provider_created_at = _as_datetime(data.get("created_at")) or occurred_at
    direction = (
        MailMessage.Direction.INBOUND
        if event_type == "email.received"
        else MailMessage.Direction.OUTBOUND
    )
    status = EVENT_STATUS.get(event_type)

    with transaction.atomic():
        duplicate = MailWebhookEvent.objects.filter(event_id=event_id).first()
        if duplicate is not None and duplicate.message_id:
            return duplicate.message, False

        message = MailMessage.objects.select_for_update().filter(provider_id=provider_id).first()
        if message is None and direction == MailMessage.Direction.OUTBOUND:
            message = _find_local_outbound(data, occurred_at)
        if message is None:
            message = MailMessage(
                provider_id=provider_id,
                direction=direction,
                status=status or MailMessage.Status.SENT,
            )
        else:
            message.provider_id = provider_id

        message.direction = direction
        message.from_address = str(data.get("from") or message.from_address or "")[:500]
        message.to_addresses = _as_list(data.get("to")) or message.to_addresses
        message.cc_addresses = _as_list(data.get("cc")) or message.cc_addresses
        message.bcc_addresses = _as_list(data.get("bcc")) or message.bcc_addresses
        message.reply_to_addresses = _as_list(data.get("reply_to")) or message.reply_to_addresses
        message.subject = str(data.get("subject") or message.subject or "")[:998]
        message.internet_message_id = str(data.get("message_id") or message.internet_message_id or "")[:500]
        message.attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else message.attachments
        message.tags = _normalise_tags(data.get("tags")) or message.tags
        message.provider_created_at = message.provider_created_at or provider_created_at
        if status and (message.last_event_at is None or occurred_at >= message.last_event_at):
            message.status = status
            message.last_event_at = occurred_at
        if event_type == "email.sent":
            message.sent_at = occurred_at
        elif event_type == "email.delivered":
            message.delivered_at = occurred_at
        elif event_type == "email.received":
            message.received_at = occurred_at
        message.is_sensitive = _is_sensitive(message.subject)
        if not message.preview:
            message.preview = _safe_preview("", "", sensitive=message.is_sensitive)
        message.save()

        MailWebhookEvent.objects.create(
            event_id=event_id,
            event_type=event_type,
            message=message,
            occurred_at=occurred_at,
            payload={"type": event_type, "created_at": event.get("created_at"), "data": data},
        )

    # Content retrieval must never make webhook acknowledgement non-idempotent.
    # A failed fetch is recorded and can be retried from the platform dashboard.
    if event_type in {"email.sent", "email.received"}:
        sync_message_content(message)
    return message, True


def sender_address(value: str) -> str:
    return parseaddr(value or "")[1] or (value or "")
