"""Screen outage detection and alerting.

Three gates stand between a silent screen and a message:

1. **Is this worth watching now?** Outages outside the school day are recorded
   for the weekly uptime report but never alerted — a television switched off
   after dismissal is not a fault.
2. **Are we sure?** An outage must survive a second scan, clear a per-screen
   cooldown, and stay under a daily cap before anything is sent. Screens that
   fail together are merged into one school-level message, and a fault that
   spans many schools is reported once as a platform incident instead of a
   storm of per-screen messages.
3. **Why did it happen?** Every alert carries a named cause from
   :mod:`core.screen_diagnostics`, so the reader knows whether to check the
   router, the television, or nothing at all.

Every message goes to the platform administrator over Telegram — schools are
never mailed about screen faults. Recovery is announced the same way: an outage
that closes reports how long the screen was dark.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from html import escape

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.display_presence import presence_map
from core.models import DisplayScreen, ScreenOutage, ScreenWeeklyUptimeReport
from core.screen_diagnostics import (
    CAUSE_PLATFORM,
    CAUSE_SCHOOL_CLOSED,
    CAUSE_SCHOOL_NETWORK,
    CONFIDENCE_CONFIRMED,
    SCOPE_SCHOOL,
    cause_label,
    cause_presentation,
    classify_outage,
    read_disconnect_signals,
)
from schedule.closures import closure_title_on
from schedule.models import SchoolSettings
from schedule.wake_broadcaster import cached_active_window_for_today
from telegram_alerts.services import queue_alert


# An outage must still be open on the next scan before anyone hears about it.
# The worker runs every 60s, so this always costs two observations.
CONFIRMATION_SECONDS = 90

# Screens that fall silent within this span of each other share a cause.
SIMULTANEOUS_WINDOW_SECONDS = 120

# One school-level message replaces this many individual ones.
SCHOOL_AGGREGATE_MIN_SCREENS = 3

# Platform-wide fault detection: a fleet this large going this quiet is us, not
# every school at once.
PLATFORM_OUTAGE_MIN_SCREENS = 12
PLATFORM_OUTAGE_MIN_SCHOOLS = 3
PLATFORM_OUTAGE_RATIO = 0.6
PLATFORM_ALERT_COOLDOWN_SECONDS = 30 * 60

# Suppression reasons recorded on the outage row.
SUPPRESSED_HOLIDAY = "holiday"
SUPPRESSED_OUTSIDE_SCHOOL_DAY = "outside_school_day"
SUPPRESSED_BEFORE_GRACE = "before_grace"
SUPPRESSED_AFTER_HOURS = "after_school_hours"
SUPPRESSED_AWAITING_CONFIRMATION = "awaiting_confirmation"
SUPPRESSED_COOLDOWN = "cooldown"
SUPPRESSED_DAILY_CAP = "daily_cap"
SUPPRESSED_PLATFORM = "platform_outage"

SUPPRESSION_LABELS = {
    SUPPRESSED_HOLIDAY: "يوم إجازة",
    SUPPRESSED_OUTSIDE_SCHOOL_DAY: "خارج أيام الدوام",
    SUPPRESSED_BEFORE_GRACE: "ضمن مهلة بداية الدوام",
    SUPPRESSED_AFTER_HOURS: "بعد انتهاء الدوام",
    SUPPRESSED_AWAITING_CONFIRMATION: "بانتظار التأكيد",
    SUPPRESSED_COOLDOWN: "ضمن فترة التهدئة",
    SUPPRESSED_DAILY_CAP: "بلغ الحد اليومي",
    SUPPRESSED_PLATFORM: "عطل منصة",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_dt(value) -> str:
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else "لم تتصل بعد"


def _fmt_time(value) -> str:
    return timezone.localtime(value).strftime("%H:%M") if value else "—"


def format_duration(delta: timedelta | None) -> str:
    """Human Arabic duration: «ساعتان و15 دقيقة»."""
    if delta is None:
        return "—"
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)

    def _unit(count: int, one: str, two: str, few: str, many: str) -> str:
        if count == 1:
            return one
        if count == 2:
            return two
        if 3 <= count <= 10:
            return f"{count} {few}"
        return f"{count} {many}"

    parts = []
    if days:
        parts.append(_unit(days, "يوم", "يومان", "أيام", "يومًا"))
    if hours:
        parts.append(_unit(hours, "ساعة", "ساعتان", "ساعات", "ساعة"))
    if minutes and not days:
        parts.append(_unit(minutes, "دقيقة", "دقيقتان", "دقائق", "دقيقة"))
    if not parts:
        return "أقل من دقيقة"
    return " و".join(parts)


def _site_base_url() -> str:
    return str(getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")


def _screens_dashboard_url() -> str:
    base = _site_base_url()
    return f"{base}/dashboard/screens/" if base else "/dashboard/screens/"


# ---------------------------------------------------------------------------
# Scan state
# ---------------------------------------------------------------------------

@dataclass
class _ScreenState:
    screen: DisplayScreen
    last_seen: datetime | None
    offline_since: datetime
    is_offline: bool
    ever_connected: bool


@dataclass
class _SchoolState:
    school: object
    school_settings: SchoolSettings
    states: list[_ScreenState] = field(default_factory=list)

    @property
    def monitored_count(self) -> int:
        return len(self.states)

    @property
    def offline_states(self) -> list[_ScreenState]:
        return [state for state in self.states if state.is_offline]


def _threshold_minutes(school_settings) -> int:
    return max(5, int(getattr(school_settings, "screen_offline_threshold_minutes", 10) or 10))


def _grace_minutes(school_settings) -> int:
    return max(0, min(120, int(getattr(school_settings, "screen_offline_grace_minutes", 15) or 0)))


def _cooldown_seconds(school_settings) -> int:
    minutes = int(getattr(school_settings, "screen_offline_cooldown_minutes", 120) or 120)
    return max(10, min(720, minutes)) * 60


def _daily_cap(school_settings) -> int:
    return max(1, min(20, int(getattr(school_settings, "screen_offline_max_alerts_per_day", 3) or 3)))


def _simultaneous(states: list[_ScreenState]) -> bool:
    """Did these screens fall silent at effectively the same moment?"""
    moments = [state.offline_since for state in states if state.offline_since]
    if len(moments) < 2:
        return False
    return (max(moments) - min(moments)).total_seconds() <= SIMULTANEOUS_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# Alert gating
# ---------------------------------------------------------------------------

def _window_suppression(school_settings, screen, window, now, *, school_closed=False) -> str:
    """Return a suppression reason when the school day says "not now".

    The holiday test sits **above** ``screen_offline_school_hours_only`` on
    purpose. That switch means "watch this school around the clock on its
    working days" — it was never meant to say "ignore the calendar". A school
    that turned it on to catch night-time faults was still being told, every
    morning of Eid, that its screens had failed. Only the per-screen
    ``monitor_always_on`` overrides a closure, because that flag is set on the
    few screens that genuinely must run through the holidays.
    """
    if getattr(screen, "monitor_always_on", False):
        return ""
    if school_closed:
        return SUPPRESSED_HOLIDAY
    if not getattr(school_settings, "screen_offline_school_hours_only", True):
        return ""
    if window is None:
        return SUPPRESSED_OUTSIDE_SCHOOL_DAY

    active_start, active_end = window
    if now < active_start + timedelta(minutes=_grace_minutes(school_settings)):
        return SUPPRESSED_BEFORE_GRACE
    if now > active_end:
        return SUPPRESSED_AFTER_HOURS
    return ""


def _rate_suppression(school_settings, screen, now) -> str:
    """Throttle repeat alerts for one screen.

    Both limits read the outage log rather than a cache counter: a Redis restart
    must not turn into a fresh burst of alerts for a screen that has been
    flapping all morning.
    """
    last_alert_at = (
        ScreenOutage.objects.filter(screen=screen, alert_sent_at__isnull=False)
        .order_by("-alert_sent_at")
        .values_list("alert_sent_at", flat=True)
        .first()
    )
    if last_alert_at and (now - last_alert_at).total_seconds() < _cooldown_seconds(school_settings):
        return SUPPRESSED_COOLDOWN

    day_start = timezone.make_aware(
        datetime.combine(timezone.localdate(now), time.min), timezone.get_current_timezone()
    )
    sent_today = ScreenOutage.objects.filter(
        screen=screen, alert_sent_at__gte=day_start, alert_sent_at__lt=day_start + timedelta(days=1)
    ).count()
    if sent_today >= _daily_cap(school_settings):
        return SUPPRESSED_DAILY_CAP
    return ""


# ---------------------------------------------------------------------------
# Messages — Telegram to the system administrator, and nowhere else
# ---------------------------------------------------------------------------

def _telegram_outage_message(school, entries, *, cause, detail, confidence, scope, now) -> str:
    presentation = cause_presentation(cause)
    count = len(entries)
    header = "🔴 انقطاع شاشة عرض" if count == 1 else f"🔴 انقطاع {count} شاشات"
    if scope == SCOPE_SCHOOL:
        header = f"🔴 انقطاع على مستوى المدرسة ({count} شاشات)"

    lines = [
        f"<b>{header}</b>",
        "",
        f"🏫 المدرسة: <b>{escape(str(school.name), quote=False)}</b>",
    ]
    for outage, state in entries[:8]:
        duration = format_duration(now - (state.last_seen or outage.detected_at))
        lines.append(
            f"🖥 <b>{escape(state.screen.name, quote=False)}</b> — "
            f"آخر ظهور <code>{_fmt_dt(state.last_seen)}</code> (منذ {duration})"
        )
    if count > 8:
        lines.append(f"… و{count - 8} شاشات أخرى")

    confidence_label = "مؤكد" if confidence == CONFIDENCE_CONFIRMED else "مرجّح"
    lines.extend(
        [
            "",
            f"{presentation['icon']} السبب ({confidence_label}): <b>{escape(cause_label(cause), quote=False)}</b>",
        ]
    )
    if detail:
        lines.append(f"🔎 {escape(detail, quote=False)}")
    return "\n".join(lines)


def _send_outage_alert(school, entries, *, cause, detail, confidence, scope, now) -> None:
    first_outage = entries[0][0]
    queue_alert(
        event_type="screen_offline",
        dedupe_key=f"screen-offline:{first_outage.pk}:{first_outage.alert_count + 1}",
        message=_telegram_outage_message(
            school, entries, cause=cause, detail=detail, confidence=confidence, scope=scope, now=now
        ),
        action_url=_screens_dashboard_url(),
        action_label="فتح الشاشات",
    )


def _send_recovery_alert(school, entries, *, now) -> None:
    count = len(entries)
    rows = []
    for outage, state in entries:
        downtime = (outage.resolved_at or now) - (outage.last_seen_at or outage.detected_at)
        rows.append(
            {
                "name": state.screen.name,
                "downtime": format_duration(downtime),
                "back_at": _fmt_time(outage.resolved_at or now),
            }
        )

    header = "🟢 عادت الشاشة للعمل" if count == 1 else f"🟢 عادت {count} شاشات للعمل"
    lines = [f"<b>{header}</b>", "", f"🏫 المدرسة: <b>{escape(str(school.name), quote=False)}</b>"]
    for row in rows[:8]:
        lines.append(
            f"🖥 <b>{escape(row['name'], quote=False)}</b> — مدة الانقطاع {row['downtime']} "
            f"(عادت {row['back_at']})"
        )
    if count > 8:
        lines.append(f"… و{count - 8} شاشات أخرى")

    first_outage = entries[0][0]
    queue_alert(
        event_type="screen_recovered",
        dedupe_key=f"screen-recovered:{first_outage.pk}",
        message="\n".join(lines),
        action_url=_screens_dashboard_url(),
        action_label="فتح الشاشات",
    )


def _send_platform_alert(*, offline_count, monitored_count, school_count, now) -> None:
    """One incident message to the administrator instead of a per-screen storm."""
    try:
        if not cache.add("screen-monitor:platform-alert", "1", timeout=PLATFORM_ALERT_COOLDOWN_SECONDS):
            return
    except Exception:
        pass

    percent = int(round(offline_count * 100 / max(1, monitored_count)))
    base = _site_base_url()
    queue_alert(
        event_type="screen_platform_outage",
        dedupe_key=f"screen-platform-outage:{int(now.timestamp() // PLATFORM_ALERT_COOLDOWN_SECONDS)}",
        message=(
            "<b>🛠 اشتباه عطل في المنصة</b>\n\n"
            f"🖥 الشاشات المنقطعة: <b>{offline_count}</b> من {monitored_count} (<b>{percent}%</b>)\n"
            f"🏫 عدد المدارس المتأثرة: <b>{school_count}</b>\n"
            f"🕒 وقت الرصد: <code>{_fmt_dt(now)}</code>\n\n"
            "🔕 أُوقفت التنبيهات الفردية للمدارس مؤقتًا لتجنّب إزعاجها برسائل سببها من طرفنا."
        ),
        action_url=f"{base}/dashboard/admin-panel/" if base else "",
        action_label="فتح لوحة مدير النظام",
    )


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def _subscription_is_active(school_id: int) -> bool:
    if not getattr(settings, "DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION", True):
        return True
    try:
        from subscriptions.access import school_subscription_is_active

        return bool(school_subscription_is_active(school_id))
    except Exception:
        # Never silence a paying customer's alerts because billing lookup broke.
        return True


def _collect_states(now) -> dict[int, _SchoolState]:
    screens = list(
        DisplayScreen.objects.filter(is_active=True, school__isnull=False, auto_disabled_by_limit=False, school__display_suspended=False)
        .exclude(bound_device_id__isnull=True)
        .exclude(bound_device_id="")
        .select_related("school", "school__schedule_settings")
    )
    if not screens:
        return {}

    presence = presence_map(screens)
    schools: dict[int, _SchoolState] = {}
    subscription_ok: dict[int, bool] = {}

    for screen in screens:
        try:
            school_settings = screen.school.schedule_settings
        except SchoolSettings.DoesNotExist:
            continue
        if not school_settings.screen_offline_alerts_enabled:
            continue
        # A lapsed subscription blacks out the screens by design. Telling the
        # school its display is "faulty" would be both wrong and unkind.
        if screen.school_id not in subscription_ok:
            subscription_ok[screen.school_id] = _subscription_is_active(screen.school_id)
        if not subscription_ok[screen.school_id]:
            continue

        last_seen = presence.get(screen.pk)
        offline_since = last_seen or screen.bound_at or screen.created_at
        cutoff = now - timedelta(minutes=_threshold_minutes(school_settings))
        state = _ScreenState(
            screen=screen,
            last_seen=last_seen,
            offline_since=offline_since,
            is_offline=offline_since < cutoff,
            ever_connected=last_seen is not None,
        )
        bucket = schools.get(screen.school_id)
        if bucket is None:
            bucket = _SchoolState(school=screen.school, school_settings=school_settings)
            schools[screen.school_id] = bucket
        bucket.states.append(state)

    return schools


def _platform_scope(schools: dict[int, _SchoolState], now) -> dict[int, _SchoolState]:
    """Compare screens expected to run now and observed during this school day."""
    eligible = {}
    for school_id, bucket in schools.items():
        school_settings = bucket.school_settings
        window = cached_active_window_for_today(school_settings)
        closed = closure_title_on(school_settings, timezone.localdate(now)) is not None
        states = []
        for state in bucket.states:
            if not state.ever_connected or _window_suppression(
                school_settings, state.screen, window, now, school_closed=closed
            ):
                continue
            if (
                school_settings.screen_offline_school_hours_only
                and not state.screen.monitor_always_on
                and window is not None
                and state.last_seen < window[0]
            ):
                continue
            states.append(state)
        if states:
            eligible[school_id] = _SchoolState(
                school=bucket.school, school_settings=school_settings, states=states
            )
    return eligible


def _detect_platform_outage(schools: dict[int, _SchoolState]) -> bool:
    monitored = sum(bucket.monitored_count for bucket in schools.values())
    offline = sum(len(bucket.offline_states) for bucket in schools.values())
    affected_schools = sum(1 for bucket in schools.values() if bucket.offline_states)
    if monitored < PLATFORM_OUTAGE_MIN_SCREENS or affected_schools < PLATFORM_OUTAGE_MIN_SCHOOLS:
        return False
    offline_states = [state for bucket in schools.values() for state in bucket.offline_states]
    return offline / monitored >= PLATFORM_OUTAGE_RATIO and _simultaneous(offline_states)


def scan_screens(*, now=None) -> dict:
    now = now or timezone.now()
    opened = resolved = alerted = suppressed = recovered = 0

    schools = _collect_states(now)
    if not schools:
        return {"opened": 0, "resolved": 0, "alerted": 0, "suppressed": 0, "recovered": 0, "platform_outage": False}

    platform_schools = _platform_scope(schools, now)
    platform_outage = _detect_platform_outage(platform_schools)
    platform_screen_ids = set()
    if platform_outage:
        platform_screen_ids = {
            state.screen.pk for bucket in platform_schools.values() for state in bucket.states
        }
        monitored = sum(bucket.monitored_count for bucket in platform_schools.values())
        offline = sum(len(bucket.offline_states) for bucket in platform_schools.values())
        _send_platform_alert(
            offline_count=offline,
            monitored_count=monitored,
            school_count=sum(1 for bucket in platform_schools.values() if bucket.offline_states),
            now=now,
        )

    signals = read_disconnect_signals(
        state.screen.pk for bucket in schools.values() for state in bucket.states
    )

    # One query answers "which screens have an outage on file at all". Without
    # it every screen in the fleet cost a transaction and a SELECT ... FOR UPDATE
    # on every pass — once a minute, around the clock — just to learn that a
    # healthy screen is still healthy. A screen that is neither offline now nor
    # carrying an open outage needs no row-level work of any kind.
    #
    # The set is a snapshot: an outage opened by a concurrent pass after this
    # read is simply resolved on the next pass, one minute later.
    screens_with_open_outage = set(
        ScreenOutage.objects.filter(resolved_at__isnull=True).values_list("screen_id", flat=True)
    )

    for bucket in schools.values():
        school = bucket.school
        school_settings = bucket.school_settings
        window = cached_active_window_for_today(school_settings)
        # يُقرأ مرة لكل مدرسة لا لكل شاشة: المسح يدور كل دقيقة على الأسطول كله.
        school_closed = closure_title_on(school_settings, timezone.localdate(now)) is not None
        offline_states = bucket.offline_states
        simultaneous = _simultaneous(offline_states)

        pending: list[tuple[ScreenOutage, _ScreenState]] = []
        recoveries: list[tuple[ScreenOutage, _ScreenState]] = []

        for state in bucket.states:
            screen = state.screen
            screen_platform_outage = screen.pk in platform_screen_ids
            if not state.is_offline and screen.pk not in screens_with_open_outage:
                continue

            with transaction.atomic():
                outage = (
                    ScreenOutage.objects.select_for_update()
                    .filter(screen=screen, resolved_at__isnull=True)
                    .order_by("-detected_at")
                    .first()
                )

                if not state.is_offline:
                    if outage is not None:
                        outage.resolved_at = now
                        outage.save(update_fields=("resolved_at",))
                        resolved += 1
                        if outage.alert_sent_at and getattr(
                            school_settings, "screen_recovery_notice_enabled", True
                        ):
                            recoveries.append((outage, state))
                    continue

                classification = classify_outage(
                    last_seen=state.last_seen,
                    ever_connected=state.ever_connected,
                    signal=signals.get(screen.pk),
                    school_offline_count=len(offline_states),
                    school_monitored_count=bucket.monitored_count,
                    school_simultaneous=simultaneous,
                    platform_outage=screen_platform_outage,
                    school_closed=school_closed,
                )
                signal = signals.get(screen.pk) or {}
                close_code = signal.get("code")

                if outage is None:
                    outage = ScreenOutage.objects.create(
                        screen=screen,
                        detected_at=now,
                        last_seen_at=state.offline_since,
                        cause=classification["cause"],
                        cause_detail=classification["detail"][:255],
                        cause_confidence=classification["confidence"],
                        scope=classification["scope"],
                        close_code=int(close_code) if isinstance(close_code, int) else None,
                    )
                    opened += 1
                elif outage.alert_sent_at is None:
                    # Keep re-reading the cause until we actually send: a
                    # school-wide pattern often becomes clear only on a later pass.
                    outage.cause = classification["cause"]
                    outage.cause_detail = classification["detail"][:255]
                    outage.cause_confidence = classification["confidence"]
                    outage.scope = classification["scope"]
                    if isinstance(close_code, int):
                        outage.close_code = int(close_code)
                    outage.save(
                        update_fields=("cause", "cause_detail", "cause_confidence", "scope", "close_code")
                    )

                if outage.alert_sent_at is not None:
                    continue

                reason = ""
                if screen_platform_outage:
                    reason = SUPPRESSED_PLATFORM
                elif (now - outage.detected_at).total_seconds() < CONFIRMATION_SECONDS:
                    reason = SUPPRESSED_AWAITING_CONFIRMATION
                else:
                    reason = _window_suppression(
                        school_settings, screen, window, now, school_closed=school_closed
                    ) or _rate_suppression(school_settings, screen, now)

                if reason:
                    if outage.suppressed_reason != reason:
                        outage.suppressed_reason = reason
                        outage.save(update_fields=("suppressed_reason",))
                    suppressed += 1
                    continue

                pending.append((outage, state))

        if recoveries:
            _send_recovery_alert(school, recoveries, now=now)
            ScreenOutage.objects.filter(pk__in=[outage.pk for outage, _ in recoveries]).update(
                recovery_notified_at=now
            )
            recovered += len(recoveries)

        if not pending:
            continue

        alerted += _dispatch_pending(school, bucket, pending, now=now)

    return {
        "opened": opened,
        "resolved": resolved,
        "alerted": alerted,
        "suppressed": suppressed,
        "recovered": recovered,
        "platform_outage": platform_outage,
    }


def _claim(outage, *, now, cause, detail, scope) -> bool:
    """Compare-and-set the alert as sent before anything leaves the building.

    Claiming first means a second worker (or an overlapping scan) finds the row
    already marked and stays quiet. A duplicate outage message is far worse than
    a missed one — the missed case still resolves itself on the next pass.
    """
    claimed = ScreenOutage.objects.filter(pk=outage.pk, alert_sent_at__isnull=True).update(
        alert_sent_at=now,
        alert_count=(outage.alert_count or 0) + 1,
        suppressed_reason="",
        cause=cause,
        cause_detail=detail[:255],
        scope=scope,
    )
    if not claimed:
        return False
    outage.alert_sent_at = now
    outage.alert_count = (outage.alert_count or 0) + 1
    outage.suppressed_reason = ""
    outage.cause = cause
    outage.cause_detail = detail[:255]
    outage.scope = scope
    return True


def _dispatch_pending(school, bucket, pending, *, now) -> int:
    """Send one message per school when several screens fail together."""
    aggregate = len(pending) >= SCHOOL_AGGREGATE_MIN_SCREENS or (
        len(pending) >= 2 and len(pending) == bucket.monitored_count
    )

    groups: list[list[tuple[ScreenOutage, _ScreenState]]]
    if aggregate:
        groups = [pending]
    else:
        groups = [[entry] for entry in pending]

    sent = 0
    for group in groups:
        lead_outage = group[0][0]
        if aggregate and len(group) > 1:
            # سببٌ عرفناه لا يُستبدل بتخمين: عطل المنصة والإغلاق المسجَّل كلاهما
            # يفسّر توقف الشاشات معاً، وإحلال «انقطاع الشبكة» محلهما يمحو
            # معرفةً مؤكدة ويضع مكانها ترجيحاً.
            if lead_outage.cause in (CAUSE_PLATFORM, CAUSE_SCHOOL_CLOSED):
                cause = lead_outage.cause
            else:
                cause = CAUSE_SCHOOL_NETWORK
            detail = (
                f"توقفت {len(group)} من {bucket.monitored_count} شاشات في المدرسة معًا، "
                "فأُرسل تنبيه واحد بدل رسالة لكل شاشة."
            )
            confidence = lead_outage.cause_confidence
            scope = SCOPE_SCHOOL
        else:
            cause = lead_outage.cause
            detail = lead_outage.cause_detail
            confidence = lead_outage.cause_confidence
            scope = lead_outage.scope

        claimed = [
            entry
            for entry in group
            if _claim(entry[0], now=now, cause=cause, detail=detail, scope=scope)
        ]
        if not claimed:
            continue

        _send_outage_alert(
            school,
            claimed,
            cause=cause,
            detail=detail,
            confidence=confidence,
            scope=scope,
            now=now,
        )
        sent += len(claimed)

    return sent


# ---------------------------------------------------------------------------
# Weekly uptime report
# ---------------------------------------------------------------------------

def _period_datetimes(week_start):
    current_tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(week_start, time.min), current_tz)
    end = start + timedelta(days=7)
    return start, end


def _offline_seconds(screen, start, end):
    total = 0
    outages = ScreenOutage.objects.filter(
        screen=screen,
        detected_at__lt=end,
    ).filter(Q(resolved_at__isnull=True) | Q(resolved_at__gt=start))
    for outage in outages:
        overlap_start = max(outage.last_seen_at or outage.detected_at, start)
        overlap_end = min(outage.resolved_at or end, end)
        if overlap_end > overlap_start:
            total += int((overlap_end - overlap_start).total_seconds())
    return min(total, int((end - start).total_seconds()))


def send_weekly_uptime_reports(*, week_start=None, now=None) -> dict:
    now = now or timezone.now()
    local_today = timezone.localdate(now)
    if week_start is None:
        current_week_start = local_today - timedelta(days=local_today.weekday())
        week_start = current_week_start - timedelta(days=7)
    week_end = week_start + timedelta(days=6)
    start, end = _period_datetimes(week_start)
    period_seconds = int((end - start).total_seconds())
    reports_created = 0
    school_reports = []

    for school_settings in SchoolSettings.objects.filter(
        school__isnull=False,
    ).select_related("school").order_by("school__name", "school_id"):
        school = school_settings.school
        reports = []
        for screen in DisplayScreen.objects.filter(school=school, is_active=True).order_by("name"):
            offline = _offline_seconds(screen, start, end)
            percent = Decimal(period_seconds - offline) * Decimal(100) / Decimal(period_seconds)
            report, created = ScreenWeeklyUptimeReport.objects.update_or_create(
                screen=screen,
                week_start=week_start,
                defaults={
                    "week_end": week_end,
                    "offline_seconds": offline,
                    "uptime_percent": percent.quantize(Decimal("0.01")),
                },
            )
            reports_created += int(created)
            reports.append(report)
        if not reports or all(report.sent_at for report in reports):
            continue
        school_reports.append((school, reports))

    if not school_reports:
        return {"schools_sent": 0, "reports_created": reports_created}

    lines = [
        "<b>📊 تقرير تشغيل الشاشات الأسبوعي</b>",
        f"🗓 الفترة: <code>{week_start} إلى {week_end}</code>",
    ]
    for school, reports in school_reports:
        average = sum(
            (report.uptime_percent for report in reports),
            Decimal("0"),
        ) / Decimal(len(reports))
        lines.extend(
            (
                "",
                f"🏫 <b>{escape(school.name, quote=False)}</b>",
                f"متوسط التشغيل: <code>{average.quantize(Decimal('0.01'))}%</code>",
            )
        )
        lines.extend(
            f"• {escape(report.screen.name, quote=False)}: "
            f"<code>{report.uptime_percent}%</code>"
            for report in reports
        )

    base_url = _site_base_url()
    alert, _created = queue_alert(
        event_type="screen_uptime_weekly",
        dedupe_key=f"screen-uptime-weekly:{week_start.isoformat()}",
        message="\n".join(lines),
        action_url=f"{base_url}/dashboard/admin-panel/" if base_url else "",
        action_label="فتح لوحة مدير النظام",
    )
    if alert is None:
        return {"schools_sent": 0, "reports_created": reports_created}

    report_ids = [
        report.pk
        for _school, reports in school_reports
        for report in reports
    ]
    ScreenWeeklyUptimeReport.objects.filter(pk__in=report_ids).update(sent_at=now)
    return {
        "schools_sent": len(school_reports),
        "reports_created": reports_created,
    }


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

PRUNE_BATCH_SIZE = 1000
PRUNE_MAX_BATCHES = 20


def _retention_days() -> int:
    try:
        value = int(getattr(settings, "DISPLAY_OPERATIONAL_RETENTION_DAYS", 180) or 180)
    except (TypeError, ValueError):
        value = 180
    return max(30, min(1095, value))


def _delete_in_batches(queryset, *, order_field: str) -> int:
    """Delete a queryset in bounded chunks.

    The first run on a table that has never been pruned can face a large
    backlog, and one statement over all of it would hold locks for as long as it
    takes. Chunking keeps every transaction short; whatever the cap leaves
    behind is taken by the next daily pass.
    """
    deleted = 0
    for _ in range(PRUNE_MAX_BATCHES):
        pks = list(queryset.order_by(order_field).values_list("pk", flat=True)[:PRUNE_BATCH_SIZE])
        if not pks:
            break
        removed, _detail = queryset.model.objects.filter(pk__in=pks).delete()
        deleted += int(removed or 0)
        if len(pks) < PRUNE_BATCH_SIZE:
            break
    return deleted


def prune_operational_data(*, now=None, retention_days: int | None = None) -> dict:
    """Drop operational history nothing reads any more.

    Two append-only logs grew without any retention policy: resolved screen
    outages and alerts that have already been delivered. Both are evidence for
    an incident that is over.

    What is deliberately never touched:

    * outages still open — they are live state, whatever their age;
    * alerts still pending or in flight — deleting one loses a message;
    * weekly uptime reports — one small row per screen per week, and they are
      the only long-term record of how a screen has behaved.
    """
    now = now or timezone.now()
    days = int(retention_days) if retention_days is not None else _retention_days()
    days = max(30, min(1095, days))
    cutoff = now - timedelta(days=days)

    from telegram_alerts.models import TelegramAlert

    outages = _delete_in_batches(
        ScreenOutage.objects.filter(resolved_at__isnull=False, resolved_at__lt=cutoff),
        order_field="resolved_at",
    )
    alerts = _delete_in_batches(
        TelegramAlert.objects.filter(
            status__in=(TelegramAlert.Status.SENT, TelegramAlert.Status.FAILED),
            updated_at__lt=cutoff,
        ),
        order_field="updated_at",
    )

    return {"outages": outages, "alerts": alerts, "cutoff": cutoff.isoformat(), "days": days}
