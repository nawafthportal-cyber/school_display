import json
import re
from datetime import datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.http import JsonResponse
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from core.middleware import DisplayTokenMiddleware, SecurityHeadersMiddleware
from core.display_presence import display_is_live, latest_display_presence, touch_display_presence
from core.models import (
    DisplayScreen,
    School,
    ScreenOutage,
    ScreenWeeklyUptimeReport,
    SubscriptionPlan,
)
from core.screen_diagnostics import (
    CAUSE_DEVICE_OFF,
    CAUSE_NEVER_CONNECTED,
    CAUSE_PLATFORM,
    CAUSE_SCHOOL_CLOSED,
    CAUSE_SCHOOL_NETWORK,
    SCOPE_SCHOOL,
    read_disconnect_signals,
    record_disconnect_signal,
)
from core.screen_monitoring import (
    CONFIRMATION_SECONDS,
    SUPPRESSED_AFTER_HOURS,
    SUPPRESSED_BEFORE_GRACE,
    SUPPRESSED_COOLDOWN,
    SUPPRESSED_HOLIDAY,
    SUPPRESSED_OUTSIDE_SCHOOL_DAY,
    SUPPRESSED_PLATFORM,
    prune_operational_data,
    scan_screens,
    send_weekly_uptime_reports,
)
from schedule.models import DaySchedule, Period, SchoolClosure, SchoolSettings
from subscriptions.models import SchoolSubscription
from telegram_alerts.models import TelegramAlert


class DisplayPresenceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة الحضور", slug="presence-school")
        self.screen = DisplayScreen.objects.create(
            name="الشاشة الرئيسية",
            school=self.school,
            is_active=True,
            bound_device_id="device-1",
        )

    def test_touch_updates_current_last_seen_field_and_live_state(self):
        seen_at = timezone.now()

        touch_display_presence(self.screen.pk, token=self.screen.token, seen_at=seen_at)

        self.screen.refresh_from_db()
        self.assertIsNotNone(self.screen.last_seen)
        self.assertAlmostEqual(self.screen.last_seen.timestamp(), seen_at.timestamp(), delta=1)
        self.assertAlmostEqual(latest_display_presence(self.screen).timestamp(), seen_at.timestamp(), delta=1)
        self.assertTrue(display_is_live(self.screen, now=seen_at))

    def test_cached_heartbeat_is_newer_than_throttled_database_value(self):
        first_seen = timezone.now()
        later_seen = first_seen + timedelta(seconds=10)
        touch_display_presence(self.screen.pk, token=self.screen.token, seen_at=first_seen)
        touch_display_presence(self.screen.pk, token=self.screen.token, seen_at=later_seen)

        self.screen.refresh_from_db()

        self.assertAlmostEqual(self.screen.last_seen.timestamp(), first_seen.timestamp(), delta=1)
        self.assertAlmostEqual(latest_display_presence(self.screen).timestamp(), later_seen.timestamp(), delta=1)

    def test_the_persisted_heartbeat_stays_fresher_than_the_strictest_alert_threshold(self):
        """The column the monitor falls back to when the cache is cold.

        A school may set its offline threshold to five minutes. If presence were
        written to the database less often than that, the first scan after a
        Redis restart would read a stale column and declare a wall of healthy
        screens dead.
        """
        from core.display_presence import _db_touch_interval_seconds

        strictest_threshold_sec = 5 * 60

        self.assertLess(_db_touch_interval_seconds(), strictest_threshold_sec)

    @override_settings(DISPLAY_LAST_SEEN_DB_INTERVAL_SEC=900)
    def test_the_ceiling_holds_even_if_the_environment_asks_for_more(self):
        from core.display_presence import LAST_SEEN_DB_INTERVAL_CEILING_SEC, _db_touch_interval_seconds

        self.assertEqual(_db_touch_interval_seconds(), LAST_SEEN_DB_INTERVAL_CEILING_SEC)


@override_settings(
    # Screen faults reach the platform administrator over Telegram and nowhere
    # else, so a queued alert row is the only observable these tests have.
    TELEGRAM_ALERTS_ENABLED=True,
    # These suites are about monitoring, not billing.
    DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION=False,
)
class ScreenMonitoringTests(TestCase):
    def setUp(self):
        # Cooldowns, daily caps and disconnect signals all live in the cache.
        cache.clear()
        self.school = School.objects.create(name="مدرسة المراقبة", slug="monitoring-school")
        self.settings = SchoolSettings.objects.create(
            school=self.school,
            name=self.school.name,
            screen_offline_threshold_minutes=5,
            screen_offline_alerts_enabled=True,
            weekly_uptime_report_enabled=True,
            # This school has no timetable, so the school-hours gate would
            # suppress everything. Tests that exercise the gate turn it back on.
            screen_offline_school_hours_only=False,
        )
        self.screen = DisplayScreen.objects.create(
            school=self.school,
            name="شاشة المدخل",
            is_active=True,
            bound_device_id="monitor-device",
            last_seen=timezone.now() - timedelta(minutes=20),
        )

    # A screen that has been offline long enough needs two scans before anyone
    # hears about it, so most tests want the second pass.
    def _confirm_and_scan(self, now):
        scan_screens(now=now)
        return scan_screens(now=now + timedelta(seconds=CONFIRMATION_SECONDS + 1))

    def test_offline_scan_alerts_once_then_resolves_after_reconnect(self):
        now = timezone.now()
        first = scan_screens(now=now)
        second = scan_screens(now=now + timedelta(seconds=CONFIRMATION_SECONDS + 1))
        third = scan_screens(now=now + timedelta(minutes=3))

        self.assertEqual(first["opened"], 1)
        self.assertEqual(first["alerted"], 0)
        self.assertEqual(first["suppressed"], 1)
        self.assertEqual(second["alerted"], 1)
        self.assertEqual(third["opened"], 0)
        self.assertEqual(third["alerted"], 0)
        self.assertEqual(TelegramAlert.objects.filter(event_type="screen_offline").count(), 1)
        outage = ScreenOutage.objects.get(screen=self.screen)
        self.assertIsNotNone(outage.alert_sent_at)
        self.assertEqual(outage.alert_count, 1)

        self.screen.last_seen = now + timedelta(minutes=4)
        self.screen.save(update_fields=("last_seen",))
        result = scan_screens(now=now + timedelta(minutes=4))
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(result["recovered"], 1)
        outage.refresh_from_db()
        self.assertIsNotNone(outage.resolved_at)
        self.assertIsNotNone(outage.recovery_notified_at)
        recovery = TelegramAlert.objects.get(event_type="screen_recovered")
        self.assertIn("عادت", recovery.message)

    @override_settings(DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION=True)
    def test_lapsed_subscription_screens_are_not_reported_as_faulty(self):
        # The platform blacks these screens out on purpose; calling that an
        # outage would be both wrong and unkind.
        result = self._confirm_and_scan(timezone.now())

        self.assertEqual(result["opened"], 0)
        self.assertEqual(result["alerted"], 0)
        self.assertFalse(TelegramAlert.objects.exists())

    def test_newly_bound_screen_waits_for_the_configured_threshold(self):
        self.screen.last_seen = None
        self.screen.bound_at = timezone.now()
        self.screen.save(update_fields=("last_seen", "bound_at"))

        early = scan_screens(now=self.screen.bound_at + timedelta(minutes=4))
        late = self._confirm_and_scan(self.screen.bound_at + timedelta(minutes=6))

        self.assertEqual(early["opened"], 0)
        self.assertEqual(early["alerted"], 0)
        self.assertEqual(late["alerted"], 1)
        outage = ScreenOutage.objects.get(screen=self.screen)
        self.assertEqual(outage.cause, CAUSE_NEVER_CONNECTED)

    def test_outage_outside_the_school_day_is_recorded_but_never_sent(self):
        self.settings.screen_offline_school_hours_only = True
        self.settings.save(update_fields=("screen_offline_school_hours_only",))
        now = timezone.now()

        result = self._confirm_and_scan(now)

        self.assertEqual(result["alerted"], 0)
        self.assertEqual(result["suppressed"], 1)
        outage = ScreenOutage.objects.get(screen=self.screen)
        self.assertEqual(outage.suppressed_reason, SUPPRESSED_OUTSIDE_SCHOOL_DAY)
        self.assertFalse(TelegramAlert.objects.exists())

    def test_screen_marked_always_on_still_alerts_outside_school_hours(self):
        self.settings.screen_offline_school_hours_only = True
        self.settings.save(update_fields=("screen_offline_school_hours_only",))
        self.screen.monitor_always_on = True
        self.screen.save(update_fields=("monitor_always_on",))

        result = self._confirm_and_scan(timezone.now())

        self.assertEqual(result["alerted"], 1)

    def test_second_outage_within_the_cooldown_is_not_sent_again(self):
        now = timezone.now()
        self._confirm_and_scan(now)
        self.assertEqual(TelegramAlert.objects.filter(event_type="screen_offline").count(), 1)

        # Back briefly, then offline again — a classic flap.
        self.screen.last_seen = now + timedelta(minutes=3)
        self.screen.save(update_fields=("last_seen",))
        scan_screens(now=now + timedelta(minutes=3))
        self.screen.last_seen = now + timedelta(minutes=3)
        self.screen.save(update_fields=("last_seen",))
        later = now + timedelta(minutes=20)
        scan_screens(now=later)
        result = scan_screens(now=later + timedelta(seconds=CONFIRMATION_SECONDS + 1))

        self.assertEqual(result["alerted"], 0)
        self.assertEqual(result["suppressed"], 1)
        self.assertEqual(
            ScreenOutage.objects.filter(screen=self.screen, resolved_at__isnull=True)
            .first()
            .suppressed_reason,
            SUPPRESSED_COOLDOWN,
        )

    def test_whole_school_outage_becomes_one_message_naming_the_shared_cause(self):
        for index in range(2):
            DisplayScreen.objects.create(
                school=self.school,
                name=f"شاشة إضافية {index}",
                is_active=True,
                bound_device_id=f"extra-device-{index}",
                last_seen=self.screen.last_seen + timedelta(seconds=index * 5),
            )

        result = self._confirm_and_scan(timezone.now())

        self.assertEqual(result["alerted"], 3)
        alerts = TelegramAlert.objects.filter(event_type="screen_offline")
        self.assertEqual(alerts.count(), 1)
        self.assertIn("انقطاع على مستوى المدرسة (3 شاشات)", alerts.first().message)
        for outage in ScreenOutage.objects.all():
            self.assertEqual(outage.scope, SCOPE_SCHOOL)
            self.assertEqual(outage.cause, CAUSE_SCHOOL_NETWORK)

    def test_clean_websocket_close_reports_the_device_as_switched_off(self):
        record_disconnect_signal(self.screen.pk, source="ws_close", code=1001)

        self._confirm_and_scan(timezone.now())

        outage = ScreenOutage.objects.get(screen=self.screen)
        self.assertEqual(outage.cause, CAUSE_DEVICE_OFF)
        self.assertEqual(outage.close_code, 1001)
        alert = TelegramAlert.objects.get(event_type="screen_offline")
        self.assertIn("مطفأ", alert.message)

    def test_daily_cap_stops_the_fourth_alert_for_the_same_screen(self):
        now = timezone.make_aware(
            datetime.combine(timezone.localdate(), dt_time(hour=9)),
            timezone.get_current_timezone(),
        )
        self.settings.screen_offline_cooldown_minutes = 10
        self.settings.screen_offline_max_alerts_per_day = 2
        self.settings.save(
            update_fields=(
                "screen_offline_cooldown_minutes",
                "screen_offline_max_alerts_per_day",
            )
        )

        for attempt in range(3):
            moment = now + timedelta(minutes=attempt * 30)
            self.screen.last_seen = moment - timedelta(minutes=20)
            self.screen.save(update_fields=("last_seen",))
            ScreenOutage.objects.filter(screen=self.screen).update(resolved_at=moment)
            scan_screens(now=moment)
            scan_screens(now=moment + timedelta(seconds=CONFIRMATION_SECONDS + 1))

        self.assertEqual(TelegramAlert.objects.filter(event_type="screen_offline").count(), 2)

    def test_weekly_report_is_a_single_admin_alert(self):
        week_start = timezone.localdate() - timedelta(days=14)
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(week_start, datetime.min.time()), tz)
        ScreenOutage.objects.create(
            screen=self.screen,
            detected_at=start + timedelta(days=1),
            resolved_at=start + timedelta(days=2),
        )
        second_school = School.objects.create(
            name="مدرسة النور",
            slug="alnoor-school",
        )
        SchoolSettings.objects.create(
            school=second_school,
            name=second_school.name,
            weekly_uptime_report_enabled=False,
        )
        second_screen = DisplayScreen.objects.create(
            school=second_school,
            name="شاشة الساحة",
            is_active=True,
            bound_device_id="second-monitor-device",
        )

        result = send_weekly_uptime_reports(week_start=week_start)
        repeated = send_weekly_uptime_reports(week_start=week_start)

        self.assertEqual(result["schools_sent"], 2)
        self.assertEqual(repeated["schools_sent"], 0)
        report = ScreenWeeklyUptimeReport.objects.get(screen=self.screen, week_start=week_start)
        self.assertEqual(report.offline_seconds, 24 * 60 * 60)
        self.assertAlmostEqual(float(report.uptime_percent), 85.71, places=2)
        self.assertIsNotNone(report.sent_at)
        self.assertIsNotNone(
            ScreenWeeklyUptimeReport.objects.get(
                screen=second_screen,
                week_start=week_start,
            ).sent_at
        )
        alert = TelegramAlert.objects.get(event_type="screen_uptime_weekly")
        self.assertIn(self.school.name, alert.message)
        self.assertIn(second_school.name, alert.message)
        self.assertIn(self.screen.name, alert.message)
        self.assertIn(second_screen.name, alert.message)

    def test_weekly_report_counts_offline_time_from_last_seen(self):
        week_start = timezone.localdate() - timedelta(days=21)
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(week_start, datetime.min.time()), tz)
        ScreenOutage.objects.create(
            screen=self.screen,
            last_seen_at=start + timedelta(hours=12),
            detected_at=start + timedelta(hours=12, minutes=5),
            resolved_at=start + timedelta(hours=13),
        )

        send_weekly_uptime_reports(week_start=week_start)

        report = ScreenWeeklyUptimeReport.objects.get(screen=self.screen, week_start=week_start)
        self.assertEqual(report.offline_seconds, 60 * 60)

    @override_settings(TELEGRAM_ALERTS_ENABLED=False)
    def test_weekly_report_is_not_marked_sent_while_telegram_is_off(self):
        # Telegram is the only channel now, so a report that could not be queued
        # must stay unsent and be picked up again on the next run.
        week_start = timezone.localdate() - timedelta(days=28)

        result = send_weekly_uptime_reports(week_start=week_start)

        report = ScreenWeeklyUptimeReport.objects.get(
            screen=self.screen,
            week_start=week_start,
        )
        self.assertEqual(result["schools_sent"], 0)
        self.assertIsNone(report.sent_at)
        self.assertFalse(TelegramAlert.objects.exists())


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="alerts@example.com",
    TELEGRAM_ALERTS_ENABLED=False,
    # These suites are about monitoring, not billing.
    DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION=False,
)
class ScreenMonitorScanCostTests(TestCase):
    """The scan runs every minute, for every screen, forever.

    It used to open a transaction and take a row lock for each screen on every
    pass — including the healthy ones, which are almost all of them. That is the
    load that grows with the fleet even when nothing is wrong, so its cost per
    healthy screen has to be zero, and it has to stay zero.
    """

    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة الكلفة", slug="scan-cost-school")
        self.settings = SchoolSettings.objects.create(
            school=self.school,
            name=self.school.name,
            screen_offline_threshold_minutes=5,
            screen_offline_alerts_enabled=True,
            screen_offline_school_hours_only=False,
        )

    def _screen(self, name, *, last_seen):
        return DisplayScreen.objects.create(
            school=self.school,
            name=name,
            is_active=True,
            bound_device_id=f"device-{name}",
            last_seen=last_seen,
        )

    def _query_count(self, now):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            scan_screens(now=now)
        return len(captured.captured_queries)

    def test_healthy_screens_do_not_scale_the_query_count(self):
        now = timezone.now()
        self._screen("شاشة ١", last_seen=now)
        scan_screens(now=now)  # warm the per-school caches first

        one_screen = self._query_count(now)
        for index in range(2, 7):
            self._screen(f"شاشة {index}", last_seen=now)
        six_screens = self._query_count(now)

        self.assertEqual(
            six_screens,
            one_screen,
            "a healthy screen still costs the scan a query; the fleet-sized load is back",
        )

    def test_a_screen_that_recovers_is_still_resolved(self):
        """The skip must never apply to a screen carrying an open outage."""
        now = timezone.now()
        screen = self._screen("شاشة متعطلة", last_seen=now - timedelta(minutes=20))

        opened = scan_screens(now=now)
        self.assertEqual(opened["opened"], 1)

        screen.last_seen = now + timedelta(minutes=1)
        screen.save(update_fields=("last_seen",))
        recovered = scan_screens(now=now + timedelta(minutes=1))

        self.assertEqual(recovered["resolved"], 1)
        self.assertIsNotNone(ScreenOutage.objects.get(screen=screen).resolved_at)

    def test_an_offline_screen_is_still_detected_among_healthy_ones(self):
        now = timezone.now()
        self._screen("سليمة", last_seen=now)
        broken = self._screen("معطلة", last_seen=now - timedelta(minutes=30))

        result = scan_screens(now=now)

        self.assertEqual(result["opened"], 1)
        self.assertTrue(ScreenOutage.objects.filter(screen=broken, resolved_at__isnull=True).exists())


class OperationalRetentionTests(TestCase):
    """Two append-only logs had no retention policy at all."""

    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة التقليم", slug="prune-school")
        self.screen = DisplayScreen.objects.create(
            school=self.school,
            name="شاشة",
            is_active=True,
            bound_device_id="prune-device",
        )
        self.now = timezone.now()
        self.old = self.now - timedelta(days=400)

    def _outage(self, *, detected_at, resolved_at):
        return ScreenOutage.objects.create(
            screen=self.screen, detected_at=detected_at, resolved_at=resolved_at
        )

    def _alert(self, *, key, status, updated_at):
        alert = TelegramAlert.objects.create(
            event_type="screen-offline", dedupe_key=key, message="x", status=status
        )
        # `updated_at` is auto_now, so it can only be set after the fact.
        TelegramAlert.objects.filter(pk=alert.pk).update(updated_at=updated_at)
        return alert

    def test_closed_outages_past_retention_are_removed(self):
        stale = self._outage(detected_at=self.old, resolved_at=self.old + timedelta(hours=1))

        result = prune_operational_data(now=self.now, retention_days=180)

        self.assertEqual(result["outages"], 1)
        self.assertFalse(ScreenOutage.objects.filter(pk=stale.pk).exists())

    def test_an_open_outage_is_never_removed_however_old(self):
        """It is live state, not history — deleting it loses a running incident."""
        open_outage = self._outage(detected_at=self.old, resolved_at=None)

        prune_operational_data(now=self.now, retention_days=180)

        self.assertTrue(ScreenOutage.objects.filter(pk=open_outage.pk).exists())

    def test_recent_history_is_kept(self):
        recent = self._outage(
            detected_at=self.now - timedelta(days=3), resolved_at=self.now - timedelta(days=3)
        )

        prune_operational_data(now=self.now, retention_days=180)

        self.assertTrue(ScreenOutage.objects.filter(pk=recent.pk).exists())

    def test_delivered_alerts_are_removed_but_queued_ones_are_not(self):
        sent = self._alert(key="sent-old", status=TelegramAlert.Status.SENT, updated_at=self.old)
        failed = self._alert(key="failed-old", status=TelegramAlert.Status.FAILED, updated_at=self.old)
        pending = self._alert(key="pending-old", status=TelegramAlert.Status.PENDING, updated_at=self.old)
        processing = self._alert(
            key="processing-old", status=TelegramAlert.Status.PROCESSING, updated_at=self.old
        )

        result = prune_operational_data(now=self.now, retention_days=180)

        self.assertEqual(result["alerts"], 2)
        self.assertFalse(TelegramAlert.objects.filter(pk__in=[sent.pk, failed.pk]).exists())
        self.assertEqual(
            set(TelegramAlert.objects.values_list("pk", flat=True)),
            {pending.pk, processing.pk},
            "an undelivered alert was deleted; that is a lost message",
        )

    def test_a_large_backlog_is_cleared_across_batches(self):
        """The first run faces everything the system ever recorded."""
        for index in range(5):
            self._outage(
                detected_at=self.old, resolved_at=self.old + timedelta(minutes=index)
            )

        with patch("core.screen_monitoring.PRUNE_BATCH_SIZE", 2):
            result = prune_operational_data(now=self.now, retention_days=180)

        self.assertEqual(result["outages"], 5)
        self.assertEqual(ScreenOutage.objects.count(), 0)


@override_settings(
    TELEGRAM_ALERTS_ENABLED=True,
    DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION=False,
)
class ScreenSchoolHoursGateTests(TestCase):
    """The headline fix: a television switched off after dismissal is not a fault."""

    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة الدوام", slug="hours-school")
        self.settings = SchoolSettings.objects.create(
            school=self.school,
            name=self.school.name,
            screen_offline_threshold_minutes=5,
            screen_offline_alerts_enabled=True,
            screen_offline_school_hours_only=True,
            screen_offline_grace_minutes=15,
        )

        # A school day running 08:00 → 12:00 gives an active window of
        # 07:30 → 12:15 (30 minutes before the first period, 15 after the last).
        self.today = timezone.localdate()
        day = DaySchedule.objects.create(
            settings=self.settings,
            weekday=self.today.weekday() + 1,
            is_active=True,
            periods_count=2,
        )
        Period.objects.create(day=day, index=1, starts_at=dt_time(8, 0), ends_at=dt_time(10, 0))
        Period.objects.create(day=day, index=2, starts_at=dt_time(10, 0), ends_at=dt_time(12, 0))

        self.screen = DisplayScreen.objects.create(
            school=self.school,
            name="شاشة الإدارة",
            is_active=True,
            bound_device_id="hours-device",
        )

    def _at(self, hour, minute=0):
        tz = timezone.get_current_timezone()
        return timezone.make_aware(datetime.combine(self.today, dt_time(hour, minute)), tz)

    def _scan_twice(self, moment):
        self.screen.last_seen = moment - timedelta(minutes=30)
        self.screen.save(update_fields=("last_seen",))
        scan_screens(now=moment)
        return scan_screens(now=moment + timedelta(seconds=CONFIRMATION_SECONDS + 1))

    def test_outage_during_lessons_alerts_the_administrator(self):
        result = self._scan_twice(self._at(10, 0))

        self.assertEqual(result["alerted"], 1)
        self.assertEqual(TelegramAlert.objects.filter(event_type="screen_offline").count(), 1)

    def test_outage_after_dismissal_is_logged_without_a_message(self):
        result = self._scan_twice(self._at(20, 0))

        self.assertEqual(result["alerted"], 0)
        self.assertFalse(TelegramAlert.objects.exists())
        outage = ScreenOutage.objects.get(screen=self.screen)
        self.assertEqual(outage.suppressed_reason, SUPPRESSED_AFTER_HOURS)
        # Still recorded, so the weekly uptime report stays accurate.
        self.assertIsNotNone(outage.detected_at)

    def test_grace_period_covers_the_start_of_the_school_day(self):
        # Window opens 07:30; the 15-minute grace pushes the first alert to 07:45.
        result = self._scan_twice(self._at(7, 35))

        self.assertEqual(result["alerted"], 0)
        self.assertEqual(
            ScreenOutage.objects.get(screen=self.screen).suppressed_reason,
            SUPPRESSED_BEFORE_GRACE,
        )


@override_settings(
    TELEGRAM_ALERTS_ENABLED=True,
    DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION=False,
)
class ScreenHolidayGateTests(TestCase):
    """إجازةٌ بتاريخها ليست عطلاً.

    الجداول تعرف أيام الأسبوع فقط، فيوم العيد الواقع على «الأحد» كان يوم دوام
    في نظر النظام: شاشتان أُطفئتا معاً تُقرآن انقطاع كهرباء، فيصل المدير تنبيهٌ
    صحيح تقنياً وخاطئ عملياً — وتكراره يعلّمه تجاهل التنبيهات كلها.
    """

    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة الإجازة", slug="holiday-school")
        self.settings = SchoolSettings.objects.create(
            school=self.school,
            name=self.school.name,
            screen_offline_threshold_minutes=5,
            screen_offline_alerts_enabled=True,
            screen_offline_school_hours_only=True,
            screen_offline_grace_minutes=15,
        )

        self.today = timezone.localdate()
        day = DaySchedule.objects.create(
            settings=self.settings,
            weekday=self.today.weekday() + 1,
            is_active=True,
            periods_count=2,
        )
        Period.objects.create(day=day, index=1, starts_at=dt_time(8, 0), ends_at=dt_time(10, 0))
        Period.objects.create(day=day, index=2, starts_at=dt_time(10, 0), ends_at=dt_time(12, 0))

        self.screen = DisplayScreen.objects.create(
            school=self.school,
            name="الشاشة الرئيسية",
            is_active=True,
            bound_device_id="holiday-device-1",
        )

    def _at(self, hour, minute=0):
        tz = timezone.get_current_timezone()
        return timezone.make_aware(datetime.combine(self.today, dt_time(hour, minute)), tz)

    def _close_today(self, title="إجازة عيد الفطر"):
        return SchoolClosure.objects.create(
            settings=self.settings,
            title=title,
            start_date=self.today,
            end_date=self.today,
        )

    def _scan_twice(self, moment, screen=None):
        screen = screen or self.screen
        screen.last_seen = moment - timedelta(minutes=30)
        screen.save(update_fields=("last_seen",))
        scan_screens(now=moment)
        return scan_screens(now=moment + timedelta(seconds=CONFIRMATION_SECONDS + 1))

    # ── الكتم ───────────────────────────────────────────────────────
    def test_a_registered_holiday_silences_the_alert_mid_lesson(self):
        """منتصف «يوم الدوام» تماماً — والفرق الوحيد أنه مسجَّل إجازة."""
        self._close_today()
        result = self._scan_twice(self._at(10, 0))

        self.assertEqual(result["alerted"], 0)
        self.assertFalse(TelegramAlert.objects.exists())
        self.assertEqual(
            ScreenOutage.objects.get(screen=self.screen).suppressed_reason,
            SUPPRESSED_HOLIDAY,
        )

    def test_the_same_moment_alerts_when_no_holiday_is_registered(self):
        """إثبات أن الكتم جاء من التقويم لا من شيء آخر."""
        result = self._scan_twice(self._at(10, 0))

        self.assertEqual(result["alerted"], 1)
        self.assertEqual(TelegramAlert.objects.filter(event_type="screen_offline").count(), 1)

    def test_the_outage_is_still_recorded_so_uptime_stays_honest(self):
        """كتمٌ لا إسكات: انقطاعٌ يبدأ في الإجازة ويمتد بعدها يجب أن يُعرف."""
        self._close_today()
        self._scan_twice(self._at(10, 0))

        outage = ScreenOutage.objects.get(screen=self.screen)
        self.assertIsNotNone(outage.detected_at)
        self.assertIsNone(outage.resolved_at)
        self.assertIsNone(outage.alert_sent_at)

    def test_round_the_clock_monitoring_still_respects_the_calendar(self):
        """‏«راقب على مدار الساعة» تعني أيام الدوام، لا تجاهل التقويم.

        المدرسة التي فعّلتها لتلتقط أعطال الليل كانت تُخبَر كل صباح عيد أن
        شاشاتها تعطّلت.
        """
        self.settings.screen_offline_school_hours_only = False
        self.settings.save(update_fields=("screen_offline_school_hours_only",))
        self._close_today()

        result = self._scan_twice(self._at(4, 35))

        self.assertEqual(result["alerted"], 0)
        self.assertEqual(
            ScreenOutage.objects.get(screen=self.screen).suppressed_reason,
            SUPPRESSED_HOLIDAY,
        )

    def test_a_screen_marked_always_on_still_alerts_through_a_holiday(self):
        """الاستثناء الوحيد: شاشةٌ وُسمت صراحةً بأنها تعمل في الإجازات."""
        self.screen.monitor_always_on = True
        self.screen.save(update_fields=("monitor_always_on",))
        self._close_today()

        result = self._scan_twice(self._at(10, 0))

        self.assertEqual(result["alerted"], 1)

    # ── السبب ───────────────────────────────────────────────────────
    def test_the_cause_says_holiday_instead_of_guessing_at_the_router(self):
        """شاشتان تُطفآن معاً في إجازة تبدوان انقطاع كهرباء تماماً."""
        second = DisplayScreen.objects.create(
            school=self.school,
            name="غرفة المعلمين",
            is_active=True,
            bound_device_id="holiday-device-2",
        )
        self._close_today()
        moment = self._at(10, 0)
        second.last_seen = moment - timedelta(minutes=30)
        second.save(update_fields=("last_seen",))
        self._scan_twice(moment)

        causes = set(ScreenOutage.objects.values_list("cause", flat=True))
        self.assertEqual(causes, {CAUSE_SCHOOL_CLOSED})
        self.assertNotIn(CAUSE_SCHOOL_NETWORK, causes)

    # ── الاستئناف ───────────────────────────────────────────────────
    def test_the_alert_arrives_on_the_first_working_morning(self):
        """الخبر يفيد حين يفيد: صباح أول دوام، لا فجر الإجازة."""
        closure = self._close_today()
        self._scan_twice(self._at(10, 0))
        self.assertFalse(TelegramAlert.objects.exists())

        # انتهت الإجازة أمس، واليوم دوام.
        closure.start_date = self.today - timedelta(days=2)
        closure.end_date = self.today - timedelta(days=1)
        closure.save(update_fields=("start_date", "end_date"))
        cache.clear()

        result = scan_screens(now=self._at(10, 0) + timedelta(seconds=CONFIRMATION_SECONDS + 2))

        self.assertEqual(result["alerted"], 1)
        self.assertEqual(TelegramAlert.objects.filter(event_type="screen_offline").count(), 1)


@override_settings(
    TELEGRAM_ALERTS_ENABLED=True,
    DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION=False,
)
class ScreenPlatformOutageTests(TestCase):
    """When most of the fleet goes quiet the fault is ours — say so once."""

    def setUp(self):
        cache.clear()
        self.screens = []
        for school_index in range(4):
            school = School.objects.create(
                name=f"مدرسة {school_index}", slug=f"platform-school-{school_index}"
            )
            SchoolSettings.objects.create(
                school=school,
                name=school.name,
                screen_offline_threshold_minutes=5,
                screen_offline_alerts_enabled=True,
                screen_offline_school_hours_only=False,
            )
            for screen_index in range(4):
                self.screens.append(
                    DisplayScreen.objects.create(
                        school=school,
                        name=f"شاشة {school_index}-{screen_index}",
                        is_active=True,
                        bound_device_id=f"platform-{school_index}-{screen_index}",
                        last_seen=timezone.now() - timedelta(minutes=30),
                    )
                )

    def test_fleet_wide_silence_sends_one_admin_incident_and_no_per_screen_alerts(self):
        now = timezone.now()
        scan_screens(now=now)
        result = scan_screens(now=now + timedelta(seconds=CONFIRMATION_SECONDS + 1))

        self.assertTrue(result["platform_outage"])
        self.assertEqual(result["alerted"], 0)
        alerts = TelegramAlert.objects.filter(event_type="screen_platform_outage")
        self.assertEqual(alerts.count(), 1)
        self.assertIn("اشتباه عطل في المنصة", alerts.first().message)
        self.assertEqual(TelegramAlert.objects.filter(event_type="screen_offline").count(), 0)
        for outage in ScreenOutage.objects.all():
            self.assertEqual(outage.suppressed_reason, SUPPRESSED_PLATFORM)
            self.assertEqual(outage.cause, CAUSE_PLATFORM)


    def test_after_hours_silence_does_not_send_a_platform_alert(self):
        now = timezone.now()
        SchoolSettings.objects.update(screen_offline_school_hours_only=True)
        with patch(
            "core.screen_monitoring.cached_active_window_for_today",
            return_value=(now - timedelta(hours=8), now - timedelta(hours=1)),
        ):
            scan_screens(now=now)
            result = scan_screens(now=now + timedelta(seconds=CONFIRMATION_SECONDS + 1))
        self.assertFalse(result["platform_outage"])
        self.assertFalse(TelegramAlert.objects.filter(event_type="screen_platform_outage").exists())
        self.assertFalse(ScreenOutage.objects.exclude(suppressed_reason=SUPPRESSED_AFTER_HOURS).exists())

    def test_missing_school_day_does_not_send_a_platform_alert(self):
        SchoolSettings.objects.update(screen_offline_school_hours_only=True)
        with patch("core.screen_monitoring.cached_active_window_for_today", return_value=None):
            result = scan_screens()
        self.assertFalse(result["platform_outage"])
        self.assertFalse(TelegramAlert.objects.filter(event_type="screen_platform_outage").exists())

    def test_holidays_are_excluded_even_with_all_day_school_monitoring(self):
        with patch("core.screen_monitoring.closure_title_on", return_value="holiday"):
            result = scan_screens()
        self.assertFalse(result["platform_outage"])
        self.assertFalse(TelegramAlert.objects.filter(event_type="screen_platform_outage").exists())

    def test_explicit_always_on_screens_are_still_monitored_on_holidays(self):
        DisplayScreen.objects.update(monitor_always_on=True)
        with patch("core.screen_monitoring.closure_title_on", return_value="holiday"):
            result = scan_screens()
        self.assertTrue(result["platform_outage"])
        self.assertEqual(TelegramAlert.objects.filter(event_type="screen_platform_outage").count(), 1)

    def test_screens_never_connected_do_not_imply_a_platform_outage(self):
        now = timezone.now()
        DisplayScreen.objects.update(last_seen=None, created_at=now - timedelta(hours=2))
        result = scan_screens(now=now)
        self.assertFalse(result["platform_outage"])
        self.assertFalse(TelegramAlert.objects.filter(event_type="screen_platform_outage").exists())

    def test_previous_school_session_does_not_imply_a_new_platform_outage(self):
        now = timezone.now()
        SchoolSettings.objects.update(screen_offline_school_hours_only=True)
        with patch(
            "core.screen_monitoring.cached_active_window_for_today",
            return_value=(now - timedelta(minutes=15), now + timedelta(hours=4)),
        ):
            result = scan_screens(now=now)
        self.assertFalse(result["platform_outage"])
        self.assertFalse(TelegramAlert.objects.filter(event_type="screen_platform_outage").exists())

    def test_independent_disconnects_do_not_imply_one_platform_outage(self):
        now = timezone.now()
        for index, screen in enumerate(self.screens):
            DisplayScreen.objects.filter(pk=screen.pk).update(
                last_seen=now - timedelta(minutes=10 + 3 * index)
            )
        result = scan_screens(now=now)
        self.assertFalse(result["platform_outage"])
        self.assertFalse(TelegramAlert.objects.filter(event_type="screen_platform_outage").exists())

    def test_platform_counts_and_suppression_exclude_closed_schools(self):
        now = timezone.now()
        SchoolSettings.objects.update(screen_offline_school_hours_only=True)
        closed_school_id = self.screens[0].school_id

        def window(school_settings):
            end = now - timedelta(minutes=1) if school_settings.school_id == closed_school_id else now + timedelta(hours=2)
            return now - timedelta(hours=3), end

        with patch("core.screen_monitoring.cached_active_window_for_today", side_effect=window):
            scan_screens(now=now)
            result = scan_screens(now=now + timedelta(seconds=CONFIRMATION_SECONDS + 1))
        self.assertTrue(result["platform_outage"])
        alert = TelegramAlert.objects.get(event_type="screen_platform_outage")
        self.assertIn("<b>12</b> من 12", alert.message)
        self.assertIn("<b>3</b>", alert.message)
        self.assertFalse(
            ScreenOutage.objects.filter(screen__school_id=closed_school_id)
            .exclude(suppressed_reason=SUPPRESSED_AFTER_HOURS).exists()
        )


class DisplayGoodbyeBeaconTests(TestCase):
    """The beacon is what separates "switched off" from "internet died"."""

    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة الوداع", slug="goodbye-school")
        plan = SubscriptionPlan.objects.create(
            code="goodbye-plan",
            name="خطة",
            price=Decimal("100.00"),
            duration_days=365,
            max_screens=3,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate(),
            status="active",
        )
        self.screen = DisplayScreen.objects.create(
            school=self.school,
            name="شاشة الوداع",
            is_active=True,
            bound_device_id="goodbye-device",
        )

    def test_beacon_records_a_device_off_signal(self):
        response = self.client.post(
            f"/api/display/goodbye/{self.screen.token}/",
            data=json.dumps({"reason": "pagehide", "online": True}),
            content_type="text/plain;charset=UTF-8",
        )

        self.assertEqual(response.status_code, 200)
        signal = read_disconnect_signals([self.screen.pk])[self.screen.pk]
        self.assertEqual(signal["source"], "pagehide")
        self.assertEqual(signal["reason"], "pagehide")

    def test_unknown_token_is_rejected_without_recording_anything(self):
        response = self.client.post(
            "/api/display/goodbye/" + "f" * 64 + "/",
            data="{}",
            content_type="text/plain;charset=UTF-8",
        )

        # Rejected by the shared display-token middleware before the view runs.
        self.assertEqual(response.status_code, 403)
        self.assertEqual(read_disconnect_signals([self.screen.pk]), {})


class DisplayTokenMiddlewareTests(SimpleTestCase):
    def test_ws_metrics_path_does_not_require_display_token(self):
        request = RequestFactory().get("/api/display/ws-metrics/")
        middleware = DisplayTokenMiddleware(lambda req: JsonResponse({"ok": True}))

        response = middleware(request)

        self.assertEqual(response.status_code, 200)


class RootAssetTests(SimpleTestCase):
    def test_platform_favicon_uses_the_requested_png_logo(self):
        expected = (
            Path(settings.BASE_DIR)
            / "static"
            / "img"
            / "school-display-logo-mark-white-preview (1).png"
        ).read_bytes()

        for route_name in ("favicon", "favicon_png"):
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/png")
                self.assertEqual(response.content, expected)

    def test_service_worker_is_served_at_root(self):
        for route_name in ("service_worker", "sw_js"):
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/javascript; charset=utf-8")
                self.assertIn(b"self.addEventListener", response.content)
                # Match the release constant rather than a literal cache name, so
                # bumping the version does not require editing this assertion.
                self.assertRegex(
                    response.content.decode("utf-8"),
                    r"const RELEASE = '(v\d+)'",
                )

    def test_service_worker_respects_asset_versioning(self):
        """A `?v=` bump must reach the screen.

        The worker previously matched cached assets with `{ignoreSearch: true}`,
        so a new release resolved to the previously cached file and screens ran
        the old bundle indefinitely. Version-agnostic lookup is now allowed in
        exactly one place: the offline fallback inside `handleAsset`'s catch.
        """
        source = self.client.get(reverse("sw_js")).content.decode("utf-8")

        offline_branch = source.split("} catch (offline) {", 1)
        self.assertEqual(len(offline_branch), 2, "handleAsset lost its offline fallback branch")

        online_path = offline_branch[0]
        self.assertNotIn(
            "ignoreSearch",
            online_path,
            "the online asset path must key the cache on the full URL, query string included",
        )
        self.assertIn("ignoreSearch", offline_branch[1])

    def test_service_worker_serves_ranges_from_a_warmed_complete_file(self):
        """The bell remains playable after an offline television restart.

        Cache Storage cannot store an origin 206 response, so the worker warms
        the complete file and synthesises the requested range from that cached
        200 response.
        """
        source = self.client.get(reverse("sw_js")).content.decode("utf-8")

        self.assertIn("async function handleRangeRequest(request)", source)
        self.assertIn("const cached = await cache.match(request.url);", source)
        self.assertIn("'Content-Range'", source)
        self.assertIn("event.respondWith(handleRangeRequest(request));", source)

    def test_service_worker_warms_the_first_display_page_and_payload_media(self):
        source = self.client.get(reverse("sw_js")).content.decode("utf-8")
        registration = (
            Path(settings.BASE_DIR) / "static" / "js" / "display-sw-register.js"
        ).read_text(encoding="utf-8")

        self.assertIn("CACHE_DISPLAY_PAGE", registration)
        self.assertIn("window.location.href", registration)
        self.assertIn("CACHE_DISPLAY_PAGE", source)
        self.assertIn("cacheDisplayPage(data.url)", source)
        self.assertIn("CACHE_DISPLAY_MEDIA", source)
        self.assertIn("cacheDisplayMedia(data.urls)", source)

    def test_service_worker_precaches_the_bundle_the_page_actually_loads(self):
        """The shell list must track what display.html links.

        It used to precache `display.js` while the page loaded `display.min.js`,
        so every screen downloaded 296 KB it never executed and the bundle it did
        execute was never available offline.
        """
        source = self.client.get(reverse("sw_js")).content.decode("utf-8")
        shell = source.split("const SHELL_ASSETS = [", 1)[1].split("]", 1)[0]
        display_template = (
            Path(settings.BASE_DIR) / "templates" / "website" / "display.html"
        ).read_text(encoding="utf-8")

        for asset in re.findall(r"'(/static/[^']+)'", shell):
            with self.subTest(asset=asset):
                self.assertIn(
                    asset.removeprefix("/static/"),
                    display_template,
                    f"{asset} is precached but no longer linked by the display page",
                )

    def test_display_shell_contains_scheduled_occasion_theme_runtime(self):
        display_template = (
            Path(settings.BASE_DIR) / "templates" / "website" / "display.html"
        ).read_text(encoding="utf-8")
        display_script = (
            Path(settings.BASE_DIR) / "static" / "js" / "display.js"
        ).read_text(encoding="utf-8")

        self.assertIn('id="occasionThemeDecor"', display_template)
        self.assertIn('id="occasionHero"', display_template)
        self.assertIn('id="occasionHeroTitle"', display_template)
        self.assertIn("occasion-hero__mark", display_template)
        self.assertIn("function applyOccasionTheme", display_script)
        self.assertIn("function refreshOccasionPresentation", display_script)
        self.assertIn('removeAttribute("data-occasion-theme")', display_script)
        self.assertIn('setAttribute("data-occasion-ambient", "1")', display_script)
        # Viewport sizing and centering are covered by DisplayBrowserTests on
        # rendered panels; local variable names do not define that behavior.

    def test_display_shell_renders_a_theme_for_every_registered_occasion(self):
        """الهوية البصرية تُولَّد من السجل، فلا تُكتب أي مناسبة يدويًا هنا.

        نصيّر الصفحة فعلًا بدل تفتيش نصّها: هذا يكشف مناسبة أُضيفت للسجل ولم
        يصلها لون، وهو بالضبط الخلل الصامت الذي كان التكرار يسبّبه.
        """
        from core import occasions

        html = render_to_string(
            "website/display.html",
            {
                "occasion_themes": occasions.all_occasions(),
                "occasion_theme_meta_json": json.dumps(
                    occasions.theme_map(), ensure_ascii=False
                ),
            },
        )

        for occasion in occasions.all_occasions():
            with self.subTest(occasion=occasion.key):
                self.assertIn(f'body[data-occasion-theme="{occasion.key}"]', html)
                self.assertIn(occasion.accent, html)
                self.assertIn(occasion.deep, html)

        # الثيمات المتقاعدة يجب ألا تُصيَّر إطلاقًا.
        for retired in occasions.RETIRED_OCCASION_KEYS:
            self.assertNotIn(f'body[data-occasion-theme="{retired}"]', html)

    def test_display_shell_publishes_occasion_meta_for_the_client(self):
        """``display.js`` يقرأ بيانات المناسبات من الصفحة لا من نسخة خاصة به."""
        from core import occasions

        html = render_to_string(
            "website/display.html",
            {
                "occasion_themes": occasions.all_occasions(),
                "occasion_theme_meta_json": json.dumps(
                    occasions.theme_map(), ensure_ascii=False
                ),
            },
        )

        match = re.search(
            r'<script type="application/json" id="occasionThemeMeta">(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(match, "كتلة بيانات المناسبات مفقودة من الصفحة")

        published = json.loads(match.group(1))
        self.assertEqual(set(published), set(occasions.OCCASIONS))
        for key, meta in published.items():
            with self.subTest(occasion=key):
                self.assertTrue(meta["label"])
                self.assertTrue(meta["mark"])
                self.assertTrue(meta["badgeIcon"])
                self.assertEqual(len(meta["symbols"]), 2)


class SecurityHeadersTests(SimpleTestCase):
    @override_settings(
        CONTENT_SECURITY_POLICY_ENABLED=True,
        CONTENT_SECURITY_POLICY_REPORT_ONLY=True,
        CONTENT_SECURITY_POLICY_REPORT_URI="/csp-report/",
    )
    def test_csp_starts_in_report_only_mode_with_restrictive_script_policy(self):
        request = RequestFactory().get("/")
        response = SecurityHeadersMiddleware(lambda _request: JsonResponse({"ok": True}))(request)

        policy = response["Content-Security-Policy-Report-Only"]
        self.assertIn("script-src 'self'", policy)
        self.assertIn("https://static.cloudflareinsights.com", policy)
        self.assertNotIn("'unsafe-eval'", policy)
        self.assertIn("report-uri /csp-report/", policy)
        self.assertIn("camera=()", response["Permissions-Policy"])

    @override_settings(
        CONTENT_SECURITY_POLICY_ENABLED=True,
        CONTENT_SECURITY_POLICY_REPORT_ONLY=False,
    )
    def test_csp_can_be_switched_to_enforcement_mode(self):
        request = RequestFactory().get("/")
        response = SecurityHeadersMiddleware(lambda _request: JsonResponse({"ok": True}))(request)

        self.assertIn("Content-Security-Policy", response)
        self.assertNotIn("Content-Security-Policy-Report-Only", response)

    def test_csp_report_endpoint_accepts_browser_payload_without_reflecting_it(self):
        payload = {
            "csp-report": {
                "effective-directive": "script-src-elem",
                "blocked-uri": "https://evil.example/script.js?secret=value",
                "document-uri": "https://school-display.com/dashboard/?private=value",
            }
        }
        with self.assertLogs("core.csp", level="WARNING") as logs:
            response = self.client.post(
                reverse("csp_report"),
                data=json.dumps(payload),
                content_type="application/csp-report",
            )

        self.assertEqual(response.status_code, 204)
        self.assertNotIn("secret=value", " ".join(logs.output))
        self.assertNotIn("private=value", " ".join(logs.output))

    def test_primary_shells_no_longer_load_alpine_or_inline_shell_scripts(self):
        dashboard_shell = (Path(settings.BASE_DIR) / "templates" / "dashboard" / "_base.html").read_text(
            encoding="utf-8"
        )
        admin_shell = (Path(settings.BASE_DIR) / "templates" / "admin" / "admin_base.html").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("unpkg.com/alpinejs", dashboard_shell)
        self.assertNotIn("unpkg.com/alpinejs", admin_shell)
        self.assertIn("js/dashboard-shell.js", dashboard_shell)
        self.assertIn("js/admin-shell.js", admin_shell)
