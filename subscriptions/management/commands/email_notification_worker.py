from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from subscriptions.email_notifications import (
    email_notifications_enabled,
    enqueue_expiry_email_reminders,
    process_pending_email_notifications,
    reset_stale_processing,
    touch_worker_heartbeat,
    worker_is_alive,
)
from subscriptions.expiry import expire_due_subscriptions
from subscriptions.invoicing import reconcile_missing_invoices
from subscriptions.trials import close_superseded_trials

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Deliver queued invoice emails and subscription expiry reminders."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--healthcheck", action="store_true")
        parser.add_argument("--interval", type=int, default=None)

    def handle(self, *args, **options):
        if options["healthcheck"]:
            if not email_notifications_enabled():
                self.stdout.write(self.style.SUCCESS("Transactional email is disabled."))
                return
            if not worker_is_alive():
                raise CommandError("Email notification worker is not alive.")
            self.stdout.write(self.style.SUCCESS("Email notification worker is alive."))
            return

        once = bool(options["once"])
        interval = max(
            5,
            min(
                300,
                int(options["interval"] or settings.EMAIL_NOTIFICATION_POLL_INTERVAL_SECONDS),
            ),
        )
        last_expiry_scan_date = None
        last_subscription_expiry_date = None
        last_delivery_review = -3600

        while True:
            state = "running" if email_notifications_enabled() else "disabled"
            touch_worker_heartbeat(state=state)
            try:
                if getattr(settings, "MAIL_DELIVERY_RECONCILE_ENABLED", False) and settings.RESEND_API_KEY and time.monotonic() - last_delivery_review > 3600:
                    from mailcenter.reconciliation import reconcile_delivery
                    summary = reconcile_delivery(limit=10, apply=True)
                    last_delivery_review = time.monotonic()
                    self.stdout.write(f"mail_delivery_review {summary}")
                reconciled = reconcile_missing_invoices()
                if reconciled:
                    self.stdout.write(f"invoice_reconciliation created={reconciled}")

                # Billing state must settle even when email delivery is off,
                # so this runs outside the transactional-email guard below.
                if last_subscription_expiry_date != timezone.localdate():
                    # Retire trials a paid plan already took over first, so the
                    # expiry pass and the reminder scan below both see one
                    # honest term per school.
                    trials = close_superseded_trials()
                    if trials.trimmed:
                        self.stdout.write(
                            f"trial_closure trimmed={trials.trimmed} "
                            f"expired={trials.expired} schools={trials.schools}"
                        )
                    expiry = expire_due_subscriptions()
                    last_subscription_expiry_date = timezone.localdate()
                    if expiry.expired:
                        self.stdout.write(
                            "subscription_expiry "
                            f"expired={expiry.expired} schools_synced={expiry.schools_synced} "
                            f"screens_disabled={expiry.screens_disabled}"
                        )

                if email_notifications_enabled():
                    today = timezone.localdate()
                    if last_expiry_scan_date != today:
                        queued = enqueue_expiry_email_reminders(on_date=today)
                        last_expiry_scan_date = today
                        self.stdout.write(
                            f"email_expiry_scan date={today.isoformat()} queued={queued}"
                        )
                    reset_stale_processing()
                    result = process_pending_email_notifications()
                    if result.sent or result.skipped or result.retried or result.failed:
                        self.stdout.write(
                            "email_delivery "
                            f"sent={result.sent} skipped={result.skipped} "
                            f"retried={result.retried} failed={result.failed}"
                        )
            except Exception:
                logger.exception("email_notification_worker_cycle_failed")

            touch_worker_heartbeat(state=state)
            if once:
                return
            time.sleep(interval)
