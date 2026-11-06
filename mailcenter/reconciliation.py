"""Read provider status only; never resend mail or change customer addresses."""

import time
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import MailMessage
from .services import sync_message_content


def delivery_review_queryset():
    return MailMessage.objects.filter(direction="outbound").filter(
        Q(status__in=["failed", "bounced", "suppressed", "complained", "delivery_delayed"])
        | Q(status="sent", created_at__lt=timezone.now() - timedelta(hours=24))
    )


def reconcile_delivery(*, limit=10, apply=False):
    candidates = delivery_review_queryset().exclude(provider_id__isnull=True).exclude(provider_id="").filter(
        Q(delivery_checked_at__isnull=True) | Q(delivery_checked_at__lt=timezone.now() - timedelta(hours=24))
    ).order_by("delivery_checked_at", "pk")[:max(1, min(limit, 100))]
    result = {"checked": 0, "updated": 0, "unavailable": 0, "dry_run": not apply}
    for message in candidates:
        if not apply:
            result["checked"] += 1
            continue
        old_status = message.status
        if sync_message_content(message):
            message.refresh_from_db()
            result["updated"] += int(message.status != old_status)
        else:
            # Retained as unknown; do not call undelivered mail delivered/failed.
            MailMessage.objects.filter(pk=message.pk).update(delivery_checked_at=timezone.now())
            result["unavailable"] += 1
            # A provider outage/auth/rate-limit failure must not keep the
            # notification worker busy for one timeout per remaining message.
            if not any(code in message.content_fetch_error for code in ("HTTP 404", "HTTP 410")):
                result["checked"] += 1
                break
        result["checked"] += 1
        time.sleep(0.6)
    return result
