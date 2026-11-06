from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class MailMessage(models.Model):
    """A privacy-aware local index of mail handled by Resend."""

    class Direction(models.TextChoices):
        INBOUND = "inbound", "وارد"
        OUTBOUND = "outbound", "صادر"

    class Status(models.TextChoices):
        QUEUED = "queued", "بانتظار الإرسال"
        SENT = "sent", "تم الإرسال"
        DELIVERED = "delivered", "تم التسليم"
        DELIVERY_DELAYED = "delivery_delayed", "تأخر التسليم"
        BOUNCED = "bounced", "مرتد"
        FAILED = "failed", "فشل"
        COMPLAINED = "complained", "شكوى بريد مزعج"
        SUPPRESSED = "suppressed", "محجوب"
        RECEIVED = "received", "مستلم"

    local_reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    provider_id = models.CharField(max_length=128, unique=True, null=True, blank=True)
    internet_message_id = models.CharField(max_length=500, blank=True, default="")
    direction = models.CharField(max_length=12, choices=Direction.choices, db_index=True)
    status = models.CharField(max_length=24, choices=Status.choices, db_index=True)
    from_address = models.CharField(max_length=500, blank=True, default="")
    to_addresses = models.JSONField(default=list, blank=True)
    cc_addresses = models.JSONField(default=list, blank=True)
    bcc_addresses = models.JSONField(default=list, blank=True)
    reply_to_addresses = models.JSONField(default=list, blank=True)
    subject = models.CharField(max_length=998, blank=True, default="", db_index=True)
    text_body = models.TextField(blank=True, default="")
    html_body = models.TextField(blank=True, default="")
    preview = models.TextField(blank=True, default="")
    is_sensitive = models.BooleanField(default=False)
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    read_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mailcenter_read_messages",
    )
    attachments = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=dict, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    delivery_checked_at = models.DateTimeField(null=True, blank=True)
    provider_created_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    content_fetch_error = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-provider_created_at", "-created_at", "-id")
        indexes = [
            models.Index(
                fields=("direction", "-provider_created_at"),
                name="mail_direction_created_idx",
            ),
            models.Index(
                fields=("direction", "is_read", "-created_at"),
                name="mail_inbox_unread_idx",
            ),
        ]
        verbose_name = "رسالة بريد"
        verbose_name_plural = "رسائل البريد"

    def __str__(self) -> str:
        return self.subject or self.provider_id or str(self.local_reference)

    @property
    def primary_recipient(self) -> str:
        return str(self.to_addresses[0]) if self.to_addresses else ""


class MailWebhookEvent(models.Model):
    """Idempotency and audit trail for signed Resend events."""

    event_id = models.CharField(max_length=160, unique=True)
    event_type = models.CharField(max_length=80, db_index=True)
    message = models.ForeignKey(
        MailMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-occurred_at", "-received_at", "-id")
        verbose_name = "حدث بريد"
        verbose_name_plural = "أحداث البريد"

    def __str__(self) -> str:
        return f"{self.event_type} · {self.event_id}"
