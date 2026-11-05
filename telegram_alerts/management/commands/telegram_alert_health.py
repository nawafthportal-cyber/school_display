from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from telegram_alerts.services import (
    alerts_enabled,
    configuration_errors,
    delivery_health_errors,
    worker_status,
)


class Command(BaseCommand):
    help = "Check Telegram alert worker liveness and configuration."

    def handle(self, *args, **options):
        if not alerts_enabled():
            self.stdout.write(self.style.SUCCESS("Telegram alerts are disabled."))
            return

        errors = configuration_errors()
        if errors:
            raise CommandError("; ".join(errors))

        status = worker_status()
        if not bool(status.get("alive")):
            raise CommandError(
                f"Telegram alert worker is not alive (age_sec={status.get('age_sec')})."
            )

        if status.get("state") != "running":
            raise CommandError("Telegram alert worker has not completed a successful delivery scan.")

        queue_errors = delivery_health_errors()
        if queue_errors:
            raise CommandError("; ".join(queue_errors))

        self.stdout.write(
            self.style.SUCCESS(
                "Telegram alert worker is alive "
                f"(age_sec={status.get('age_sec')}, worker_id={status.get('worker_id')})."
            )
        )
