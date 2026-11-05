from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from telegram_alerts.services import (
    alerts_enabled,
    configuration_errors,
    enqueue_expiry_reminders,
    process_pending_alerts,
    reset_stale_processing,
    touch_worker_heartbeat,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the reliable Telegram alert delivery and expiry-reminder worker."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=int(settings.TELEGRAM_ALERT_POLL_INTERVAL_SECONDS),
            help="Seconds between queue scans.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=int(settings.TELEGRAM_ALERT_BATCH_SIZE),
            help="Maximum alerts delivered per scan.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one scan and exit.",
        )

    def handle(self, *args, **options):
        interval = max(2, min(300, int(options["interval"])))
        batch_size = max(1, min(100, int(options["batch_size"])))
        once = bool(options["once"])
        worker_id = f"telegram-alert-worker:{int(time.time())}"
        last_expiry_scan_date = None

        self.stdout.write(
            f"telegram_alert_worker_started interval={interval}s batch_size={batch_size}"
        )

        while True:
            state = "disabled" if not alerts_enabled() else "starting"
            errors = configuration_errors()
            if alerts_enabled() and errors:
                state = "misconfigured"
            try:
                if alerts_enabled():
                    today = timezone.localdate()
                    if last_expiry_scan_date != today:
                        queued = enqueue_expiry_reminders(on_date=today)
                        last_expiry_scan_date = today
                        self.stdout.write(
                            f"telegram_expiry_scan date={today.isoformat()} queued={queued}"
                        )

                    reset_stale_processing()
                    if errors:
                        logger.error(
                            "telegram_alert_worker_misconfigured errors=%s",
                            ",".join(errors),
                        )
                    else:
                        result = process_pending_alerts(limit=batch_size)
                        state = "degraded" if result.retried or result.failed else "running"
                        if result.sent or result.retried or result.failed:
                            self.stdout.write(
                                "telegram_alert_delivery "
                                f"sent={result.sent} retried={result.retried} failed={result.failed}"
                            )
            except Exception:
                state = "error"
                logger.exception("telegram_alert_worker_scan_failed")

            touch_worker_heartbeat(worker_id=worker_id, state=state)
            if once:
                return
            time.sleep(interval)
