"""Readiness checks without publishing connection details or exception text."""
from __future__ import annotations

import secrets

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from redis import Redis


def readiness_status() -> dict[str, bool]:
    checks = {"database": False, "cache": False, "channels": False}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            checks["database"] = cursor.fetchone() == (1,)
    except Exception:
        pass

    key = f"readiness:{secrets.token_hex(16)}"
    try:
        cache.set(key, "ok", timeout=10)
        checks["cache"] = cache.get(key) == "ok"
    except Exception:
        pass
    finally:
        try:
            cache.delete(key)
        except Exception:
            pass

    url = str(getattr(settings, "CHANNELS_REDIS_URL", "") or "")
    backend = settings.CHANNEL_LAYERS.get("default", {}).get("BACKEND", "")
    if backend == "channels.layers.InMemoryChannelLayer":
        checks["channels"] = bool(settings.DEBUG or settings.RUNNING_TESTS)
    elif url:
        try:
            with Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1) as client:
                checks["channels"] = bool(client.ping())
        except Exception:
            pass
    return checks
