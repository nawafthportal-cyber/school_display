from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from typing import Iterable

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone


def display_live_threshold_seconds() -> int:
    """Maximum heartbeat age that still counts as a live display."""
    try:
        value = int(getattr(settings, "DASHBOARD_SCREEN_LIVE_THRESHOLD_SEC", 120) or 120)
    except (TypeError, ValueError):
        value = 120
    return max(60, min(24 * 60 * 60, value))


def _presence_ttl_seconds() -> int:
    return max(180, display_live_threshold_seconds() + 60)


# The persisted column is what the monitor reads when the presence cache is
# cold, and the lowest offline threshold a school can configure is five minutes.
# Writing less often than this would let a cache restart present every screen as
# offline for the moment before the cache warms again.
LAST_SEEN_DB_INTERVAL_CEILING_SEC = 240


def _db_touch_interval_seconds() -> int:
    try:
        value = int(getattr(settings, "DISPLAY_LAST_SEEN_DB_INTERVAL_SEC", 240) or 240)
    except (TypeError, ValueError):
        value = 240
    return max(30, min(LAST_SEEN_DB_INTERVAL_CEILING_SEC, value))


def _identity_fragment(token: str | None) -> str:
    raw = str(token or "").strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if raw else "legacy"


def _presence_key(screen_id: int, token: str | None = None) -> str:
    return f"display:presence:screen:{int(screen_id)}:{_identity_fragment(token)}"


def _db_touch_key(screen_id: int, token: str | None = None) -> str:
    return f"display:presence:db_touch:{int(screen_id)}:{_identity_fragment(token)}"


def _last_seen_field_name(model) -> str | None:
    # Current installations use ``last_seen``. Keep the legacy name supported
    # so rolling deployments do not lose presence updates.
    for field_name in ("last_seen", "last_seen_at"):
        try:
            model._meta.get_field(field_name)
            return field_name
        except Exception:
            continue
    return None


def touch_display_presence(
    screen_id: int,
    *,
    token: str | None = None,
    seen_at: datetime | None = None,
) -> datetime | None:
    """Record a display heartbeat in shared cache and periodically in the DB."""
    try:
        screen_id = int(screen_id or 0)
    except (TypeError, ValueError):
        return None
    if screen_id <= 0:
        return None

    seen_at = seen_at or timezone.now()
    if timezone.is_naive(seen_at):
        seen_at = timezone.make_aware(seen_at, timezone.get_current_timezone())

    try:
        cache.set(_presence_key(screen_id, token), seen_at.timestamp(), timeout=_presence_ttl_seconds())
    except Exception:
        pass

    interval = _db_touch_interval_seconds()
    should_persist = True
    try:
        should_persist = bool(cache.add(_db_touch_key(screen_id, token), "1", timeout=interval))
    except Exception:
        pass

    if should_persist:
        try:
            DisplayScreen = apps.get_model("core", "DisplayScreen")
            field_name = _last_seen_field_name(DisplayScreen)
            if field_name:
                cutoff = seen_at - timedelta(seconds=interval)
                stale_filter = Q(**{f"{field_name}__isnull": True}) | Q(**{f"{field_name}__lt": cutoff})
                DisplayScreen.objects.filter(pk=screen_id).filter(stale_filter).update(**{field_name: seen_at})
        except Exception:
            # Presence is operational telemetry and must never break display data.
            pass

    return seen_at


def latest_display_presence(screen) -> datetime | None:
    """Return the newest cached heartbeat or persisted last-seen timestamp."""
    db_seen = None
    for field_name in ("last_seen", "last_seen_at"):
        value = getattr(screen, field_name, None)
        if value is not None and (db_seen is None or value > db_seen):
            db_seen = value

    cache_seen = None
    try:
        raw = cache.get(_presence_key(int(screen.pk), getattr(screen, "token", None)))
        if raw is not None:
            cache_seen = datetime.fromtimestamp(float(raw), tz=timezone.get_current_timezone())
    except Exception:
        cache_seen = None

    if cache_seen is not None and (db_seen is None or cache_seen > db_seen):
        return cache_seen
    return db_seen


def presence_map(screens: Iterable) -> dict[int, datetime | None]:
    """Batch version of :func:`latest_display_presence` for monitoring scans.

    The monitor walks every screen on every pass, so one cache round-trip per
    screen would dominate the scan. ``get_many`` keeps it to a single call.
    """
    screens = list(screens)
    keys: dict[str, int] = {}
    for screen in screens:
        try:
            keys[_presence_key(int(screen.pk), getattr(screen, "token", None))] = int(screen.pk)
        except (TypeError, ValueError):
            continue

    cached: dict[str, object] = {}
    if keys:
        try:
            cached = cache.get_many(list(keys.keys())) or {}
        except Exception:
            cached = {}

    tz = timezone.get_current_timezone()
    cache_seen: dict[int, datetime] = {}
    for key, raw in cached.items():
        screen_id = keys.get(key)
        if screen_id is None or raw is None:
            continue
        try:
            cache_seen[screen_id] = datetime.fromtimestamp(float(raw), tz=tz)
        except (TypeError, ValueError, OSError, OverflowError):
            continue

    out: dict[int, datetime | None] = {}
    for screen in screens:
        try:
            screen_id = int(screen.pk)
        except (TypeError, ValueError):
            continue
        db_seen = None
        for field_name in ("last_seen", "last_seen_at"):
            value = getattr(screen, field_name, None)
            if value is not None and (db_seen is None or value > db_seen):
                db_seen = value
        seen = cache_seen.get(screen_id)
        if seen is None or (db_seen is not None and db_seen > seen):
            seen = db_seen
        out[screen_id] = seen
    return out


def display_is_live(screen, *, now: datetime | None = None, require_bound: bool = True) -> bool:
    if not bool(getattr(screen, "is_active", False)):
        return False
    if require_bound and not (getattr(screen, "bound_device_id", "") or "").strip():
        return False
    seen_at = latest_display_presence(screen)
    if seen_at is None:
        return False
    now = now or timezone.now()
    try:
        return max(0, (now - seen_at).total_seconds()) <= display_live_threshold_seconds()
    except Exception:
        return False


def display_connection_state(screen, seen_at: datetime | None, *, now: datetime) -> str:
    if not screen.is_active:
        return "disabled"
    if not (screen.bound_device_id or "").strip():
        return "waiting"
    if seen_at is None:
        return "pending"
    return "live" if (now - seen_at).total_seconds() <= display_live_threshold_seconds() else "offline"


def live_display_count(screens: Iterable, *, now: datetime | None = None) -> int:
    now = now or timezone.now()
    return sum(1 for screen in screens if display_is_live(screen, now=now))
