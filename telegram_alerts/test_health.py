from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.core.cache import cache
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from telegram_alerts.admin import TelegramAlertAdmin
from telegram_alerts.models import TelegramAlert
from telegram_alerts.services import DeliveryResult, delivery_health_errors, touch_worker_heartbeat, worker_status


@override_settings(TELEGRAM_ALERTS_ENABLED=True, TELEGRAM_BOT_TOKEN="test", TELEGRAM_ADMIN_CHAT_ID="test")
class TelegramHealthTests(TestCase):
    def setUp(self):
        cache.clear()

    def _scan(self, **kwargs):
        with patch("telegram_alerts.management.commands.telegram_alert_worker.enqueue_expiry_reminders", return_value=0), \
             patch("telegram_alerts.management.commands.telegram_alert_worker.reset_stale_processing", return_value=0), \
             patch("telegram_alerts.management.commands.telegram_alert_worker.process_pending_alerts", **kwargs):
            call_command("telegram_alert_worker", once=True, stdout=StringIO())

    def test_processing_exception_is_unhealthy_despite_live_heartbeat(self):
        self._scan(side_effect=RuntimeError("processing failed"))
        self.assertTrue(worker_status()["alive"])
        self.assertEqual(worker_status()["state"], "error")
        with self.assertRaises(CommandError):
            call_command("telegram_alert_health", stdout=StringIO())

    def test_retrying_delivery_is_degraded(self):
        self._scan(return_value=DeliveryResult(retried=1))
        self.assertEqual(worker_status()["state"], "degraded")
        with self.assertRaises(CommandError):
            call_command("telegram_alert_health", stdout=StringIO())

    def test_successful_scan_recovers_health(self):
        self._scan(side_effect=RuntimeError("temporary"))
        self._scan(return_value=DeliveryResult())
        call_command("telegram_alert_health", stdout=StringIO())

    def test_terminal_failure_is_not_hidden_by_idle_worker(self):
        TelegramAlert.objects.create(event_type="test", dedupe_key="failed", status="failed", message="test")
        touch_worker_heartbeat(worker_id="test", state="running")
        with self.assertRaises(CommandError):
            call_command("telegram_alert_health", stdout=StringIO())

    def test_overdue_pending_and_stalled_processing_are_unhealthy(self):
        TelegramAlert.objects.create(
            event_type="test", dedupe_key="pending", message="test",
            available_at=timezone.now() - timedelta(minutes=6),
        )
        TelegramAlert.objects.create(
            event_type="test", dedupe_key="processing", message="test", status="processing",
            locked_at=timezone.now() - timedelta(minutes=6),
        )
        self.assertEqual(len(delivery_health_errors()), 2)

    def test_future_retry_is_not_overdue(self):
        TelegramAlert.objects.create(
            event_type="test", dedupe_key="future", message="test", attempts=1,
            available_at=timezone.now() + timedelta(minutes=10),
        )
        self.assertEqual(delivery_health_errors(), [])

    def test_admin_retry_does_not_resend_successful_messages(self):
        failed = TelegramAlert.objects.create(
            event_type="test", dedupe_key="failed", message="test", status="failed", attempts=10,
        )
        sent = TelegramAlert.objects.create(
            event_type="test", dedupe_key="sent", message="test", status="sent", attempts=1,
        )
        admin = TelegramAlertAdmin(TelegramAlert, AdminSite())
        with patch.object(admin, "message_user"):
            admin.retry_failed(None, TelegramAlert.objects.all())
        failed.refresh_from_db(); sent.refresh_from_db()
        self.assertEqual((failed.status, failed.attempts), ("pending", 0))
        self.assertEqual(sent.status, "sent")
