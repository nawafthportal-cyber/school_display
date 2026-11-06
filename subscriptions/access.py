"""Cached subscription access checks.

Both the dashboard middleware and the display token middleware need to answer
"is this school paid up?" on every single request. Hitting the database each
time does not scale to a screen fleet, so the answer is cached per school and
invalidated whenever a subscription changes.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.db import InterfaceError, OperationalError
from django.utils import timezone

from .utils import school_effective_max_screens, school_has_active_subscription


logger = logging.getLogger(__name__)

_ACTIVE_KEY = "subscriptions:active:{school_id}:{day}"
_LIMIT_KEY = "subscriptions:max_screens:{school_id}:{day}"


def school_display_is_suspended(school_id) -> bool:
    from core.models import School

    key = f"subscriptions:display_suspended:{school_id}"
    try:
        value = cache.get(key)
    except Exception:
        value = None
    if value is None:
        try:
            value = School.objects.filter(pk=school_id, display_suspended=True).exists()
        except (OperationalError, InterfaceError):
            # Preserve the existing display's availability during an outage,
            # just as the paid-subscription gate does. Known cached stops win.
            logger.exception("display_suspension_lookup_failed school_id=%s", school_id)
            return False
        try:
            cache.set(key, int(value), timeout=_ttl())
        except Exception:
            logger.debug("display_suspension_cache_set_failed school_id=%s", school_id)
    return bool(value)


def _ttl() -> int:
    return int(getattr(settings, "SUBSCRIPTION_ACCESS_CACHE_TTL", 300) or 300)


def _day_stamp() -> str:
    return timezone.localdate().isoformat()


def school_subscription_is_active(school_id: int | None) -> bool:
    """Cached form of :func:`school_has_active_subscription`."""
    if not school_id:
        return False

    key = _ACTIVE_KEY.format(school_id=int(school_id), day=_day_stamp())
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return bool(cached)

    value = school_has_active_subscription(school_id=int(school_id))
    try:
        cache.set(key, int(value), timeout=_ttl())
    except Exception:
        logger.debug("subscription_access_cache_set_failed school_id=%s", school_id)
    return value


def school_max_screens(school_id: int | None) -> int | None:
    """Cached form of :func:`school_effective_max_screens`.

    ``None`` means unlimited; ``0`` means no active subscription.
    """
    if not school_id:
        return 0

    key = _LIMIT_KEY.format(school_id=int(school_id), day=_day_stamp())
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        # ``-1`` is the on-the-wire encoding of "unlimited".
        return None if cached == -1 else int(cached)

    value = school_effective_max_screens(int(school_id))
    try:
        cache.set(key, -1 if value is None else int(value), timeout=_ttl())
    except Exception:
        logger.debug("subscription_limit_cache_set_failed school_id=%s", school_id)
    return value


def invalidate_school_subscription_cache(school_id: int | None) -> None:
    """Drop cached access answers so a renewal or lapse takes effect at once."""
    if not school_id:
        return
    day = _day_stamp()
    try:
        cache.delete_many(
            [
                _ACTIVE_KEY.format(school_id=int(school_id), day=day),
                _LIMIT_KEY.format(school_id=int(school_id), day=day),
                f"subscriptions:display_suspended:{school_id}",
            ]
        )
    except Exception:
        logger.debug("subscription_access_cache_invalidate_failed school_id=%s", school_id)
