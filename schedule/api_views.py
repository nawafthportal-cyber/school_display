# schedule/api_views.py
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import secrets
import socket
import time
from datetime import date, datetime
from datetime import time as dt_time
from typing import Iterable, Optional

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.core.cache import caches
from django.db import models
from django.db.models import Q
from django.http import HttpResponse, JsonResponse, HttpResponseNotModified
from django.templatetags.static import static
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from core.models import School, DisplayScreen
from core.display_presence import touch_display_presence
from schedule.models import (
    BELL_SOUND_DEFAULT,
    BELL_SOUND_STATIC_PATHS,
    SchoolSettings,
    ClassLesson,
    Period,
)
from schedule.time_engine import build_day_snapshot
from schedule.cache_utils import (
    get_cached_schedule_revision_for_school_id,
    set_cached_schedule_revision_for_school_id,
    status_metrics_bump,
    status_metrics_day_key,
    status_metrics_should_sample,
)
from schedule.snapshot_observability import (
    log_event as _obs_log_event,
    metric_add as _obs_metric_add,
    metric_incr as _obs_metric_incr,
    metric_set_max as _obs_metric_set_max,
    observe_snapshot_build as _obs_snapshot_build,
    observe_snapshot_cache as _obs_snapshot_cache,
    observe_snapshot_queue as _obs_snapshot_queue,
    snapshot_metrics_payload as _snapshot_metrics_payload,
)

logger = logging.getLogger(__name__)

SNAPSHOT_CACHE_NAMESPACE = "v11"


def _snapshot_ttl_floor_seconds() -> int:
    # Keep origin snapshot bodies warm long enough to survive fleet poll intervals.
    try:
        v = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_TTL_MIN_SECONDS", 300) or 300)
    except Exception:
        v = 300
    return max(300, min(3600, v))


def _stable_ttl_with_jitter(ttl: int, seed: str) -> int:
    try:
        ttl_i = int(ttl)
    except Exception:
        ttl_i = _snapshot_ttl_floor_seconds()

    base_ttl = max(_snapshot_ttl_floor_seconds(), ttl_i)

    try:
        jitter = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_TTL_JITTER_SEC", 30) or 30)
    except Exception:
        jitter = 30
    jitter = max(0, min(300, jitter))

    if jitter <= 0:
        return base_ttl

    try:
        h = int(hashlib.sha1(str(seed).encode("utf-8")).hexdigest(), 16)
        delta = (h % ((jitter * 2) + 1)) - jitter
    except Exception:
        delta = 0
    return max(_snapshot_ttl_floor_seconds(), base_ttl + delta)


def _cache_redis_url_configured() -> bool:
    try:
        if str(getattr(dj_settings, "CACHE_REDIS_URL", "") or "").strip():
            return True
    except Exception:
        pass
    try:
        return bool(
            os.getenv("REDIS_CACHE_URL", "").strip()
            or os.getenv("CACHE_REDIS_URL", "").strip()
            or os.getenv("REDIS_URL", "").strip()
        )
    except Exception:
        return False


def _channels_redis_url_configured() -> bool:
    try:
        if str(getattr(dj_settings, "CHANNELS_REDIS_URL", "") or "").strip():
            return True
    except Exception:
        pass
    try:
        return bool(
            os.getenv("REDIS_CHANNELS_URL", "").strip()
            or os.getenv("CHANNELS_REDIS_URL", "").strip()
            or os.getenv("CHANNEL_REDIS_URL", "").strip()
            or os.getenv("REDIS_URL", "").strip()
        )
    except Exception:
        return False


def _cache_is_shared() -> bool:
    """Best-effort check whether the configured cache is shared across processes.

    In production we expect Redis (django-redis). If Redis isn't configured and
    LocMemCache is used, relying on cache-only revision comparisons can cause
    some devices (e.g., TVs) to never detect updates if they hit a different
    worker/process.
    """
    try:
        if _cache_redis_url_configured():
            return True
    except Exception:
        pass
    try:
        default_cache = dj_settings.CACHES.get("default", {})
        backend = str(default_cache.get("BACKEND", "") or "").lower()
        return "django_redis" in backend or "rediscache" in backend
    except Exception:
        return False


def _log_cache_env(logger: logging.Logger) -> None:
    try:
        default_cache = dj_settings.CACHES.get("default", {})
        backend = str(default_cache.get("BACKEND", "") or "")
        location = str(default_cache.get("LOCATION", "") or "")
        safe_location = location.split("@")[-1] if location else ""
        logger.info(
            "cache_env backend=%s location=%s host=%s",
            backend,
            safe_location,
            socket.gethostname(),
        )
    except Exception:
        try:
            logger.info("cache_env backend=unknown")
        except Exception:
            pass


def _steady_cache_log_enabled() -> bool:
    try:
        if bool(getattr(dj_settings, "DEBUG", False)):
            return True
    except Exception:
        pass
    return (os.getenv("DISPLAY_STEADY_CACHE_LOG", "").strip() == "1")


def _safe_snapshot_rollout_enabled() -> bool:
    try:
        return bool(getattr(dj_settings, "SNAPSHOT_STEADY_CACHE_V2", False))
    except Exception:
        return False


def _cache_backend_name() -> str:
    try:
        backend = caches["default"]
        return f"{backend.__class__.__module__}.{backend.__class__.__name__}"
    except Exception:
        return f"{cache.__class__.__module__}.{cache.__class__.__name__}"


def _steady_cache_key_for_school_rev(school_id: int, rev: int, *, day_key: object | None = None) -> str:
    return (
        f"snapshot:{SNAPSHOT_CACHE_NAMESPACE}:school:{int(school_id)}:"
        f"day:{_snapshot_cache_day_key(day_key)}"
    )


def _acquire_build_lock(school_id: int, rev: int, *, day_key: object | None = None) -> bool:
    lock_key = (
        f"lock:snapshot:{int(school_id)}:"
        f"day:{_snapshot_cache_day_key(day_key)}"
    )
    try:
        return bool(cache.add(lock_key, "1", timeout=8))
    except Exception:
        return True


def _stale_snapshot_fallback_key(school_id: int, *, day_key: object | None = None) -> str:
    return f"snapshot:last:{SNAPSHOT_CACHE_NAMESPACE}:{int(school_id)}:{_snapshot_cache_day_key(day_key)}"


def _get_stale_snapshot_fallback(school_id: int, *, day_key: object | None = None) -> dict | None:
    stale_key = _stale_snapshot_fallback_key(int(school_id), day_key=day_key)
    try:
        entry, _ = _validated_snapshot_cache_entry_from_value(
            cache.get(stale_key),
            cache_key=stale_key,
            reject_past_wake_boundary=True,
        )
        if isinstance(entry, dict) and isinstance(entry.get("snap"), dict):
            snap = entry["snap"]
            snap.setdefault("meta", {})
            snap["meta"]["is_stale"] = True
            return snap
    except Exception:
        pass

    return None


def _snapshot_decision_log_interval_seconds() -> int:
    try:
        v = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_DECISION_LOG_INTERVAL_SEC", 30) or 30)
    except Exception:
        v = 30
    return max(5, min(300, v))


def _log_snapshot_cache_decision(
    *,
    school_id: int,
    day_key: str,
    cache_key: str,
    cache_hit: bool,
    cache_miss: bool,
    rebuild_reason: str,
) -> None:
    reason = str(rebuild_reason or "none").strip().lower() or "none"
    try:
        interval = _snapshot_decision_log_interval_seconds()
        throttle = (
            f"log:snapshot_decision:{int(school_id)}:{str(day_key)}:"
            f"{int(bool(cache_hit))}:{int(bool(cache_miss))}:{reason}:{int(interval)}"
        )
        if not bool(cache.add(throttle, "1", timeout=interval)):
            return
    except Exception:
        pass

    try:
        _obs_log_event(
            logger,
            "snapshot_cache_decision",
            school_id=int(school_id),
            day_key=str(day_key),
            cache_hit=bool(cache_hit),
            cache_miss=bool(cache_miss),
            rebuild_reason=reason,
            cache_key=cache_key,
        )
    except Exception:
        pass


def get_or_build_snapshot(school_id: int, rev: int, builder, *, day_key: object | None = None):
    """
    Returns: (cache_entry, cache_kind)
      cache_kind ∈ {"HIT", "MISS", "STALE", "BYPASS", "QUEUED"}
    """
    key = _steady_cache_key_for_school_rev(school_id, rev, day_key=day_key)
    normalized_day_key = _snapshot_cache_day_key(day_key)

    try:
        entry, entry_reason = _validated_snapshot_cache_entry_from_value(
            cache.get(key),
            min_rev=int(rev),
            cache_key=key,
        )
    except Exception:
        entry = None
        entry_reason = "missing"

    is_hit = isinstance(entry, dict) and isinstance(entry.get("snap"), dict)
    _log_steady_get(key, hit=is_hit, school_id=school_id, rev=rev)

    if is_hit:
        try:
            _metrics_incr("metrics:snapshot_cache:steady_hit")
            _obs_snapshot_cache(
                logger=logger,
                outcome="hit",
                layer="steady",
                school_id=int(school_id),
                rev=int(rev),
                day_key=normalized_day_key,
                cache_key=key,
            )
        except Exception:
            pass
        _log_snapshot_cache_decision(
            school_id=int(school_id),
            day_key=normalized_day_key,
            cache_key=key,
            cache_hit=True,
            cache_miss=False,
            rebuild_reason="none",
        )
        return entry, "HIT"

    rebuild_reason = entry_reason if entry_reason not in {"", "hit", "missing"} else "request_miss"
    try:
        _metrics_incr("metrics:snapshot_cache:steady_miss")
        _obs_snapshot_cache(
            logger=logger,
            outcome="miss",
            layer="steady",
            school_id=int(school_id),
            rev=int(rev),
            day_key=normalized_day_key,
            cache_key=key,
            reason=rebuild_reason,
        )
    except Exception:
        pass

    try:
        from schedule.snapshot_materializer import (
            enqueue_snapshot_build,
            snapshot_async_build_enabled,
            snapshot_inline_fallback_enabled,
            snapshot_queue_available,
            snapshot_worker_status,
            wait_for_materialized_snapshot,
        )

        async_enabled = bool(snapshot_async_build_enabled())
        queue_available = bool(snapshot_queue_available())
        worker_status = snapshot_worker_status() if async_enabled else {}
        worker_alive = bool(worker_status.get("alive"))
        inline_fallback = bool(snapshot_inline_fallback_enabled())
    except Exception:
        async_enabled = False
        queue_available = False
        worker_alive = False
        inline_fallback = True

    if async_enabled and queue_available and worker_alive:
        _metrics_incr("metrics:snapshot_queue:requested")
        _obs_snapshot_queue(
            logger=logger,
            decision="attempt",
            school_id=int(school_id),
            rev=int(rev),
            day_key=normalized_day_key,
            reason=rebuild_reason,
        )
        queue_result = enqueue_snapshot_build(
            school_id=int(school_id),
            rev=int(rev),
            day_key=normalized_day_key,
            reason=rebuild_reason,
        )
        try:
            job = queue_result.get("job") if isinstance(queue_result, dict) else None
            job_id = job.get("job_id") if isinstance(job, dict) else None
            _obs_snapshot_queue(
                logger=logger,
                decision=(
                    "queued"
                    if queue_result.get("queued")
                    else "deduped"
                    if queue_result.get("deduped")
                    else "skipped"
                ) if isinstance(queue_result, dict) else "skipped",
                school_id=int(school_id),
                rev=int(rev),
                latest_rev=queue_result.get("latest_rev") if isinstance(queue_result, dict) else None,
                day_key=normalized_day_key,
                reason=queue_result.get("reason") if isinstance(queue_result, dict) else None,
                job_id=job_id,
            )
        except Exception:
            _obs_snapshot_queue(
                logger=logger,
                decision="skipped",
                school_id=int(school_id),
                rev=int(rev),
                day_key=normalized_day_key,
                reason="result_unavailable",
            )
        queued_or_existing = bool(
            queue_result.get("queued")
            or queue_result.get("duplicate")
            or queue_result.get("deduped")
            or queue_result.get("debounced")
            or queue_result.get("coalesced")
        )

        waited_entry = wait_for_materialized_snapshot(
            school_id=int(school_id),
            rev=int(rev),
            day_key=normalized_day_key,
        )
        if isinstance(waited_entry, dict) and isinstance(waited_entry.get("snap"), dict):
            _metrics_incr("metrics:snapshot_queue:served_after_wait")
            _obs_snapshot_queue(
                logger=logger,
                decision="served_after_wait",
                school_id=int(school_id),
                rev=int(rev),
                day_key=normalized_day_key,
            )
            _log_snapshot_cache_decision(
                school_id=int(school_id),
                day_key=normalized_day_key,
                cache_key=key,
                cache_hit=True,
                cache_miss=False,
                rebuild_reason="served_after_queue_wait",
            )
            return waited_entry, "HIT"

        fallback = _get_stale_snapshot_fallback(school_id, day_key=normalized_day_key)
        if isinstance(fallback, dict):
            try:
                meta = fallback.get("meta")
                if not isinstance(meta, dict):
                    meta = {}
                    fallback["meta"] = meta
                meta["cache"] = "STALE"
                meta["cache_key"] = key
                meta["revalidate"] = "queued"
                meta["rebuild_reason"] = f"{rebuild_reason}_queue_wait_timeout_stale"
            except Exception:
                pass
            _log_snapshot_cache_decision(
                school_id=int(school_id),
                day_key=normalized_day_key,
                cache_key=key,
                cache_hit=False,
                cache_miss=True,
                rebuild_reason=f"{rebuild_reason}_queue_wait_timeout_stale",
            )
            _obs_snapshot_build(
                logger=logger,
                stage="end",
                source="stale",
                school_id=int(school_id),
                rev=int(rev),
                day_key=normalized_day_key,
                duration_ms=0,
                reason=f"{rebuild_reason}_queue_wait_timeout_stale",
            )
            return _snapshot_cache_entry(fallback), "STALE"

        if queued_or_existing and not inline_fallback:
            _metrics_incr("metrics:snapshot_queue:building_payload")
            building = _fallback_building_payload(
                school_id=int(school_id),
                rev=int(rev),
                day_key=normalized_day_key,
                reason=f"{rebuild_reason}_queued_for_materialization",
                refresh_interval_sec=3,
            )
            _log_snapshot_cache_decision(
                school_id=int(school_id),
                day_key=normalized_day_key,
                cache_key=key,
                cache_hit=False,
                cache_miss=True,
                rebuild_reason=f"{rebuild_reason}_queued_async",
            )
            return _snapshot_cache_entry(building), "QUEUED"
    elif async_enabled and queue_available and not worker_alive:
        _metrics_incr("metrics:snapshot_queue:worker_unavailable")

    if _acquire_build_lock(school_id, rev, day_key=normalized_day_key):
        try:
            build_started = time.monotonic()
            _obs_snapshot_build(
                logger=logger,
                stage="start",
                source="inline",
                school_id=int(school_id),
                rev=int(rev),
                day_key=normalized_day_key,
                reason=rebuild_reason,
            )
            snap = builder()

            if not isinstance(snap, dict):
                snap = _fallback_payload("تعذر تجهيز بيانات الشاشة")

            # TTL ديناميكي حسب نوع الحالة داخل snapshot
            try:
                ttl = int(compute_dynamic_ttl_seconds(snap))
            except Exception:
                ttl = int(getattr(dj_settings, "SCHOOL_SNAPSHOT_TTL", 1200) or 1200)

            ttl = _stable_ttl_with_jitter(ttl, seed=f"{school_id}:{rev}")

            # stale fallback أطول قليلًا من الـ TTL الأساسي
            try:
                stale_ttl = int(_active_fallback_steady_ttl_seconds(snap)) if _is_active_window(snap) else max(600, ttl)
            except Exception:
                stale_ttl = max(600, ttl)

            try:
                meta = snap.get("meta")
                if not isinstance(meta, dict):
                    meta = {}
                    snap["meta"] = meta
                meta["cache"] = "MISS"
                meta["cache_key"] = key
                meta["cache_ttl"] = int(ttl)
                meta["generated_at"] = timezone.now().isoformat()
                meta["rebuild_reason"] = rebuild_reason
            except Exception:
                pass

            entry = _snapshot_cache_entry(snap)

            try:
                cache.set(key, entry, timeout=ttl)
                cache.set(
                    _stale_snapshot_fallback_key(int(school_id), day_key=normalized_day_key),
                    entry,
                    timeout=stale_ttl,
                )
                _log_steady_set(
                    key,
                    ttl=int(ttl),
                    school_id=school_id,
                    rev=rev,
                    success=True,
                )
            except Exception as e:
                _log_steady_set(
                    key,
                    ttl=int(ttl),
                    school_id=school_id,
                    rev=rev,
                    success=False,
                    error=e.__class__.__name__,
                )

            build_ms = int((time.monotonic() - build_started) * 1000)
            _obs_snapshot_build(
                logger=logger,
                stage="end",
                source="inline",
                school_id=int(school_id),
                rev=int(rev),
                day_key=normalized_day_key,
                duration_ms=build_ms,
                reason=rebuild_reason,
                payload_bytes=len(_snapshot_entry_body_bytes(entry)),
            )
            try:
                meta = snap.get("meta")
                if not isinstance(meta, dict):
                    meta = {}
                    snap["meta"] = meta
                meta.setdefault("snapshot_build_ms", build_ms)
            except Exception:
                pass

            _log_snapshot_cache_decision(
                school_id=int(school_id),
                day_key=normalized_day_key,
                cache_key=key,
                cache_hit=False,
                cache_miss=True,
                rebuild_reason=rebuild_reason,
            )

            return entry, "MISS"
        finally:
            try:
                cache.delete(
                    f"lock:snapshot:{int(school_id)}:"
                    f"day:{normalized_day_key}"
                )
            except Exception:
                pass
    else:
        try:
            _metrics_incr("metrics:snapshot_cache:build_lock_contention")
            _obs_snapshot_cache(
                logger=logger,
                outcome="miss",
                layer="steady",
                school_id=int(school_id),
                rev=int(rev),
                day_key=normalized_day_key,
                cache_key=key,
                reason="build_lock_contention",
            )
        except Exception:
            pass
        waited_entry, waited_reason = _wait_for_valid_snapshot_entry(
            key,
            min_rev=int(rev),
            timeout_s=0.25,
            step_s=0.05,
        )
        if isinstance(waited_entry, dict) and isinstance(waited_entry.get("snap"), dict):
            _log_snapshot_cache_decision(
                school_id=int(school_id),
                day_key=normalized_day_key,
                cache_key=key,
                cache_hit=True,
                cache_miss=False,
                rebuild_reason="served_after_lock_wait",
            )
            return waited_entry, "HIT"
        rebuild_reason = waited_reason if waited_reason not in {"", "hit", "missing"} else "build_lock_contention"

    fallback = _get_stale_snapshot_fallback(school_id, day_key=day_key)
    if isinstance(fallback, dict):
        try:
            meta = fallback.get("meta")
            if not isinstance(meta, dict):
                meta = {}
                fallback["meta"] = meta
            meta["cache"] = "STALE"
            meta["cache_key"] = key
            meta["rebuild_reason"] = f"{rebuild_reason}_stale"
        except Exception:
            pass
        _log_snapshot_cache_decision(
            school_id=int(school_id),
            day_key=normalized_day_key,
            cache_key=key,
            cache_hit=False,
            cache_miss=True,
            rebuild_reason=f"{rebuild_reason}_stale",
        )
        _obs_snapshot_build(
            logger=logger,
            stage="end",
            source="stale",
            school_id=int(school_id),
            rev=int(rev),
            day_key=normalized_day_key,
            duration_ms=0,
            reason=f"{rebuild_reason}_stale",
        )
        return _snapshot_cache_entry(fallback), "STALE"

    # إذا لم نجد fallback، نبني مباشرة كحل أخير
    snap = builder()
    if not isinstance(snap, dict):
        snap = _fallback_payload("تعذر تجهيز بيانات الشاشة")
    try:
        meta = snap.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            snap["meta"] = meta
        meta["cache"] = "BYPASS"
        meta["cache_key"] = key
        meta["rebuild_reason"] = f"{rebuild_reason}_no_stale"
    except Exception:
        pass
    _log_snapshot_cache_decision(
        school_id=int(school_id),
        day_key=normalized_day_key,
        cache_key=key,
        cache_hit=False,
        cache_miss=True,
        rebuild_reason=f"{rebuild_reason}_no_stale",
    )
    return _snapshot_cache_entry(snap), "BYPASS"

def _log_steady_get(key: str, *, hit: bool, school_id: int | None, rev: int | None) -> None:
    if not _steady_cache_log_enabled():
        return
    try:
        # Throttle per key to avoid production log storms.
        throttle = f"log:steady_get:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}"
        if not bool(cache.add(throttle, "1", timeout=10)):
            return
    except Exception:
        pass
    try:
        logger.info(
            "steady_get key=%s hit=%s school_id=%s rev=%s backend=%s host=%s",
            key,
            "1" if hit else "0",
            school_id,
            rev,
            _cache_backend_name(),
            socket.gethostname(),
        )
    except Exception:
        pass


def _log_steady_set(
    key: str,
    *,
    ttl: int,
    school_id: int | None,
    rev: int | None,
    success: bool = True,
    error: str = "",
) -> None:
    if not _steady_cache_log_enabled():
        return
    try:
        throttle = f"log:steady_set:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}"
        if not bool(cache.add(throttle, "1", timeout=10)):
            return
    except Exception:
        pass
    try:
        logger.info(
            "steady_set key=%s ttl=%s school_id=%s rev=%s success=%s error=%s backend=%s host=%s",
            key,
            int(ttl),
            school_id,
            rev,
            "1" if success else "0",
            (error or "")[:80],
            _cache_backend_name(),
            socket.gethostname(),
        )
    except Exception:
        pass


def _metrics_interval_seconds() -> int:
    # Log cache hit/miss metrics at INFO at most once per N seconds.
    # Keep it conservative to avoid log noise in SaaS.
    try:
        v = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_CACHE_METRICS_INTERVAL_SEC", os.getenv("DISPLAY_SNAPSHOT_CACHE_METRICS_INTERVAL_SEC", "600")))
    except Exception:
        v = 600
    return max(60, min(3600, v))


def _status_log_interval_seconds() -> int:
    # For large fleets, logging every status poll is too noisy.
    # Default: log at most once per token per 5 minutes.
    try:
        v = int(getattr(dj_settings, "DISPLAY_STATUS_LOG_INTERVAL_SEC", os.getenv("DISPLAY_STATUS_LOG_INTERVAL_SEC", "300")))
    except Exception:
        v = 300
    return max(30, min(3600, v))


def _status_log_interval_200_seconds() -> int:
    # Status 200 can spike during update waves; use a shorter dedicated window.
    try:
        v = int(getattr(dj_settings, "DISPLAY_STATUS_200_LOG_INTERVAL_SEC", os.getenv("DISPLAY_STATUS_200_LOG_INTERVAL_SEC", "120")))
    except Exception:
        v = 120
    return max(10, min(3600, v))


def _status_warn_log_interval_seconds() -> int:
    # Operational warnings should be visible but still throttled at fleet scale.
    try:
        v = int(getattr(dj_settings, "DISPLAY_STATUS_WARN_LOG_INTERVAL_SEC", os.getenv("DISPLAY_STATUS_WARN_LOG_INTERVAL_SEC", "300")))
    except Exception:
        v = 300
    return max(30, min(3600, v))


def _snapshot_resp_log_interval_seconds() -> int:
    try:
        v = int(
            getattr(
                dj_settings,
                "DISPLAY_SNAPSHOT_RESP_LOG_INTERVAL_SEC",
                os.getenv("DISPLAY_SNAPSHOT_RESP_LOG_INTERVAL_SEC", "60"),
            )
        )
    except Exception:
        v = 60
    return max(10, min(3600, v))


def _should_log_snapshot_resp(*, school_id: int | None, rev: int | None, cache_status: str | None, status_code: int) -> bool:
    status_text = str(cache_status or "").strip().upper()
    if status_text in {"MISS", "STALE", "BYPASS", "ERROR", "LOOP"}:
        return True
    try:
        interval = _snapshot_resp_log_interval_seconds()
        sid = int(school_id or 0)
        rv = int(rev or 0)
        key = f"log:snapshot_resp:{sid}:{rv}:{status_text or 'NONE'}:{int(status_code)}:{interval}"
        return bool(cache.add(key, "1", timeout=interval))
    except Exception:
        return True


def _should_log_status(token_hash: str, *, interval: int | None = None) -> bool:
    interval = int(interval if interval is not None else _status_log_interval_seconds())
    key = f"log:status_poll:{token_hash[:12]}:{interval}"
    try:
        return bool(cache.add(key, "1", timeout=interval))
    except Exception:
        return True


def _should_log_status_200_school_rev(*, school_id: int, rev: int) -> bool:
    """Throttle noisy status=200 logs for large fleets.

    We want at most one INFO log per (school_id, rev) per interval regardless
    of how many screens poll.
    """
    interval = _status_log_interval_200_seconds()
    if not school_id:
        return True
    try:
        key = f"log:status_poll_200:{int(school_id)}:{int(rev)}:{int(interval)}"
        return bool(cache.add(key, "1", timeout=interval))
    except Exception:
        return True


def _get_school_revision_cache_only(school_id: int) -> tuple[int | None, str]:
    """Return (revision, source) without any DB fallback.

    This keeps /api/display/status cache-only. If revision cache is missing,
    we return (None, "none") and let the client fetch snapshot.
    """
    if not school_id:
        return None, "none"

    _metrics_incr("metrics:status:cache_get")
    try:
        cached = get_cached_schedule_revision_for_school_id(int(school_id))
    except Exception:
        cached = None
    if cached is not None:
        _metrics_incr("metrics:status:rev_cache_hit")
        return int(cached), "cache"
    _metrics_incr("metrics:status:rev_none")
    return None, "none"


def _should_log_status_304_sample(token_hash: str) -> bool:
    # ~1% sampling based on token hash prefix.
    try:
        return int(token_hash[:2], 16) < 3  # 3/256 ≈ 1.17%
    except Exception:
        return False


def _get_school_revision_cached(school_id: int) -> tuple[int | None, str]:
    """Return (revision, source) where source is cache|db|none."""
    if not school_id:
        return None, "none"

    # If cache is not shared (e.g. LocMemCache), do not trust cached values.
    # Always refresh from DB to ensure all workers see updates.
    if _cache_is_shared():
        _metrics_incr("metrics:status:cache_get")
        cached = get_cached_schedule_revision_for_school_id(int(school_id))
        if cached is not None:
            _metrics_incr("metrics:status:rev_cache_hit")
            return int(cached), "cache"

    try:
        _metrics_incr("metrics:status:rev_db")
        rev = int(
            SchoolSettings.objects.filter(school_id=int(school_id)).values_list("schedule_revision", flat=True).first()
            or 0
        )
        _metrics_incr("metrics:status:cache_set")
        set_cached_schedule_revision_for_school_id(int(school_id), int(rev))
        return int(rev), "db"
    except Exception:
        _metrics_incr("metrics:status:rev_none")
        return None, "none"

def _metrics_incr(key: str) -> None:
    _obs_metric_incr(key)


@require_GET
def metrics(request):
    """Expose internal counters for load testing / debugging.

    Disabled by default in production.
    - Enabled when DEBUG=True OR DISPLAY_METRICS_ENABLED=1
    - Optional auth via DISPLAY_METRICS_KEY + X-Display-Metrics-Key header
    """
    is_debug = bool(getattr(dj_settings, "DEBUG", False))
    enabled_flag = (os.getenv("DISPLAY_METRICS_ENABLED", "").strip() == "1")

    # Production: endpoint is OFF unless explicitly enabled.
    # When enabled in production, a key is mandatory.
    if (not is_debug) and (not enabled_flag):
        return JsonResponse({"detail": "not_found"}, status=404)

    required_key = os.getenv("DISPLAY_METRICS_KEY", "").strip()
    if (not is_debug) and (not required_key):
        return JsonResponse({"detail": "not_found"}, status=404)

    if required_key:
        provided = (request.headers.get("X-Display-Metrics-Key") or "").strip()
        if not secrets.compare_digest(provided, required_key):
            return JsonResponse({"detail": "forbidden"}, status=403)

    keys = [
        "metrics:status:requests",
        "metrics:status:resp_200",
        "metrics:status:resp_304",
        "metrics:status:cache_get",
        "metrics:status:cache_set",
        "metrics:status:rev_cache_hit",
        "metrics:status:rev_db",
        "metrics:status:rev_none",
        "metrics:status:fallback_poll",

        # Snapshot cache counters
        "metrics:snapshot_cache:token_hit",
        "metrics:snapshot_cache:token_miss",
        "metrics:snapshot_cache:school_hit",
        "metrics:snapshot_cache:school_miss",
        "metrics:snapshot_cache:steady_hit",
        "metrics:snapshot_cache:steady_miss",
        "metrics:snapshot_cache:stale_fallback",
        "metrics:snapshot_cache:hit",
        "metrics:snapshot_cache:miss",
        "metrics:snapshot_cache:revision_reject",
        "metrics:snapshot_cache:wake_boundary_reject",
        "metrics:snapshot_cache:build_lock_contention",

        # Snapshot build metrics
        "metrics:snapshot_cache:build_count",
        "metrics:snapshot_cache:build_sum_ms",
        "metrics:snapshot_cache:build_max_ms",
        "metrics:snapshot_queue:requested",
        "metrics:snapshot_queue:enqueued",
        "metrics:snapshot_queue:deduped",
        "metrics:snapshot_queue:coalesced",
        "metrics:snapshot_queue:dequeued",
        "metrics:snapshot_queue:materialized",
        "metrics:snapshot_queue:error",
        "metrics:snapshot_queue:debounced",
        "metrics:snapshot_queue:latest_revision_replaced",
        "metrics:snapshot_queue:queue_skipped_outdated",
        "metrics:snapshot_queue:outdated_job_dropped",
        "metrics:snapshot_queue:worker_unavailable",
        "metrics:snapshot_queue:building_payload",
        "metrics:snapshot_queue:served_after_wait",
        "metrics:snapshot_queue:lock_busy",
        "metrics:snapshot_queue:already_materialized",
        "metrics:snapshot_queue:settings_missing",
        "metrics:snapshot_queue:queue_wait_sum_ms",
        "metrics:snapshot_queue:queue_wait_max_ms",
        "metrics:snapshot_queue:process_sum_ms",
        "metrics:snapshot_queue:process_max_ms",
        "metrics:snapshot_queue:e2e_sum_ms",
        "metrics:snapshot_queue:e2e_max_ms",
        "metrics:snapshot_build:count",
        "metrics:snapshot_build:soft_timeout",
        "metrics:snapshot_build:duration_ms:sum",
        "metrics:snapshot_build:duration_ms:max",
        "metrics:snapshot_build:source:inline:count",
        "metrics:snapshot_build:source:inline:duration_ms:sum",
        "metrics:snapshot_build:source:inline:duration_ms:max",
        "metrics:snapshot_build:source:queue:count",
        "metrics:snapshot_build:source:queue:duration_ms:sum",
        "metrics:snapshot_build:source:queue:duration_ms:max",
        "metrics:snapshot_build:source:stale:count",
        "metrics:snapshot_build:source:stale:duration_ms:sum",
        "metrics:snapshot_build:source:stale:duration_ms:max",
        "metrics:snapshot_queue:enqueue_count",
        "metrics:snapshot_queue:skipped_enqueue",
        "metrics:snapshot_queue:deduplicated_jobs",
        "metrics:snapshot_queue:queue_wait_time_ms:sum",
        "metrics:snapshot_queue:queue_wait_time_ms:max",
        "metrics:snapshot_queue:queue_wait_time_ms:count",
    ]

    try:
        backend = caches["default"]
        backend_name = f"{backend.__class__.__module__}.{backend.__class__.__name__}"
    except Exception:
        backend_name = f"{cache.__class__.__module__}.{cache.__class__.__name__}"

    out: dict[str, object] = {
        "server_time": int(time.time()),
        "hostname": (socket.gethostname() or "").strip(),
        "process_id": int(os.getpid()),
        "cache_backend": backend_name,
        "redis_url_configured": bool(
            os.getenv("REDIS_URL", "").strip()
            or os.getenv("REDIS_CACHE_URL", "").strip()
            or os.getenv("REDIS_CHANNELS_URL", "").strip()
            or os.getenv("CACHE_REDIS_URL", "").strip()
            or os.getenv("CHANNELS_REDIS_URL", "").strip()
            or os.getenv("CHANNEL_REDIS_URL", "").strip()
        ),
        "cache_redis_url_configured": _cache_redis_url_configured(),
        "channels_redis_url_configured": _channels_redis_url_configured(),
        "cache_is_shared": _cache_is_shared(),
        "cache_key_prefix": os.getenv("CACHE_KEY_PREFIX", "school_display"),
    }

    try:
        from core.redis_topology import redis_topology_summary

        topology = redis_topology_summary()
        out["redis_topology_split"] = bool(topology.get("split"))
        out["redis_topology_shared"] = bool(topology.get("shared"))
        out["redis_topology_warnings"] = topology.get("warnings", [])
    except Exception:
        out["redis_topology_split"] = False
        out["redis_topology_shared"] = False
        out["redis_topology_warnings"] = []

    try:
        from schedule.snapshot_materializer import snapshot_worker_status
        from schedule.snapshot_materializer import snapshot_async_build_enabled, snapshot_inline_fallback_enabled

        worker_status = snapshot_worker_status()
        out["snapshot_async_build_enabled"] = bool(snapshot_async_build_enabled())
        out["snapshot_inline_fallback_enabled"] = bool(snapshot_inline_fallback_enabled())
        out["snapshot_worker_alive"] = bool(worker_status.get("alive"))
        out["snapshot_worker_age_sec"] = worker_status.get("age_sec")
        out["snapshot_worker_id"] = worker_status.get("worker_id")
        out["snapshot_queue_available"] = bool(worker_status.get("queue_available"))
        out["snapshot_queue_depth"] = worker_status.get("queue_depth")
    except Exception:
        out["snapshot_async_build_enabled"] = False
        out["snapshot_inline_fallback_enabled"] = True
        out["snapshot_worker_alive"] = False
        out["snapshot_worker_age_sec"] = None
        out["snapshot_worker_id"] = None
        out["snapshot_queue_available"] = False
        out["snapshot_queue_depth"] = None

    school_id_raw = (request.GET.get("school_id") or "").strip()
    if school_id_raw.isdigit():
        sid = int(school_id_raw)
        try:
            out["school_snapshot_debug"] = {
                "school_id": sid,
                "current_revision_cache": get_cached_schedule_revision_for_school_id(sid),
                "last_snapshot_revision": cache.get(f"metrics:snapshot_cache:last_rev:{sid}"),
                "last_snapshot_cache_status": cache.get(f"metrics:snapshot_cache:last_cache_status:{sid}"),
                "last_snapshot_payload_bytes": int(cache.get(f"metrics:snapshot_cache:last_payload_bytes:{sid}") or 0),
            }
        except Exception:
            out["school_snapshot_debug"] = {"school_id": sid}

    def _sanitize_error(msg: str) -> str:
        # Avoid leaking connection strings or credentials in metrics output.
        try:
            s = (msg or "").strip()
            if not s:
                return ""
            # Redact any redis/rediss URLs.
            for scheme in ("redis://", "rediss://"):
                if scheme in s:
                    # Keep only scheme and a marker.
                    s = s.replace(scheme, scheme + "***")
            return s[:200]
        except Exception:
            return ""

    # Optional: validate Redis connectivity (best-effort, no hard dependency).
    # This helps distinguish "Redis configured" vs "Redis actually reachable".
    try:
        from django_redis import get_redis_connection  # type: ignore

        t0 = time.monotonic()
        conn = get_redis_connection("default")
        ok = bool(conn.ping())
        out["redis_ping_ok"] = ok
        out["redis_ping_ms"] = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        out["redis_ping_ok"] = False
        out["redis_ping_error"] = e.__class__.__name__
        out["redis_ping_error_detail"] = _sanitize_error(str(e))

    # Optional: shared-cache probe.
    # If you call metrics repeatedly and see different process_id values but the same probe_last,
    # that's strong evidence the cache is shared across workers.
    try:
        probe = (request.GET.get("probe") or "").strip().lower() in {"1", "true", "yes"}
        if not probe:
            probe = (request.headers.get("X-Display-Metrics-Probe") or "").strip().lower() in {"1", "true", "yes"}
        if probe:
            probe_key = f"metrics:cache_probe:{os.getenv('CACHE_KEY_PREFIX', 'school_display')}"
            prev = cache.get(probe_key)
            out["cache_probe_last"] = prev
            payload = {"pid": int(os.getpid()), "ts": int(time.time())}
            cache.set(probe_key, payload, timeout=120)
            out["cache_probe_written"] = payload
    except Exception as e:
        out["cache_probe_error"] = str(e)[:200]

    for k in keys:
        try:
            v = cache.get(k)
            out[k] = int(v) if v is not None else 0
        except Exception:
            out[k] = 0

    # Derived metrics
    try:
        bc = int(out.get("metrics:snapshot_cache:build_count", 0) or 0)
        bsum = int(out.get("metrics:snapshot_cache:build_sum_ms", 0) or 0)
        out["metrics:snapshot_cache:build_avg_ms"] = int(bsum / bc) if bc > 0 else 0
    except Exception:
        out["metrics:snapshot_cache:build_avg_ms"] = 0

    try:
        status_requests = int(out.get("metrics:status:requests", 0) or 0)
        status_db = int(out.get("metrics:status:rev_db", 0) or 0)
        out["metrics:status:db_queries_total"] = status_db
        out["metrics:status:db_query_ratio"] = round((status_db / status_requests), 4) if status_requests > 0 else 0.0
    except Exception:
        out["metrics:status:db_queries_total"] = 0
        out["metrics:status:db_query_ratio"] = 0.0

    try:
        steady_hit = int(out.get("metrics:snapshot_cache:steady_hit", 0) or 0)
        steady_miss = int(out.get("metrics:snapshot_cache:steady_miss", 0) or 0)
        steady_total = steady_hit + steady_miss
        out["metrics:snapshot_cache:steady_hit_ratio"] = round((steady_hit / steady_total), 4) if steady_total > 0 else 0.0
    except Exception:
        out["metrics:snapshot_cache:steady_hit_ratio"] = 0.0

    try:
        school_hit = int(out.get("metrics:snapshot_cache:school_hit", 0) or 0)
        school_miss = int(out.get("metrics:snapshot_cache:school_miss", 0) or 0)
        school_total = school_hit + school_miss
        out["metrics:snapshot_cache:school_hit_ratio"] = round((school_hit / school_total), 4) if school_total > 0 else 0.0
    except Exception:
        out["metrics:snapshot_cache:school_hit_ratio"] = 0.0

    try:
        queue_enqueued = int(out.get("metrics:snapshot_queue:enqueued", 0) or 0)
        queue_dequeued = int(out.get("metrics:snapshot_queue:dequeued", 0) or 0)
        queue_materialized = int(out.get("metrics:snapshot_queue:materialized", 0) or 0)
        out["metrics:snapshot_queue:enqueue_dequeue_gap"] = max(0, queue_enqueued - queue_dequeued)
        out["metrics:snapshot_queue:dequeue_materialize_gap"] = max(0, queue_dequeued - queue_materialized)
    except Exception:
        out["metrics:snapshot_queue:enqueue_dequeue_gap"] = 0
        out["metrics:snapshot_queue:dequeue_materialize_gap"] = 0

    try:
        queue_dequeued = int(out.get("metrics:snapshot_queue:dequeued", 0) or 0)
        queue_materialized = int(out.get("metrics:snapshot_queue:materialized", 0) or 0)

        queue_wait_sum_ms = int(out.get("metrics:snapshot_queue:queue_wait_sum_ms", 0) or 0)
        process_sum_ms = int(out.get("metrics:snapshot_queue:process_sum_ms", 0) or 0)
        e2e_sum_ms = int(out.get("metrics:snapshot_queue:e2e_sum_ms", 0) or 0)

        out["metrics:snapshot_queue:queue_wait_avg_ms"] = int(queue_wait_sum_ms / queue_dequeued) if queue_dequeued > 0 else 0
        out["metrics:snapshot_queue:process_avg_ms"] = int(process_sum_ms / queue_materialized) if queue_materialized > 0 else 0
        out["metrics:snapshot_queue:e2e_avg_ms"] = int(e2e_sum_ms / queue_dequeued) if queue_dequeued > 0 else 0
    except Exception:
        out["metrics:snapshot_queue:queue_wait_avg_ms"] = 0
        out["metrics:snapshot_queue:process_avg_ms"] = 0
        out["metrics:snapshot_queue:e2e_avg_ms"] = 0

    try:
        out["snapshot_observability"] = _snapshot_metrics_payload()
    except Exception:
        out["snapshot_observability"] = {}

    return JsonResponse(out, json_dumps_params={"ensure_ascii": False})


@require_http_methods(["GET"])
def ws_metrics(request):
    """
    GET /api/display/ws-metrics/
    
    Returns WebSocket metrics for monitoring/dashboards.
    
    Response:
    {
        "connections_active": 1234,
        "connections_total": 5678,
        "connections_failed": 12,
        "broadcasts_sent": 8901,
        "broadcasts_failed": 3,
        "broadcast_latency_avg_ms": 0.5,
        "health": "ok|warning|critical"
    }
    """
    is_debug = bool(getattr(dj_settings, "DEBUG", False))
    if not is_debug:
        required_key = (
            os.getenv("DISPLAY_WS_METRICS_KEY", "").strip()
            or os.getenv("DISPLAY_METRICS_KEY", "").strip()
        )
        if not required_key:
            return JsonResponse({"detail": "not_found"}, status=404)
        provided = (request.headers.get("X-Display-Metrics-Key") or "").strip()
        if not secrets.compare_digest(provided, required_key):
            return JsonResponse({"detail": "forbidden"}, status=403)

    try:
        from display.ws_metrics import ws_metrics as metrics_tracker
        from display.ws_cluster_metrics import snapshot as ws_cluster_snapshot

        metrics = metrics_tracker.get_snapshot()
        cluster = ws_cluster_snapshot()

        def _cache_int(key: str) -> int:
            try:
                v = cache.get(key)
                return int(v) if v is not None else 0
            except Exception:
                return 0

        # Calculate derived metrics. Instance metrics can be zero on the worker
        # that serves this request even while other ASGI workers have live
        # display sockets, so health decisions use the cluster count when it is
        # available.
        active = metrics.get("connections_active", 0)
        failed = metrics.get("connections_failed", 0)
        total = max(1, metrics.get("connections_total", 1))  # Avoid division by zero
        active_cluster = int(cluster.get("active_ws", 0) or 0)
        active_effective = max(int(active or 0), active_cluster)

        broadcasts_sent = metrics.get("broadcasts_sent", 0)
        broadcasts_failed = metrics.get("broadcasts_failed", 0)
        disconnect_codes = metrics.get("disconnect_codes", {}) or {}

        shared_total = _cache_int("metrics:ws:connect_total")
        shared_failed = _cache_int("metrics:ws:connect_failed")
        shared_disconnect_total = _cache_int("metrics:ws:disconnect_total")
        shared_reconnect_total = _cache_int("metrics:ws:reconnect_total")
        shared_broadcasts_sent = _cache_int("metrics:ws:broadcast_sent")
        shared_broadcasts_failed = _cache_int("metrics:ws:broadcast_failed")
        shared_snapshot_refresh = _cache_int("metrics:ws:snapshot_refresh_total")
        shared_patch_total = _cache_int("metrics:ws:patch_total")
        shared_server_ping_sent = _cache_int("metrics:ws:server_ping_sent")
        shared_server_ping_send_failed = _cache_int("metrics:ws:server_ping_send_failed")
        for code in (1000, 1001, 1006, 1011, 1012, 1013, 4400, 4403, 4408, 4500, 4501):
            code_key = str(code)
            shared_code_total = _cache_int(f"metrics:ws:disconnect_code:{code_key}")
            if shared_code_total:
                disconnect_codes[code_key] = max(int(disconnect_codes.get(code_key, 0) or 0), shared_code_total)

        cluster_disconnect_codes = cluster.get("disconnect_codes", {}) or {}
        for code_key, total_value in cluster_disconnect_codes.items():
            try:
                disconnect_codes[str(code_key)] = max(int(disconnect_codes.get(str(code_key), 0) or 0), int(total_value or 0))
            except Exception:
                continue

        avg_latency = 0.0
        if metrics.get("broadcast_latency_count", 0) > 0:
            avg_latency = (
                metrics.get("broadcast_latency_sum", 0) / 
                metrics["broadcast_latency_count"]
            )
        
        # Health status
        health = "ok"
        if active_effective == 0 and total > 10:
            health = "warning"  # No connections but had connections before
        elif failed / total > 0.1:  # > 10% connection failure rate
            health = "critical"
        elif broadcasts_failed / max(1, broadcasts_sent + broadcasts_failed) > 0.05:  # > 5% broadcast failures
            health = "warning"
        elif avg_latency > 100:  # Broadcast latency > 100ms
            health = "warning"
        
        return JsonResponse({
            "connections_active": active,
            "connections_total": total,
            "connections_failed": failed,
            "connections_total_shared": shared_total,
            "connections_failed_shared": shared_failed,
            "disconnect_total_shared": shared_disconnect_total,
            "active_ws_cluster": active_cluster,
            "cluster_rates_60s": cluster.get("rates_60s", {}),
            "cluster_rates_300s": cluster.get("rates_300s", {}),
            "cluster_totals_retained": cluster.get("totals_retained", {}),
            "cluster_metrics_enabled": bool(cluster.get("enabled")),
            "reconnects_total": metrics.get("reconnects_total", 0),
            "reconnects_total_shared": shared_reconnect_total,
            "disconnect_codes": disconnect_codes,
            "broadcasts_sent": broadcasts_sent,
            "broadcasts_failed": broadcasts_failed,
            "broadcasts_sent_shared": shared_broadcasts_sent,
            "broadcasts_failed_shared": shared_broadcasts_failed,
            "snapshot_refresh_sent_shared": shared_snapshot_refresh,
            "patch_sent_shared": shared_patch_total,
            "server_ping_sent_shared": shared_server_ping_sent,
            "server_ping_send_failed_shared": shared_server_ping_send_failed,
            "broadcast_latency_avg_ms": round(avg_latency, 2),
            "scope": {
                "connections_active": "instance",
                "connections_total_shared": "cluster_best_effort",
            },
            "health": health,
        })
    except ImportError:
        # ws_metrics not available (channels not installed/configured)
        return JsonResponse({
            "error": "WebSocket metrics not available",
            "detail": "Channels not configured or DISPLAY_WS_ENABLED=false"
        }, status=503)
    except Exception as e:
        logger.exception(f"ws_metrics error: {e}")
        return JsonResponse({"error": "Internal error"}, status=500)


def _metrics_add(key: str, delta: int) -> None:
    _obs_metric_add(key, int(delta))


def _metrics_set_max(key: str, value: int) -> None:
    _obs_metric_set_max(key, int(value))

def _metrics_log_maybe() -> None:
    interval = _metrics_interval_seconds()
    throttle_key = f"metrics:snapshot_cache:log:{interval}"
    try:
        should_log = bool(cache.add(throttle_key, "1", timeout=interval))
    except Exception:
        should_log = False

    if not should_log:
        return

    keys = [
        "metrics:snapshot_cache:token_hit",
        "metrics:snapshot_cache:token_miss",
        "metrics:snapshot_cache:school_hit",
        "metrics:snapshot_cache:school_miss",
        "metrics:snapshot_cache:steady_hit",
        "metrics:snapshot_cache:steady_miss",
        "metrics:snapshot_cache:hit",
        "metrics:snapshot_cache:miss",
        "metrics:snapshot_cache:stale_fallback",
        "metrics:snapshot_cache:revision_reject",
        "metrics:snapshot_cache:wake_boundary_reject",
        "metrics:snapshot_cache:build_lock_contention",
        "metrics:snapshot_cache:build_count",
        "metrics:snapshot_cache:build_sum_ms",
        "metrics:snapshot_cache:build_max_ms",
        "metrics:snapshot_build:count",
        "metrics:snapshot_build:soft_timeout",
        "metrics:snapshot_build:duration_ms:sum",
        "metrics:snapshot_build:duration_ms:max",
        "metrics:snapshot_build:source:inline:count",
        "metrics:snapshot_build:source:queue:count",
        "metrics:snapshot_build:source:stale:count",
        "metrics:snapshot_queue:enqueued",
        "metrics:snapshot_queue:enqueue_count",
        "metrics:snapshot_queue:skipped_enqueue",
        "metrics:snapshot_queue:deduplicated_jobs",
        "metrics:snapshot_queue:deduped",
        "metrics:snapshot_queue:coalesced",
        "metrics:snapshot_queue:dequeued",
        "metrics:snapshot_queue:materialized",
        "metrics:snapshot_queue:error",
        "metrics:snapshot_queue:debounced",
        "metrics:snapshot_queue:latest_revision_replaced",
        "metrics:snapshot_queue:queue_skipped_outdated",
        "metrics:snapshot_queue:outdated_job_dropped",
        "metrics:snapshot_queue:worker_unavailable",
        "metrics:snapshot_queue:queue_wait_sum_ms",
        "metrics:snapshot_queue:queue_wait_max_ms",
        "metrics:snapshot_queue:queue_wait_time_ms:sum",
        "metrics:snapshot_queue:queue_wait_time_ms:max",
        "metrics:snapshot_queue:queue_wait_time_ms:count",
        "metrics:snapshot_queue:process_sum_ms",
        "metrics:snapshot_queue:process_max_ms",
        "metrics:snapshot_queue:e2e_sum_ms",
        "metrics:snapshot_queue:e2e_max_ms",
    ]
    try:
        vals = {k: (cache.get(k) or 0) for k in keys}
    except Exception:
        vals = {k: 0 for k in keys}

    build_count = int(vals.get("metrics:snapshot_cache:build_count", 0) or 0)
    build_sum_ms = int(vals.get("metrics:snapshot_cache:build_sum_ms", 0) or 0)
    build_max_ms = int(vals.get("metrics:snapshot_cache:build_max_ms", 0) or 0)
    build_avg_ms = int(build_sum_ms / build_count) if build_count > 0 else 0
    stale_fallback = int(vals.get("metrics:snapshot_cache:stale_fallback", 0) or 0)
    revision_reject = int(vals.get("metrics:snapshot_cache:revision_reject", 0) or 0)
    wake_boundary_reject = int(vals.get("metrics:snapshot_cache:wake_boundary_reject", 0) or 0)
    build_lock_contention = int(vals.get("metrics:snapshot_cache:build_lock_contention", 0) or 0)
    queue_enqueued = int(vals.get("metrics:snapshot_queue:enqueued", 0) or 0)
    queue_deduped = int(vals.get("metrics:snapshot_queue:deduped", 0) or 0)
    queue_coalesced = int(vals.get("metrics:snapshot_queue:coalesced", 0) or 0)
    queue_dequeued = int(vals.get("metrics:snapshot_queue:dequeued", 0) or 0)
    queue_materialized = int(vals.get("metrics:snapshot_queue:materialized", 0) or 0)
    queue_errors = int(vals.get("metrics:snapshot_queue:error", 0) or 0)
    queue_debounced = int(vals.get("metrics:snapshot_queue:debounced", 0) or 0)
    queue_latest_revision_replaced = int(vals.get("metrics:snapshot_queue:latest_revision_replaced", 0) or 0)
    queue_skipped_outdated = int(vals.get("metrics:snapshot_queue:queue_skipped_outdated", 0) or 0)
    queue_outdated_job_dropped = int(vals.get("metrics:snapshot_queue:outdated_job_dropped", 0) or 0)
    queue_worker_unavailable = int(vals.get("metrics:snapshot_queue:worker_unavailable", 0) or 0)
    queue_wait_sum_ms = int(vals.get("metrics:snapshot_queue:queue_wait_sum_ms", 0) or 0)
    queue_wait_max_ms = int(vals.get("metrics:snapshot_queue:queue_wait_max_ms", 0) or 0)
    queue_wait_avg_ms = int(queue_wait_sum_ms / queue_dequeued) if queue_dequeued > 0 else 0
    queue_wait_obs_sum_ms = int(vals.get("metrics:snapshot_queue:queue_wait_time_ms:sum", 0) or 0)
    queue_wait_obs_count = int(vals.get("metrics:snapshot_queue:queue_wait_time_ms:count", 0) or 0)
    queue_wait_obs_avg_ms = int(queue_wait_obs_sum_ms / queue_wait_obs_count) if queue_wait_obs_count > 0 else 0
    queue_process_sum_ms = int(vals.get("metrics:snapshot_queue:process_sum_ms", 0) or 0)
    queue_process_max_ms = int(vals.get("metrics:snapshot_queue:process_max_ms", 0) or 0)
    queue_process_avg_ms = int(queue_process_sum_ms / queue_materialized) if queue_materialized > 0 else 0
    queue_e2e_sum_ms = int(vals.get("metrics:snapshot_queue:e2e_sum_ms", 0) or 0)
    queue_e2e_max_ms = int(vals.get("metrics:snapshot_queue:e2e_max_ms", 0) or 0)
    queue_e2e_avg_ms = int(queue_e2e_sum_ms / queue_dequeued) if queue_dequeued > 0 else 0

    _obs_log_event(
        logger,
        "snapshot_metrics",
        cache_hit=vals.get("metrics:snapshot_cache:hit", 0),
        cache_miss=vals.get("metrics:snapshot_cache:miss", 0),
        stale_fallback=stale_fallback,
        revision_reject=revision_reject,
        wake_boundary_reject=wake_boundary_reject,
        build_lock_contention=build_lock_contention,
        build_count=build_count,
        build_avg_ms=build_avg_ms,
        build_max_ms=build_max_ms,
        build_soft_timeout=vals.get("metrics:snapshot_build:soft_timeout", 0),
        build_inline_count=vals.get("metrics:snapshot_build:source:inline:count", 0),
        build_queue_count=vals.get("metrics:snapshot_build:source:queue:count", 0),
        build_stale_count=vals.get("metrics:snapshot_build:source:stale:count", 0),
        queue_enqueue_count=vals.get("metrics:snapshot_queue:enqueue_count", 0),
        queue_skipped_enqueue=vals.get("metrics:snapshot_queue:skipped_enqueue", 0),
        queue_deduplicated_jobs=vals.get("metrics:snapshot_queue:deduplicated_jobs", 0),
        queue_coalesced=queue_coalesced,
        queue_dequeued=queue_dequeued,
        queue_materialized=queue_materialized,
        queue_errors=queue_errors,
        queue_wait_avg_ms=queue_wait_avg_ms,
        queue_wait_obs_avg_ms=queue_wait_obs_avg_ms,
        queue_process_avg_ms=queue_process_avg_ms,
        queue_e2e_avg_ms=queue_e2e_avg_ms,
    )


def _snapshot_ttl_seconds() -> int:
    try:
        v = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_TTL", 10) or 10)
    except Exception:
        v = 10
    return max(1, min(60, v))


def _snapshot_build_lock_ttl_seconds() -> int:
    # Short lock to coalesce concurrent requests.
    return 8


def _snapshot_bind_ttl_seconds() -> int:
    # How long a token stays bound to the first seen device_key.
    return 60 * 60 * 24 * 30  # 30 days


def _stable_json_bytes(payload: dict) -> bytes:
    # Stable encoding for ETag hashing (order-independent).
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _etag_from_json_bytes(json_bytes: bytes) -> str:
    return hashlib.sha256(json_bytes).hexdigest()


def _snapshot_cache_day_key(day_key: object | None = None) -> str:
    if hasattr(day_key, "isoformat"):
        try:
            return str(day_key.isoformat())[:32]
        except Exception:
            pass

    raw = str(day_key or "").strip()
    if not raw:
        try:
            raw = timezone.localdate().isoformat()
        except Exception:
            raw = date.today().isoformat()
    if re.fullmatch(r"\d{8}", raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw[:32]


def _normalize_snapshot_theme_fields(snap: dict) -> bool:
    if not isinstance(snap, dict):
        return False
    settings_payload = snap.get("settings")
    if not isinstance(settings_payload, dict):
        return False

    changed = False
    old_theme = settings_payload.get("theme")
    theme = _normalize_theme_value(old_theme)
    if old_theme != theme:
        settings_payload["theme"] = theme
        changed = True

    old_accent = settings_payload.get("display_accent_color")
    accent = _normalize_display_accent(old_accent, theme)
    if old_accent != accent:
        settings_payload["display_accent_color"] = accent
        changed = True

    return changed


def _snapshot_cache_entry(payload: dict) -> dict:
    _normalize_snapshot_theme_fields(payload)
    json_bytes = _stable_json_bytes(payload)
    return {
        "snap": payload,
        "etag": _etag_from_json_bytes(json_bytes),
        "body": json_bytes,
        "size": len(json_bytes),
    }


def _snapshot_cache_entry_from_cached(cached: object) -> dict | None:
    if isinstance(cached, dict):
        snap = cached.get("snap")
        if isinstance(snap, dict):
            normalized = _normalize_snapshot_theme_fields(snap)
            body = cached.get("body")
            if isinstance(body, str):
                body = body.encode("utf-8")
            elif not isinstance(body, (bytes, bytearray)):
                body = _stable_json_bytes(snap)
            if normalized:
                body = _stable_json_bytes(snap)

            etag = cached.get("etag")
            if normalized or not isinstance(etag, str) or not etag:
                etag = _etag_from_json_bytes(bytes(body))

            return {
                "snap": snap,
                "etag": etag,
                "body": bytes(body),
                "size": int(cached.get("size") or len(bytes(body))),
            }

        # Backward compatibility: cache may still contain plain snapshot dicts.
        return _snapshot_cache_entry(cached)

    if isinstance(cached, (bytes, bytearray)):
        try:
            snap = json.loads(bytes(cached).decode("utf-8"))
        except Exception:
            return None
        if isinstance(snap, dict):
            normalized = _normalize_snapshot_theme_fields(snap)
            body = _stable_json_bytes(snap) if normalized else bytes(cached)
            return {
                "snap": snap,
                "etag": _etag_from_json_bytes(body),
                "body": body,
                "size": len(body),
            }
        return None

    if isinstance(cached, str):
        try:
            snap = json.loads(cached)
        except Exception:
            return None
        if isinstance(snap, dict):
            normalized = _normalize_snapshot_theme_fields(snap)
            body = _stable_json_bytes(snap) if normalized else cached.encode("utf-8")
            return {
                "snap": snap,
                "etag": _etag_from_json_bytes(body),
                "body": body,
                "size": len(body),
            }

    return None


def _snapshot_entry_body_bytes(entry: dict) -> bytes:
    body = entry.get("body")
    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    snap = entry.get("snap")
    if isinstance(snap, dict):
        return _stable_json_bytes(snap)
    return b"{}"


def _snapshot_entry_schedule_revision(entry: dict | None) -> int | None:
    if not isinstance(entry, dict):
        return None
    snap = entry.get("snap")
    if not isinstance(snap, dict):
        return None
    meta = snap.get("meta")
    if not isinstance(meta, dict):
        return None
    try:
        rev = int(meta.get("schedule_revision"))
    except Exception:
        return None
    return rev if rev >= 0 else None


def _snapshot_entry_past_wake_boundary(entry: dict | None) -> bool:
    if not isinstance(entry, dict):
        return False
    snap = entry.get("snap")
    if not isinstance(snap, dict):
        return False
    state = snap.get("state")
    meta = snap.get("meta")
    if not isinstance(state, dict) or not isinstance(meta, dict):
        return False
    if str(state.get("reason") or "").strip().lower() != "before_hours":
        return False
    next_wake_at = str(meta.get("next_wake_at") or "").strip()
    if not next_wake_at:
        return False
    try:
        return timezone.now() >= datetime.fromisoformat(next_wake_at)
    except Exception:
        return False


def _validated_snapshot_cache_entry_from_value(
    cached: object,
    *,
    min_rev: int | None = None,
    cache_key: str | None = None,
    reject_past_wake_boundary: bool = True,
) -> tuple[dict | None, str]:
    entry = _snapshot_cache_entry_from_cached(cached)
    if not isinstance(entry, dict) or not isinstance(entry.get("snap"), dict):
        return None, "missing"

    if reject_past_wake_boundary and _snapshot_entry_past_wake_boundary(entry):
        if cache_key:
            try:
                cache.delete(cache_key)
            except Exception:
                pass
        _metrics_incr("metrics:snapshot_cache:wake_boundary_reject")
        _obs_snapshot_cache(
            logger=logger,
            outcome="miss",
            layer="steady",
            reason="past_wake_boundary",
            cache_key=cache_key,
        )
        return None, "past_wake_boundary"

    try:
        min_rev_i = int(min_rev) if min_rev is not None else None
    except Exception:
        min_rev_i = None
    if min_rev_i is not None and min_rev_i > 0:
        entry_rev = _snapshot_entry_schedule_revision(entry)
        if entry_rev is not None and entry_rev < min_rev_i:
            if cache_key:
                try:
                    cache.delete(cache_key)
                except Exception:
                    pass
            _metrics_incr("metrics:snapshot_cache:revision_reject")
            _obs_snapshot_cache(
                logger=logger,
                outcome="miss",
                layer="steady",
                rev=entry_rev,
                reason="older_revision",
                cache_key=cache_key,
            )
            return None, "older_revision"

    return entry, "hit"


def _wait_for_valid_snapshot_entry(
    cache_key: str,
    *,
    min_rev: int | None = None,
    timeout_s: float = 0.25,
    step_s: float = 0.05,
) -> tuple[dict | None, str]:
    timeout_s = max(0.0, float(timeout_s))
    step_s = max(0.01, float(step_s))
    deadline = time.monotonic() + timeout_s
    last_reason = "missing"
    while time.monotonic() < deadline:
        try:
            cached = cache.get(cache_key)
        except Exception:
            cached = None
        entry, reason = _validated_snapshot_cache_entry_from_value(
            cached,
            min_rev=min_rev,
            cache_key=cache_key,
        )
        if isinstance(entry, dict):
            return entry, "hit"
        last_reason = reason
        time.sleep(step_s)
    return None, last_reason


def _parse_if_none_match(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    # We only support a single strong ETag value.
    if s.startswith("W/"):
        s = s[2:].strip()
    if s.startswith('"') and s.endswith('"') and len(s) > 1:
        s = s[1:-1]
    if not s:
        return None
    return s


def _snapshot_anti_loop_check(token_hash: str) -> bool:
    """
    Checks if a token is requesting too frequently (looping).
    Returns True if safe, False if looping (should receive cool-down).
    Limit: 120 requests per 60-second window.
    A single screen legitimately uses ~12 requests per period transition
    (20 s window at 2 s intervals) plus normal polling. With back-to-back
    transitions and page reloads this can reach 50-60 req/min easily.
    """
    key = f"loop:{token_hash}"
    try:
        # 60 seconds rolling window (approx)
        added = cache.add(key, 1, timeout=60)
        if added:
            val = 1
        else:
            val = cache.incr(key)
        
        if val > 120: 
            return False
        return True
    except Exception:
        return True


def _snapshot_rate_limit_allow(token_hash: str, device_hash: str) -> bool:
    """Token bucket: 1 req/sec with burst 3, keyed by token_hash + device_hash."""

    capacity = 3.0
    refill_per_sec = 1.0
    state_key = f"rl:snapshot:{token_hash}:{device_hash}"
    lock_key = f"{state_key}:lock"

    now = time.monotonic()
    state = None

    have_lock = False
    try:
        have_lock = bool(cache.add(lock_key, "1", timeout=1))
    except Exception:
        have_lock = False

    if not have_lock:
        # Best effort: if we can't lock, avoid hard-failing the request.
        return True

    try:
        state = cache.get(state_key)
        if not isinstance(state, dict):
            state = {"tokens": capacity, "ts": now}

        tokens = float(state.get("tokens", capacity))
        last_ts = float(state.get("ts", now))

        elapsed = max(0.0, now - last_ts)
        tokens = min(capacity, tokens + elapsed * refill_per_sec)

        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0

        state = {"tokens": tokens, "ts": now}
        try:
            cache.set(state_key, state, timeout=60)
        except Exception:
            pass

        return allowed
    finally:
        try:
            cache.delete(lock_key)
        except Exception:
            pass


def _app_revision() -> str:
    try:
        v = str(getattr(dj_settings, "APP_REVISION", "") or "").strip()
    except Exception:
        v = ""
    return v


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        for k in ("items", "results", "data", "rows", "list"):
            v = value.get(k)
            if isinstance(v, list):
                return v
    return []


def _normalize_snapshot_keys(snap: dict) -> dict:
    """
    مفاتيح ثابتة للواجهة:
      - announcements
      - excellence
      - standby
      - period_classes
      - day_path
    """
    if not isinstance(snap, dict):
        return {
            "settings": {},
            "state": {},
            "day_path": [],
            "current_period": None,
            "next_period": None,
            "period_classes": [],
            "period_classes_map": {},
            "standby": [],
            "excellence": [],
            "announcements": [],
        }

    for container_key in ("data", "payload", "result", "snapshot"):
        c = snap.get(container_key)
        if isinstance(c, dict):
            for k, v in c.items():
                snap.setdefault(k, v)

    def fill(dst_key: str, source_keys):
        cur = _as_list(snap.get(dst_key))
        if cur:
            snap[dst_key] = cur
            return
        for k in source_keys:
            arr = _as_list(snap.get(k))
            if arr:
                snap[dst_key] = arr
                return
        snap[dst_key] = []

    fill(
        "excellence",
        ["honor_board", "excellence_board", "honors", "awards", "excellent", "excellent_students", "honor_items"],
    )
    fill(
        "standby",
        ["waiting", "standby_periods", "standby_items", "standby_list", "standbyClasses", "standby_classes"],
    )
    fill(
        "announcements",
        ["alerts", "notices", "messages", "announcement_list", "announcements_list"],
    )

    snap["day_path"] = _as_list(snap.get("day_path"))
    snap["period_classes"] = _as_list(snap.get("period_classes"))
    pcm = snap.get("period_classes_map")
    if isinstance(pcm, dict):
        norm_map = {}
        for k, v in pcm.items():
            try:
                kk = str(int(k))
            except Exception:
                continue
            norm_map[kk] = _as_list(v)
        snap["period_classes_map"] = norm_map
    else:
        snap["period_classes_map"] = {}
    return snap


def _fallback_payload(message: str = "إعدادات المدرسة غير مهيأة") -> dict:
    now = timezone.localtime()
    return {
        "now": now.isoformat(),
        "meta": {"weekday": now.isoweekday(), "schedule_revision": 0},
        "settings": {
            "name": "",
            "logo_url": None,
            "theme": "indigo",
            "refresh_interval_sec": 10,
            "standby_scroll_speed": 0.8,
            "periods_scroll_speed": 0.5,
            "bell_sound": "bell",
            "bell_sound_url": _static_url("sounds/bell.mp3"),
        },
        "state": {
            "type": "config",
            "label": message,
            "from": None,
            "to": None,
            "remaining_seconds": 0,
        },
        "day_path": [],
        "current_period": None,
        "next_period": None,
        "period_classes": [],
        "period_classes_map": {},
        "standby": [],
        "excellence": [],
        "announcements": [],
    }


def _fallback_building_payload(
    *,
    school_id: int,
    rev: int,
    day_key: str,
    reason: str = "building",
    refresh_interval_sec: int = 3,
) -> dict:
    """DB-free safe snapshot payload to avoid black screens during rebuilds."""

    payload = _fallback_payload("جاري تجهيز الجدول...")
    try:
        payload["settings"]["refresh_interval_sec"] = int(refresh_interval_sec)
    except Exception:
        pass
    try:
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            payload["meta"] = meta
        meta["school_id"] = int(school_id)
        meta["schedule_revision"] = int(rev)
        meta["day_key"] = str(day_key)
        meta["cache"] = "FALLBACK"
        meta["reason"] = str(reason)
        meta["generated_at"] = timezone.now().isoformat()
    except Exception:
        pass
    try:
        st = payload.get("state")
        if isinstance(st, dict):
            st["type"] = "BUILDING"
            st["label"] = "جاري تجهيز الجدول..."
    except Exception:
        pass
    return payload


def _extract_token(request, token_from_path: str | None) -> str | None:
    t = (token_from_path or "").strip()

    # Fallback: some callers may embed token in the URL path while not passing it
    # via query/header (or if the URL pattern changes). Parse it from the path.
    if not t:
        try:
            p = (getattr(request, "path_info", "") or getattr(request, "path", "") or "").strip()
            m = re.search(r"/api/display/(?:snapshot|today|live|status|goodbye)/([^/]+)/?$", p, flags=re.IGNORECASE)
            if m and m.group(1):
                t = (m.group(1) or "").strip()
        except Exception:
            pass

    if not t:
        t = (request.headers.get("X-Display-Token") or "").strip()
    if not t:
        t = (request.GET.get("token") or "").strip()
    if not t or len(t) < 8 or len(t) > 256:
        return None
    return t


@require_http_methods(["GET", "HEAD"])
def status(request, token: str | None = None):
    """Lightweight polling endpoint.

    GET /api/display/status/?token=...   (or X-Display-Token header)
    GET /api/display/status/<token>/

        Behavior (authoritative numeric revision mode):
        - When client sends `v=<schedule_revision>`:
                - return 304 ONLY if v == current schedule_revision
                - else return 200 with {fetch_required: true, schedule_revision: current_rev}
            This path intentionally ignores If-None-Match/ETag for status.

        Backward compatibility:
        - If `v` is missing, we keep legacy behavior (ETag-based) for older clients.

    This endpoint never builds snapshots and should stay cheap.
    """
    # --- Optional lightweight metrics (sampled, cache-only) ---
    def _metrics_enabled() -> bool:
        try:
            return bool(getattr(dj_settings, "DISPLAY_STATUS_METRICS_ENABLED", False))
        except Exception:
            return False

    def _metrics_sample_every() -> int:
        try:
            return int(getattr(dj_settings, "DISPLAY_STATUS_METRICS_SAMPLE_EVERY", 50) or 50)
        except Exception:
            return 50

    def _metrics_ttl() -> int:
        try:
            return int(getattr(dj_settings, "DISPLAY_STATUS_METRICS_KEY_TTL", 86400) or 86400)
        except Exception:
            return 86400

    def _invalid_token_signature() -> str:
        # Safe, non-PII signature used only for deterministic sampling.
        # Prefer the provided token (even if invalid length), else mix in path + UA.
        try:
            p = (token or "").strip()
        except Exception:
            p = ""
        try:
            if not p:
                p = (request.GET.get("token") or "").strip()
        except Exception:
            pass
        try:
            if not p:
                p = (request.headers.get("X-Display-Token") or "").strip()
        except Exception:
            pass
        try:
            path = (getattr(request, "path_info", "") or getattr(request, "path", "") or "").strip()
        except Exception:
            path = ""
        try:
            ua = (request.headers.get("User-Agent") or "").strip()
        except Exception:
            ua = ""
        return f"{p}|{path}|{ua}"

    token_value = _extract_token(request, token)
    if not token_value:
        # Sampled metrics for invalid_token to avoid becoming a Redis surface for spam.
        try:
            if _metrics_enabled():
                sig_hash = _sha256(_invalid_token_signature())
                if status_metrics_should_sample(token_hash=sig_hash, sample_every=_metrics_sample_every()):
                    day_key = status_metrics_day_key()
                    ttl = _metrics_ttl()
                    status_metrics_bump(day_key=str(day_key), name="invalid_token", ttl_sec=int(ttl))
        except Exception:
            pass

        try:
            p = (getattr(request, "path_info", "") or getattr(request, "path", "") or "").strip()
            logger.warning("status: invalid_token path=%s", p)
        except Exception:
            pass
        return JsonResponse({"detail": "access_denied", "message": "\u062a\u0639\u0630\u0631 \u0627\u0644\u0648\u0635\u0648\u0644"}, status=403)

    _metrics_incr("metrics:status:requests")
    if (request.headers.get("X-Display-Fallback") or "").strip().lower() in {"1", "true", "yes"}:
        _metrics_incr("metrics:status:fallback_poll")

    token_hash = _sha256(token_value)
    school_id = None

    device_key = (request.headers.get("X-Display-Device") or "").strip()
    if not device_key:
        device_key = (request.GET.get("dk") or request.GET.get("device_key") or "").strip()

    # A manager previewing the screen from the dashboard must not claim the
    # device, and must not be counted as the screen being live.
    from display.services import resolve_preview_screen

    preview_screen = resolve_preview_screen(request, token_value)
    if preview_screen is not None:
        device_key = ""
        school_id = int(getattr(preview_screen, "school_id", 0) or 0) or None

    if device_key:
        try:
            from display.services import (
                ScreenBoundError,
                ScreenNotFoundError,
                bind_device_atomic,
            )

            bound_screen = bind_device_atomic(token=token_value, device_id=device_key)
            touch_display_presence(bound_screen.pk, token=token_value)
            school_id = int(getattr(bound_screen, "school_id", 0) or 0) or None
            if school_id:
                try:
                    cache.set(
                        f"display:token_map:{token_hash}",
                        {
                            "id": int(bound_screen.pk),
                            "school_id": int(school_id),
                            "bound_device_id": device_key,
                        },
                        timeout=60 * 60,
                    )
                except Exception:
                    pass
        except ScreenNotFoundError:
            resp = JsonResponse(
                {
                    "detail": "access_denied",
                    "message": "\u062a\u0639\u0630\u0631 \u0627\u0644\u0648\u0635\u0648\u0644",
                },
                status=403,
            )
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
            resp["Vary"] = "Accept-Encoding"
            try:
                resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
            except Exception:
                pass
            return resp
        except ScreenBoundError as e:
            logger.warning(
                "device_binding_reject token_hash=%s device=%s reason=%s",
                token_hash[:12],
                device_key[:8],
                str(e),
            )
            resp = JsonResponse(
                {
                    "detail": "screen_bound",
                    "message": "\u0647\u0630\u0647 \u0627\u0644\u0634\u0627\u0634\u0629 \u0645\u0631\u062a\u0628\u0637\u0629 \u0628\u062c\u0647\u0627\u0632 \u0622\u062e\u0631",
                },
                status=403,
            )
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
            resp["Vary"] = "Accept-Encoding"
            try:
                resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
            except Exception:
                pass
            return resp
        except Exception:
            logger.exception("status: device binding lookup failed token_hash=%s", token_hash[:12])
            resp = JsonResponse({"detail": "internal_error"}, status=500)
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
            resp["Vary"] = "Accept-Encoding"
            try:
                resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
            except Exception:
                pass
            return resp

    # Token-scoped manual refresh: if set, force fetch_required regardless of revision.
    # Used by dashboard "refresh single screen".
    force_key = f"display:force_refresh:{token_hash}"
    force_refresh = False
    try:
        force_refresh = bool(cache.get(force_key))
        if force_refresh:
            try:
                cache.delete(force_key)
            except Exception:
                pass
    except Exception:
        force_refresh = False

    # Token-scoped manual reload: force a full page reload on the client.
    reload_key = f"display:force_reload:{token_hash}"
    force_reload = False
    try:
        force_reload = bool(cache.get(reload_key))
        if force_reload:
            try:
                cache.delete(reload_key)
            except Exception:
                pass
    except Exception:
        force_reload = False

    # --- Optional lightweight metrics (sampled, cache-only) ---
    metrics_day_key = None
    metrics_ttl = None
    if _metrics_enabled():
        metrics_ttl = _metrics_ttl()
        if status_metrics_should_sample(token_hash=token_hash, sample_every=_metrics_sample_every()):
            metrics_day_key = status_metrics_day_key()

    def _bump_metric(name: str) -> None:
        if not metrics_day_key or not metrics_ttl:
            return
        status_metrics_bump(day_key=str(metrics_day_key), name=str(name), ttl_sec=int(metrics_ttl))

    # --- Numeric revision mode (source of truth) ---
    client_v_raw = (request.GET.get("v") or request.GET.get("rev") or "").strip()
    client_v = None
    try:
        if client_v_raw != "":
            client_v = int(client_v_raw)
    except Exception:
        client_v = None

    # Resolve school_id cheaply (prefer cache map)
    try:
        if not school_id:
            _metrics_incr("metrics:status:cache_get")
            cached_map = cache.get(f"display:token_map:{token_hash}")
            if isinstance(cached_map, dict):
                school_id = cached_map.get("school_id")
            else:
                try:
                    school_id = int(cached_map) if cached_map else None
                except Exception:
                    school_id = None
    except Exception:
        school_id = None

    # If cache is not shared (LocMem / missing Redis), fall back to DB so different
    # workers/processes don't disagree and leave some devices stuck.
    if (not school_id) and (not _cache_is_shared()):
        try:
            qs = DisplayScreen.objects.filter(is_active=True)
            # Prefer exact token match; short_code is not expected here (token length check in _extract_token).
            scr = qs.filter(token__iexact=token_value).only("school_id").first()
            if scr and getattr(scr, "school_id", None):
                school_id = int(scr.school_id)
                try:
                    cache.set(f"display:token_map:{token_hash}", {"school_id": int(school_id)}, timeout=60 * 60)
                except Exception:
                    pass
        except Exception:
            school_id = school_id

    if client_v is not None:
        if _cache_is_shared():
            current_rev, rev_source = _get_school_revision_cache_only(int(school_id or 0))
        else:
            current_rev, rev_source = _get_school_revision_cached(int(school_id or 0))

        _bump_metric("total")

        resolve_failed = not bool(school_id)
        if resolve_failed:
            _bump_metric("resolve_fail")

        # If we couldn't resolve school_id, we can't compare; force fetch.
        if current_rev is None:
            if not resolve_failed:
                _bump_metric("rev_miss")
            _bump_metric("fetch_required")
            resp = JsonResponse({"fetch_required": True}, json_dumps_params={"ensure_ascii": False})
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
            resp["Vary"] = "Accept-Encoding"
            try:
                resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
            except Exception:
                pass
            # Throttle warnings to avoid log storms if token->school mapping is missing.
            if _should_log_status(token_hash, interval=_status_warn_log_interval_seconds()):
                logger.warning(
                    "status_poll token_hash=%s school_id=%s client_v=%s current_rev=%s rev_source=%s resp=%s",
                    token_hash[:12],
                    school_id,
                    client_v,
                    current_rev,
                    rev_source,
                    200,
                )
            return resp

        # If a manual force-refresh was requested, always require a fetch.
        if force_reload:
            _bump_metric("fetch_required")
            resp = JsonResponse(
                {"fetch_required": True, "schedule_revision": int(current_rev or 0), "reload": True},
                json_dumps_params={"ensure_ascii": False},
            )
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
            resp["Vary"] = "Accept-Encoding"
            try:
                resp["X-Schedule-Revision"] = str(int(current_rev or 0))
            except Exception:
                pass
            try:
                resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
            except Exception:
                pass
            _metrics_incr("metrics:status:resp_200")
            return resp

        if force_refresh:
            _bump_metric("fetch_required")
            resp = JsonResponse(
                {"fetch_required": True, "schedule_revision": int(current_rev or 0)},
                json_dumps_params={"ensure_ascii": False},
            )
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
            resp["Vary"] = "Accept-Encoding"
            try:
                resp["X-Schedule-Revision"] = str(int(current_rev or 0))
            except Exception:
                pass
            try:
                resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
            except Exception:
                pass
            _metrics_incr("metrics:status:resp_200")
            return resp

        if int(client_v) == int(current_rev):
            _bump_metric("rev_hit")
            resp = HttpResponseNotModified()
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
            resp["Vary"] = "Accept-Encoding"
            resp["X-Schedule-Revision"] = str(int(current_rev))
            try:
                resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
            except Exception:
                pass
            _metrics_incr("metrics:status:resp_304")
            # Do NOT log 304s except sampling.
            if _should_log_status_304_sample(token_hash) and _should_log_status(token_hash):
                logger.info(
                    "status_poll token_hash=%s school_id=%s client_v=%s current_rev=%s rev_source=%s resp=%s",
                    token_hash[:12],
                    int(school_id or 0),
                    int(client_v),
                    int(current_rev),
                    rev_source,
                    304,
                )
            return resp

        _bump_metric("rev_hit")
        _bump_metric("fetch_required")
        resp = JsonResponse(
            {"fetch_required": True, "schedule_revision": int(current_rev)},
            json_dumps_params={"ensure_ascii": False},
        )
        resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
        resp["Vary"] = "Accept-Encoding"
        resp["X-Schedule-Revision"] = str(int(current_rev))
        try:
            resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
        except Exception:
            pass
        _metrics_incr("metrics:status:resp_200")
        # Log 200 updates, but throttle to once per (school_id, rev) per interval.
        if _should_log_status_200_school_rev(school_id=int(school_id or 0), rev=int(current_rev)):
            logger.info(
                "status_poll token_hash=%s school_id=%s client_v=%s current_rev=%s rev_source=%s resp=%s",
                token_hash[:12],
                int(school_id or 0),
                int(client_v),
                int(current_rev),
                rev_source,
                200,
            )
        return resp
    cache_key = get_cache_key(token_hash)

    cached_entry = cache.get(cache_key)
    if isinstance(cached_entry, dict) and isinstance(cached_entry.get("etag"), str):
        # ✅ Critical: if schedule revision changed, never return 304.
        # Otherwise the client will keep using old JSON forever.
        try:
            current_school_id = None
            cached_map = cache.get(f"display:token_map:{token_hash}")
            if isinstance(cached_map, dict):
                current_school_id = cached_map.get("school_id")
            else:
                try:
                    current_school_id = int(cached_map) if cached_map else None
                except Exception:
                    current_school_id = None

            # Cache-only: if revision cache is missing, force fetch_required.
            current_rev = get_cached_schedule_revision_for_school_id(int(current_school_id)) if current_school_id else None
            cached_rev = cached_entry.get("rev")
            if current_rev is None:
                resp = JsonResponse({"fetch_required": True}, json_dumps_params={"ensure_ascii": False})
                resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
                resp["Vary"] = "Accept-Encoding"
                try:
                    resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
                except Exception:
                    pass
                return resp
            if cached_rev is not None and int(current_rev) != int(cached_rev):
                resp = JsonResponse(
                    {"fetch_required": True, "schedule_revision": int(current_rev)},
                    json_dumps_params={"ensure_ascii": False},
                )
                resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
                resp["Vary"] = "Accept-Encoding"
                try:
                    resp["X-Schedule-Revision"] = str(int(current_rev))
                except Exception:
                    pass
                try:
                    resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
                except Exception:
                    pass
                return resp
        except Exception:
            pass

        inm = _parse_if_none_match(request.headers.get("If-None-Match"))
        etag = cached_entry.get("etag")
        if inm and etag and inm == etag:
            resp = HttpResponseNotModified()
            resp["ETag"] = f"\"{etag}\""
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
            resp["Vary"] = "Accept-Encoding"
            try:
                resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
            except Exception:
                pass
            _metrics_incr("metrics:status:resp_304")
            if _should_log_status_304_sample(token_hash) and _should_log_status(token_hash):
                logger.info(
                    "status_poll token_hash=%s school_id=%s client_v=%s current_rev=%s rev_source=%s resp=%s",
                    token_hash[:12],
                    current_school_id,
                    None,
                    current_rev,
                    "legacy",
                    304,
                )
            return resp

        resp = JsonResponse({"fetch_required": True, "etag": etag}, json_dumps_params={"ensure_ascii": False})
        resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
        resp["Vary"] = "Accept-Encoding"
        try:
            if current_rev is not None:
                resp["X-Schedule-Revision"] = str(int(current_rev or 0))
        except Exception:
            pass
        try:
            resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
        except Exception:
            pass
        _metrics_incr("metrics:status:resp_200")
        try:
            sid = int(current_school_id or 0)
            revv = int(current_rev or 0)
        except Exception:
            sid = 0
            revv = 0
        if (sid and _should_log_status_200_school_rev(school_id=sid, rev=revv)) or (not sid and _should_log_status(token_hash)):
            logger.info(
                "status_poll token_hash=%s school_id=%s client_v=%s current_rev=%s rev_source=%s resp=%s",
                token_hash[:12],
                current_school_id,
                None,
                current_rev,
                "legacy",
                200,
            )
        return resp

    resp = JsonResponse({"fetch_required": True}, json_dumps_params={"ensure_ascii": False})
    resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
    resp["Vary"] = "Accept-Encoding"
    try:
        resp["X-Server-Time-MS"] = str(int(timezone.now().timestamp() * 1000))
    except Exception:
        pass
    _metrics_incr("metrics:status:resp_200")
    if _should_log_status(token_hash):
        logger.info(
            "status_poll token_hash=%s school_id=%s client_v=%s current_rev=%s rev_source=%s resp=%s",
            token_hash[:12],
            school_id,
            None,
            None,
            "none",
            200,
        )
    return resp


def _is_hex_sha256(token_value: str) -> bool:
    if not token_value or len(token_value) != 64:
        return False
    try:
        int(token_value, 16)
        return True
    except Exception:
        return False


def _candidate_fields_for_model(model_cls) -> list[str]:
    keywords = ("token", "key", "api", "secret", "hash", "code", "slug")
    fields: list[str] = []
    for f in model_cls._meta.fields:
        if isinstance(f, (models.CharField, models.TextField)):
            n = f.name.lower()
            if any(k in n for k in keywords):
                fields.append(f.name)
    return fields


def _get_settings_by_school_id(school_id: int) -> SchoolSettings | None:
    return (
        SchoolSettings.objects.select_related("school")
        .filter(school_id=school_id)
        .first()
    )


def _get_schedule_revision_for_school_id(school_id: int) -> int:
    if not school_id:
        return 0
    # Cache-first to avoid repeated DB hits during snapshot polling.
    try:
        cached = get_cached_schedule_revision_for_school_id(int(school_id))
        if cached is not None:
            return int(cached)
    except Exception:
        pass
    try:
        rev = int(
            SchoolSettings.objects.filter(school_id=int(school_id)).values_list("schedule_revision", flat=True).first()
            or 0
        )
    except Exception:
        rev = 0
    try:
        set_cached_schedule_revision_for_school_id(int(school_id), int(rev))
    except Exception:
        pass
    return int(rev)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _token_variants_for_school_ids(school_id: int) -> Iterable[str]:
    secret = getattr(dj_settings, "DISPLAY_TOKEN_SALT", "") or dj_settings.SECRET_KEY
    sid = str(school_id)
    patterns = [
        f"{sid}:{secret}",
        f"{secret}:{sid}",
        f"display:{sid}:{secret}",
        f"{sid}{secret}",
        f"{secret}{sid}",
    ]
    for p in patterns:
        yield _sha256(p)


def _match_settings_by_hash_token(token_value: str) -> SchoolSettings | None:
    if not _is_hex_sha256(token_value):
        return None

    qs = SchoolSettings.objects.select_related("school").only("id", "school_id")
    for ss in qs:
        for v in _token_variants_for_school_ids(ss.school_id):
            if v == token_value:
                return SchoolSettings.objects.select_related("school").get(pk=ss.pk)
    return None


def _get_settings_by_token(token_value: str) -> SchoolSettings | None:
    if not token_value:
        return None

    ss_fields = _candidate_fields_for_model(SchoolSettings)
    if ss_fields:
        q = Q()
        for name in ss_fields:
            q |= Q(**{name: token_value})
        obj = SchoolSettings.objects.select_related("school").filter(q).first()
        if obj:
            return obj

    s_fields = _candidate_fields_for_model(School)
    if s_fields:
        q = Q()
        for name in s_fields:
            q |= Q(**{name: token_value})
        school = School.objects.filter(q).first()
        if school:
            return _get_settings_by_school_id(school.id)

    obj = _match_settings_by_hash_token(token_value)
    if obj:
        return obj

    return None


def _abs_media_url(request, maybe_url: str | None) -> str | None:
    if not maybe_url:
        return None
    s = str(maybe_url).strip()
    if s.lower() in {"none", "null", "-"}:
        return None
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        return s.replace("http://", "//").replace("https://", "//")
    try:
        return request.build_absolute_uri(s)
    except Exception:
        try:
            base = (
                os.getenv("DISPLAY_PUBLIC_BASE_URL", "").strip()
                or os.getenv("PUBLIC_BASE_URL", "").strip()
            ).rstrip("/")
        except Exception:
            base = ""
        if base:
            return f"{base}/{s.lstrip('/')}"
        return s


def _static_url(path: str | None) -> str:
    return static(path or "sounds/bell.mp3")


def _settings_bell_sound_path(settings_obj) -> str:
    get_path = getattr(settings_obj, "get_bell_sound_path", None)
    if callable(get_path):
        return get_path()
    bell_sound = getattr(settings_obj, "bell_sound", BELL_SOUND_DEFAULT) or BELL_SOUND_DEFAULT
    return BELL_SOUND_STATIC_PATHS.get(
        bell_sound,
        BELL_SOUND_STATIC_PATHS[BELL_SOUND_DEFAULT],
    )


def _model_has_field(model_cls, field_name: str) -> bool:
    try:
        model_cls._meta.get_field(field_name)
        return True
    except Exception:
        return False


def _get_active_screens_qs():
    qs = DisplayScreen.objects.all()
    if _model_has_field(DisplayScreen, "is_active"):
        qs = qs.filter(is_active=True)
    return qs.select_related("school")


def _match_settings_via_display_screen(token_value: str) -> Optional[SchoolSettings]:
    if not token_value:
        return None

    only_fields = ["id", "school_id"]
    if _model_has_field(DisplayScreen, "token"):
        only_fields.append("token")

    qs = _get_active_screens_qs().only(*only_fields)

    if _model_has_field(DisplayScreen, "token"):
        screen = qs.filter(token__iexact=token_value).first()
        if screen:
            return _get_settings_by_school_id(screen.school_id)

    if _is_hex_sha256(token_value) and _model_has_field(DisplayScreen, "token"):
        for s in qs:
            try:
                if _sha256(s.token) == token_value:
                    return _get_settings_by_school_id(s.school_id)
            except Exception:
                continue

    return None


def _parse_hhmm(value: str | None) -> dt_time | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        parts = s.split(":")
        if len(parts) == 2:
            h, m = int(parts[0]), int(parts[1])
            return dt_time(hour=h, minute=m)
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
            return dt_time(hour=h, minute=m, second=sec)
    except Exception:
        return None
    return None


def _infer_period_index(settings_obj: SchoolSettings, weekday: int, current_period: dict | None) -> int | None:
    if not current_period:
        return None

    idx = current_period.get("index")
    try:
        if idx is not None:
            idx_int = int(idx)
            if idx_int > 0:
                return idx_int
    except Exception:
        pass

    t_from = _parse_hhmm(current_period.get("from"))
    t_to = _parse_hhmm(current_period.get("to"))
    if not t_from or not t_to:
        return None

    try:
        return (
            Period.objects
            .filter(
                day__settings=settings_obj,
                day__weekday=weekday,
                starts_at=t_from,
                ends_at=t_to,
            )
            .values_list("index", flat=True)
            .first()
        )
    except Exception:
        return None


def _build_period_classes(settings_obj: SchoolSettings, weekday: int, period_index: int) -> list[dict]:
    qs = (
        ClassLesson.objects
        .filter(settings=settings_obj, weekday=weekday, period_index=period_index, is_active=True)
        .select_related("school_class", "subject", "teacher")
        .order_by("school_class__name")
    )
    items: list[dict] = []
    for cl in qs:
        items.append({
            "class": getattr(cl.school_class, "name", "") or "",
            "subject": getattr(cl.subject, "name", "") or "",
            "teacher": getattr(cl.teacher, "name", "") or "",
            "period_index": cl.period_index,
            "weekday": cl.weekday,
        })
    return items


def _build_period_classes_map(settings_obj: SchoolSettings, weekday: int) -> dict[str, list[dict]]:
    qs = (
        ClassLesson.objects
        .filter(settings=settings_obj, weekday=weekday, is_active=True)
        .select_related("school_class", "subject", "teacher")
        .order_by("period_index", "school_class__name")
    )
    out: dict[str, list[dict]] = {}
    for cl in qs:
        try:
            idx = int(getattr(cl, "period_index", 0) or 0)
        except Exception:
            idx = 0
        if idx <= 0:
            continue
        k = str(idx)
        out.setdefault(k, []).append({
            "class": getattr(cl.school_class, "name", "") or "",
            "subject": getattr(cl.subject, "name", "") or "",
            "teacher": getattr(cl.teacher, "name", "") or "",
            "period_index": idx,
            "weekday": cl.weekday,
        })
    return out


def _normalize_theme_value(raw: str | None) -> str:
    """
    SchoolSettings.theme عندك: default/boys/girls
    شاشة العرض/CSS: indigo/emerald/rose
    """
    v = (raw or "").strip().lower()
    if not v:
        return "indigo"

    if v in ("indigo", "emerald", "rose", "cyan", "amber", "orange", "violet"):
        return v

    if v in ("default", "theme_default"):
        return "indigo"
    if v in ("boys", "theme_boys"):
        return "emerald"
    if v in ("girls", "theme_girls"):
        return "rose"

    return "indigo"


_THEME_ACCENT_PRESETS = {
    "indigo": "#6366F1",
    "emerald": "#22C55E",
    "rose": "#EC4899",
    "cyan": "#06B6D4",
    "amber": "#EAB308",
    "orange": "#F97316",
    "violet": "#A855F7",
}


def _normalize_display_accent(raw: str | None, theme: str | None) -> str | None:
    accent = raw.strip() if isinstance(raw, str) else ""
    if accent and re.match(r"^#[0-9A-Fa-f]{6}$", accent):
        # Hidden type=color fields used to submit #000000 when empty. That value
        # is not part of the school dashboard palette, so fall back to the
        # selected theme instead of letting black override the display.
        if accent.upper() != "#000000":
            return accent.upper()

    normalized_theme = _normalize_theme_value(theme)
    return _THEME_ACCENT_PRESETS.get(normalized_theme)


def _merge_real_data_into_snapshot(request, snap: dict, settings_obj: SchoolSettings):
    """
    ✅ دمج بيانات المدرسة الحقيقية داخل snapshot:
    - announcements  (notices.Announcement)
    - excellence     (notices.Excellence)
    - standby        (standby.StandbyAssignment)
    - duty           (schedule.DutyAssignment)
    """
    school = getattr(settings_obj, "school", None)
    if not school:
        return

    # -----------------------------
    # Full-screen emergency alerts
    # -----------------------------
    try:
        from notices.models import EmergencyAlert

        now = timezone.now()
        emergency_qs = (
            EmergencyAlert.objects.filter(
                schools=school,
                is_active=True,
                cancelled_at__isnull=True,
                starts_at__lte=now,
            )
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
            .prefetch_related("screens")
            .order_by("-created_at")[:10]
        )
        snap["emergency_alerts"] = [alert.as_display_dict() for alert in emergency_qs]
    except Exception:
        logger.exception("snapshot: failed to merge emergency alerts")
        snap["emergency_alerts"] = []

    # -----------------------------
    # Announcements
    # -----------------------------
    try:
        from notices.models import Announcement  # type: ignore

        qs = Announcement.objects.active_for_school(school, now=timezone.now())

        order = []
        if _model_has_field(Announcement, "priority"):
            order.append("-priority")
        if _model_has_field(Announcement, "starts_at"):
            order.append("-starts_at")
        order.append("-id")
        qs = qs.prefetch_related("screens").order_by(*order)[:10]

        items = []
        for a in qs:
            d = a.as_dict() if hasattr(a, "as_dict") else {
                "title": getattr(a, "title", "") or "",
                "body": getattr(a, "body", "") or "",
                "level": getattr(a, "level", "") or "info",
            }
            title = (d.get("title") or "").strip()
            body = (d.get("body") or "").strip()
            if title and body:
                d["message"] = f"{title}\n{body}"
            else:
                d["message"] = title or body or "تنبيه"
            items.append(d)

        snap["announcements"] = items

    except Exception:
        logger.exception("snapshot: failed to merge announcements")

    # -----------------------------
    # Excellence (Honor Board)
    # -----------------------------
    try:
        from notices.models import Excellence  # type: ignore

        qs = Excellence.active_for_today(school) if hasattr(Excellence, "active_for_today") else Excellence.objects.filter(school=school)
        qs = qs[:30]

        items = []
        for e in qs:
            d = e.as_dict() if hasattr(e, "as_dict") else {
                "name": getattr(e, "teacher_name", "") or getattr(e, "name", "") or "",
                "reason": getattr(e, "reason", "") or "",
                "photo_url": getattr(e, "photo_url", None),
            }
            for k in ("image", "image_url", "photo_url"):
                if d.get(k):
                    d[k] = _abs_media_url(request, d.get(k))
            items.append(d)

        snap["excellence"] = items

    except Exception:
        logger.exception("snapshot: failed to merge excellence")

    # -----------------------------
    # Standby assignments
    # -----------------------------
    try:
        from standby.models import StandbyAssignment  # type: ignore

        today = timezone.localdate()
        qs = StandbyAssignment.objects.filter(school=school, date=today).order_by("period_index", "id")

        items = []
        for s in qs:
            items.append({
                "period_index": getattr(s, "period_index", None),
                "class_name": getattr(s, "class_name", "") or "",
                "teacher_name": getattr(s, "teacher_name", "") or "",
                "notes": getattr(s, "notes", "") or "",
            })

        snap["standby"] = items

    except Exception:
        logger.exception("snapshot: failed to merge standby")

    # -----------------------------
    # Duty / Supervision
    # -----------------------------
    try:
        from schedule.models import DutyAssignment  # type: ignore

        today = timezone.localdate()
        qs = (
            DutyAssignment.objects.filter(school=school, date=today, is_active=True)
            .order_by("priority", "-id")
        )

        snap["duty"] = {"items": [obj.as_dict() if hasattr(obj, "as_dict") else {
            "id": getattr(obj, "id", None),
            "date": getattr(obj, "date", None).isoformat() if getattr(obj, "date", None) else None,
            "teacher_name": getattr(obj, "teacher_name", "") or "",
            "duty_type": getattr(obj, "duty_type", "") or "",
            "duty_label": getattr(obj, "get_duty_type_display", lambda: "")() if hasattr(obj, "get_duty_type_display") else "",
            "location": getattr(obj, "location", "") or "",
        } for obj in qs]}

    except Exception:
        logger.exception("snapshot: failed to merge duty")


@require_GET
def ping(request):
    now = timezone.localtime()
    return JsonResponse({"ok": True, "now": now.isoformat()}, json_dumps_params={"ensure_ascii": False})


_GOODBYE_REASONS = {
    "pagehide",
    "unload",
    "beforeunload",
    "offline",
    "binding_lost",
    "heartbeat_timeout",
}


@csrf_exempt
@require_http_methods(["POST"])
def goodbye(request, token: str | None = None):
    """Farewell beacon from a display that is about to stop.

    A television being switched off and a school losing its internet look
    identical from the server's side — both simply go quiet. This one-shot
    `sendBeacon` on `pagehide` is what separates them, so the offline alert can
    say "the device was switched off" instead of "the screen is unreachable".

    Deliberately cheap and forgiving: no session, no CSRF (the token is the
    identity), and any failure is silent. It only ever writes a cache hint.
    """
    token_value = _extract_token(request, token)
    if not token_value:
        return JsonResponse({"ok": False}, status=400)

    reason = ""
    online = None
    code = None
    try:
        payload = json.loads((request.body or b"").decode("utf-8") or "{}")
        if isinstance(payload, dict):
            reason = str(payload.get("reason") or "").strip().lower()[:64]
            code = payload.get("code")
            code = int(code) if isinstance(code, (int, float)) else None
            if isinstance(payload.get("online"), bool):
                online = bool(payload["online"])
    except Exception:
        pass

    if reason not in _GOODBYE_REASONS:
        reason = "pagehide"

    try:
        screen = (
            DisplayScreen.objects.filter(token=token_value, is_active=True)
            .only("id")
            .first()
        )
        if screen is None:
            return JsonResponse({"ok": False}, status=404)
        from core.screen_diagnostics import record_disconnect_signal

        record_disconnect_signal(
            int(screen.pk),
            source="pagehide",
            code=code,
            reason=reason,
            online=online,
        )
    except Exception:
        logger.debug("goodbye beacon failed", exc_info=True)

    return JsonResponse({"ok": True})


def _call_build_day_snapshot(settings_obj: SchoolSettings) -> dict:
    now = timezone.localtime()
    try:
        return build_day_snapshot(settings_obj, now=now)
    except TypeError:
        try:
            school = getattr(settings_obj, "school", None)
            if school:
                return build_day_snapshot(school=school, for_date=now.date())
        except Exception:
            pass
        return build_day_snapshot(settings_obj)


def _is_missing_index(d: dict) -> bool:
    if "index" not in d:
        return True
    v = d.get("index")
    return v is None or v == "" or v == 0


def _snapshot_period_day_bounds(snap: dict) -> tuple[datetime, datetime] | None:
    """Return the real lesson span: first period start through last period end."""
    if not isinstance(snap, dict):
        return None

    try:
        now_dt = datetime.fromisoformat(str(snap.get("now") or "").strip())
    except Exception:
        now_dt = timezone.localtime()

    day_path = snap.get("day_path")
    if not isinstance(day_path, list):
        return None

    starts: list[datetime] = []
    ends: list[datetime] = []
    for block in day_path:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind") or block.get("type") or "").strip().lower()
        if kind != "period":
            continue
        from_t = _parse_hhmm(block.get("from") or block.get("starts_at"))
        to_t = _parse_hhmm(block.get("to") or block.get("ends_at"))
        if not from_t or not to_t:
            continue
        start = datetime.combine(now_dt.date(), from_t)
        end = datetime.combine(now_dt.date(), to_t)
        if now_dt.tzinfo is not None:
            start = start.replace(tzinfo=now_dt.tzinfo)
            end = end.replace(tzinfo=now_dt.tzinfo)
        if end <= start:
            continue
        starts.append(start)
        ends.append(end)

    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _snapshot_is_within_period_day(snap: dict) -> bool:
    bounds = _snapshot_period_day_bounds(snap)
    if bounds is None:
        return False
    start, end = bounds
    try:
        now_dt = datetime.fromisoformat(str(snap.get("now") or "").strip())
    except Exception:
        now_dt = timezone.localtime()
    return start <= now_dt < end


def _snapshot_is_before_period_day(snap: dict) -> bool:
    """Return true during the active wake window before the first lesson."""
    if not bool((snap.get("meta") or {}).get("is_active_window")):
        return False
    bounds = _snapshot_period_day_bounds(snap)
    if bounds is None:
        return False
    start, _end = bounds
    try:
        now_dt = datetime.fromisoformat(str(snap.get("now") or "").strip())
    except Exception:
        now_dt = timezone.localtime()
    return now_dt < start


def _clear_period_activity_boards(snap: dict) -> None:
    snap["period_classes"] = []
    snap["period_classes_map"] = {}


def _clear_school_day_boards(snap: dict) -> None:
    """Hide day-scoped panels outside the real lesson span."""
    _clear_period_activity_boards(snap)
    snap["standby"] = []
    snap["excellence"] = []
    snap["duty"] = {"items": []}


def _snapshot_cache_key(settings_obj: SchoolSettings) -> str:
    school_id = int(getattr(settings_obj, "school_id", None) or 0)
    rev = int(getattr(settings_obj, "schedule_revision", 0) or 0)
    return _steady_cache_key_for_school_rev(
        school_id,
        rev,
        day_key=timezone.localdate().isoformat(),
    )


def _snapshot_cache_ttl_seconds() -> int:
    try:
        v = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_CACHE_TTL", 900) or 900)
    except Exception:
        v = 900
    return max(5, min(900, v))


def _active_window_cache_ttl_seconds() -> int:
    """Cache TTL during active window (Phase 2).

    Defaults to 15–20s, but the upper bound can be raised via
    DISPLAY_SNAPSHOT_ACTIVE_TTL_MAX when operating large fleets.
    """
    try:
        v = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_ACTIVE_TTL", 15) or 15)
    except Exception:
        v = 15
    try:
        vmax = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_ACTIVE_TTL_MAX", 20) or 20)
    except Exception:
        vmax = 20
    vmax = max(15, min(60, vmax))
    out = max(15, min(vmax, v))
    if _safe_snapshot_rollout_enabled():
        try:
            safe_min = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_ACTIVE_TTL_SAFE_MIN", 30) or 30)
        except Exception:
            safe_min = 30
        safe_min = max(15, min(60, safe_min))
        out = max(out, safe_min)
    return out


def _active_snapshot_cache_ttl_seconds(day_snap: dict) -> int:
    """Active-window TTL aligned with client poll interval when available.

    Some fleets poll every ~20s. If active TTL is lower (e.g. 15s), we get
    avoidable MISS/build cycles. We keep server-config as the base, then lift
    it to the snapshot refresh interval (bounded) when present.
    """
    base = _active_window_cache_ttl_seconds()
    try:
        s = (day_snap.get("settings") or {}) if isinstance(day_snap, dict) else {}
        refresh = int(s.get("refresh_interval_sec") or 0)
    except Exception:
        refresh = 0
    if refresh > 0:
        base = max(base, min(60, refresh))
    return max(15, min(60, int(base)))

def _steady_snapshot_cache_ttl_seconds(day_snap: dict) -> int:
    """Outside active window/holidays: long TTL, aligned to refresh_interval_sec when available."""
    try:
        s = (day_snap.get("settings") or {}) if isinstance(day_snap, dict) else {}
        refresh = int(s.get("refresh_interval_sec") or 3600)
    except Exception:
        refresh = 3600
    # Day-aware + revision-aware keys let us keep steady snapshots longer without
    # leaking yesterday's payload into the next day.
    try:
        max_ttl = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_STEADY_MAX_TTL", 3600) or 3600)
    except Exception:
        max_ttl = 3600
    # Allow env to override up to 24h, while the default logic now caps at 3600s.
    max_ttl = max(300, min(86400, max_ttl))
    return max(300, min(max_ttl, refresh))


def _active_fallback_steady_ttl_seconds(snap: dict) -> int:
    """TTL for steady fallback key when current snapshot is active-window.

    Keep it modest (default 90s) and clamp by remaining_seconds so we never
    serve a block beyond its natural boundary.
    """
    try:
        v = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_ACTIVE_STEADY_TTL", 90) or 90)
    except Exception:
        v = 90
    v = max(30, min(300, v))
    v = max(v, _active_snapshot_cache_ttl_seconds(snap))
    return _clamp_active_ttl_by_remaining_seconds(snap, v)

def _is_active_window(day_snap: dict) -> bool:
    try:
        meta = day_snap.get("meta") or {}
        return bool(meta.get("is_active_window"))
    except Exception:
        return False

def _steady_snapshot_cache_key(settings_obj: SchoolSettings, day_snap: dict) -> str:
    school_id = int(getattr(settings_obj, "school_id", None) or 0)
    rev = int(getattr(settings_obj, "schedule_revision", 0) or 0)
    meta = (day_snap.get("meta") or {}) if isinstance(day_snap, dict) else {}
    return _steady_cache_key_for_school_rev(school_id, rev, day_key=meta.get("date"))


def get_cache_key(token_hash: str, school_id: int | None = None) -> str:
    """Tenant-safe snapshot cache key.

    If tokens are globally unique, token_hash alone is enough.
    We still include school_id whenever we have it, to avoid accidental collisions.
    """
    if school_id:
        return f"display:snapshot:{SNAPSHOT_CACHE_NAMESPACE}:{int(school_id)}:{token_hash}"
    return f"display:snapshot:{SNAPSHOT_CACHE_NAMESPACE}:{token_hash}"


def get_cache_key_rev(token_hash: str, school_id: int, schedule_revision: int) -> str:
    return f"display:snapshot:{SNAPSHOT_CACHE_NAMESPACE}:{int(school_id)}:rev:{int(schedule_revision)}:{token_hash}"


def compute_dynamic_ttl_seconds(day_snap: dict) -> int:
    if _is_active_window(day_snap):
        return _active_snapshot_cache_ttl_seconds(day_snap)
    return _steady_snapshot_cache_ttl_seconds(day_snap)


def _clamp_active_ttl_by_remaining_seconds(snap: dict, ttl: int) -> int:
    """Avoid caching a countdown snapshot past its natural boundary.

    If we cache a snapshot for (say) 15–20s while a period/break has 3s remaining,
    clients can keep receiving the *previous* block even after time has passed.

    However, clamping below the fleet polling interval (~20s) causes the cache to
    expire before the next poll arrives, leading to repeated cache MISS / rebuild
    cycles.  The client-side dayEngine already handles time-based transitions
    locally (via day_path + serverNowMs), so a short stale window is harmless.

    Minimum clamp: 15s — matches DISPLAY_SNAPSHOT_ACTIVE_TTL lower bound.
    """
    try:
        if not isinstance(snap, dict):
            return ttl
        st = snap.get("state") or {}
        st_type = str(st.get("type") or "").strip().lower()
        if st_type not in ("period", "break", "before"):
            return ttl
        rem = st.get("remaining_seconds")
        if isinstance(rem, (int, float)):
            r = int(rem)
            if r < 1:
                r = 1
            return max(15, min(int(ttl), r))
    except Exception:
        return ttl
    return ttl


def build_steady_snapshot(
    request,
    settings_obj: SchoolSettings,
    *,
    steady_state: str,
    refresh_interval_sec: int,
    label: str,
) -> dict:
    """Build a UI-safe steady snapshot (no expensive merges/queries).

    Required invariants:
    - never returns {}
    - includes expected arrays/keys
    - explicit state types for off-hours / no schedule
    """
    now = timezone.localtime()
    school = getattr(settings_obj, "school", None)

    school_name = ""
    if school is not None:
        school_name = getattr(school, "name", "") or ""
    if not school_name:
        school_name = getattr(settings_obj, "name", "") or ""

    logo = getattr(settings_obj, "logo_url", None)
    if not logo and school is not None:
        for attr in ("logo_url", "logo", "logo_image", "logo_file"):
            if hasattr(school, attr):
                val = getattr(school, attr)
                try:
                    logo = val.url
                except Exception:
                    logo = val
                if logo:
                    break

    return {
        "now": now.isoformat(),
        "meta": {
            "date": str(now.date()),
            "weekday": (now.date().weekday() + 1),
            "is_school_day": steady_state != "NO_SCHEDULE_TODAY",
            "is_active_window": False,
            "active_window": None,
        },
        "settings": {
            "name": school_name,
            "logo_url": _abs_media_url(request, logo),
            "theme": _normalize_theme_value(getattr(settings_obj, "theme", None)),
            "refresh_interval_sec": int(refresh_interval_sec),
            "standby_scroll_speed": float(getattr(settings_obj, "standby_scroll_speed", 0.8) or 0.8),
            "periods_scroll_speed": float(getattr(settings_obj, "periods_scroll_speed", 0.5) or 0.5),
            "bell_sound": getattr(settings_obj, "bell_sound", "bell") or "bell",
            "bell_sound_url": _static_url(_settings_bell_sound_path(settings_obj)),
        },
        "state": {
            "type": steady_state,
            "label": label,
            "from": None,
            "to": None,
            "remaining_seconds": 0,
        },
        "day_path": [],
        "current_period": None,
        "next_period": None,
        "period_classes": [],
        "period_classes_map": {},
        "standby": [],
        "excellence": [],
        "duty": {"items": []},
        "announcements": [],
        "emergency_alerts": [],
    }


def _snapshot_edge_cache_max_age_seconds() -> int:
    try:
        v = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_EDGE_MAX_AGE", 10) or 10)
    except Exception:
        v = 10
    return max(1, min(60, v))


def _snapshot_build_soft_timeout_ms() -> int:
    try:
        v = int(getattr(dj_settings, "DISPLAY_SNAPSHOT_BUILD_SOFT_TIMEOUT_MS", 1500) or 1500)
    except Exception:
        v = 1500
    return max(250, min(30000, v))


def _resolve_snapshot_weekday(meta: dict | None) -> int:
    if isinstance(meta, dict):
        weekday_raw = meta.get("weekday")
        try:
            return int(weekday_raw) if weekday_raw not in (None, "") else (timezone.localdate().weekday() + 1)
        except Exception:
            return timezone.localdate().weekday() + 1
    return timezone.localdate().weekday() + 1


def _display_message_defaults(settings_obj: SchoolSettings) -> dict[str, str]:
    return {
        "display_before_title": settings_obj.get_display_before_title(),
        "display_before_badge": settings_obj.get_display_before_badge(),
        "display_after_title": settings_obj.get_display_after_title(),
        "display_after_badge": settings_obj.get_display_after_badge(),
        "display_after_holiday_title": settings_obj.get_display_after_holiday_title(),
        "display_after_holiday_badge": settings_obj.get_display_after_holiday_badge(),
        "display_holiday_title": settings_obj.get_display_holiday_title(),
        "display_holiday_badge": settings_obj.get_display_holiday_badge(),
    }

def _build_final_snapshot(
    request,
    settings_obj: SchoolSettings,
    *,
    day_snap: dict | None = None,
    merge_real_data: bool = True,
) -> dict:
    snap = day_snap if isinstance(day_snap, dict) else _call_build_day_snapshot(settings_obj)
    snap = _normalize_snapshot_keys(snap)

    # مفاتيح أساسية
    snap.setdefault("meta", {})
    snap.setdefault("settings", {})
    snap.setdefault("state", {})
    snap.setdefault("day_path", [])
    snap.setdefault("current_period", None)
    snap.setdefault("next_period", None)
    snap.setdefault("period_classes", [])
    snap.setdefault("period_classes_map", {})
    snap.setdefault("standby", [])
    snap.setdefault("excellence", [])
    snap.setdefault("duty", {"items": []})
    snap.setdefault("announcements", [])
    snap.setdefault("emergency_alerts", [])

    try:
        snap["meta"]["is_period_day_window"] = _snapshot_is_within_period_day(snap)
    except Exception:
        snap["meta"]["is_period_day_window"] = False

    # settings unify + theme mapping
    s = snap["settings"] or {}
    school = getattr(settings_obj, "school", None)

    school_name = ""
    if school is not None:
        school_name = getattr(school, "name", "") or ""
    if not school_name:
        school_name = getattr(settings_obj, "name", "") or ""

    if school_name and not s.get("name"):
        s["name"] = school_name

    logo = s.get("logo_url") or getattr(settings_obj, "logo_url", None)
    if not logo and school is not None:
        for attr in ("logo_url", "logo", "logo_image", "logo_file"):
            if hasattr(school, attr):
                val = getattr(school, attr)
                try:
                    logo = val.url
                except Exception:
                    logo = val
                if logo:
                    break
    s["logo_url"] = _abs_media_url(request, logo)

    # ✅ الثيم: تحويل default/boys/girls -> indigo/emerald/rose
    s["theme"] = _normalize_theme_value(getattr(settings_obj, "theme", None) or s.get("theme"))
    from core.display_themes import effective_custom_theme, theme_payload
    s["custom_theme"] = theme_payload(effective_custom_theme(settings_obj))

    # ✅ Featured panel toggle (excellence|duty)
    s.setdefault("featured_panel", getattr(settings_obj, "featured_panel", "excellence") or "excellence")

    s.setdefault("refresh_interval_sec", getattr(settings_obj, "refresh_interval_sec", 10) or 10)
    s.setdefault("standby_scroll_speed", getattr(settings_obj, "standby_scroll_speed", 0.8) or 0.8)
    s.setdefault("periods_scroll_speed", getattr(settings_obj, "periods_scroll_speed", 0.5) or 0.5)
    s["bell_sound"] = getattr(settings_obj, "bell_sound", "bell") or "bell"
    s["bell_sound_url"] = _static_url(_settings_bell_sound_path(settings_obj))
    for message_key, message_value in _display_message_defaults(settings_obj).items():
        s.setdefault(message_key, message_value)

    # ✅ لون شاشة العرض (اختياري)
    s["display_accent_color"] = _normalize_display_accent(
        getattr(settings_obj, "display_accent_color", None) or s.get("display_accent_color"),
        s.get("theme"),
    )
    snap["settings"] = s

    # ✅ ROOT FIX: merge real data
    if merge_real_data:
        # ✅ ROOT FIX: merge real data
        _merge_real_data_into_snapshot(request, snap, settings_obj)

    # ✅ لو period_classes فاضية — نعبيها من ClassLesson
    # نبني الخريطة أيضاً قبل بداية أول حصة (داخل نافذة الاستيقاظ) حتى تكون
    # بيانات الإسناد جاهزة لدى الشاشة لحظة دخول الحصة الأولى دون انتظار
    # snapshot جديد — الانتقال عند الحدود يتم محلياً في العميل.
    try:
        _before_period_day = _snapshot_is_before_period_day(snap)
    except Exception:
        _before_period_day = False
    if merge_real_data and (bool((snap.get("meta") or {}).get("is_period_day_window")) or _before_period_day):
        meta = snap.get("meta") or {}
        weekday = _resolve_snapshot_weekday(meta if isinstance(meta, dict) else None)
        period_map = snap.get("period_classes_map") if isinstance(snap.get("period_classes_map"), dict) else {}
        inferred_index_cache: dict[tuple[object, ...], int | None] = {}

        def _infer_period_index_cached(block: dict | None) -> int | None:
            if not isinstance(block, dict):
                return None
            cache_key = (
                block.get("kind") or block.get("type"),
                block.get("index"),
                block.get("from"),
                block.get("to"),
            )
            if cache_key not in inferred_index_cache:
                inferred_index_cache[cache_key] = _infer_period_index(settings_obj, weekday, block)
            return inferred_index_cache[cache_key]

        try:
            # Build map first so current-period classes can reuse the same query.
            if not period_map:
                period_map = _build_period_classes_map(settings_obj, weekday)
                snap["period_classes_map"] = period_map
        except Exception:
            logger.exception("snapshot: failed to build period_classes_map")
            weekday = timezone.localdate().weekday() + 1
            period_map = snap.get("period_classes_map") if isinstance(snap.get("period_classes_map"), dict) else {}

        try:
            current = snap.get("current_period") or {}
            kind = None
            if isinstance(current, dict):
                kind = current.get("kind") or current.get("type")
            if not kind:
                kind = (snap.get("state") or {}).get("type")

            if kind == "period" and not snap.get("period_classes"):
                period_index = None
                if isinstance(current, dict):
                    try:
                        period_index = int(current.get("index") or 0) or None
                    except Exception:
                        period_index = None
                if not period_index:
                    period_index = _infer_period_index_cached(current if isinstance(current, dict) else None)

                if period_index:
                    from_map = period_map.get(str(period_index)) if isinstance(period_map, dict) else None
                    if isinstance(from_map, list):
                        snap["period_classes"] = list(from_map)
                    elif period_map:
                        snap["period_classes"] = []
                    else:
                        snap["period_classes"] = _build_period_classes(settings_obj, weekday, period_index)

                    if isinstance(snap.get("current_period"), dict) and _is_missing_index(snap["current_period"]):
                        snap["current_period"]["index"] = period_index
        except Exception:
            logger.exception("snapshot: failed to fill period_classes")

        # ✅ ضمان ظهور رقم الحصة للـ current و next
        try:
            curp = snap.get("current_period")
            if isinstance(curp, dict) and _is_missing_index(curp):
                idx = _infer_period_index_cached(curp)
                if idx:
                    curp["index"] = idx

            nxtp = snap.get("next_period")
            if isinstance(nxtp, dict) and _is_missing_index(nxtp):
                idx2 = _infer_period_index_cached(nxtp)
                if idx2:
                    nxtp["index"] = idx2
        except Exception:
            logger.exception("snapshot: failed to ensure current/next period index")

    if not bool((snap.get("meta") or {}).get("is_period_day_window")):
        if _before_period_day:
            # Preload today's boards while the preparation screen is up.
            # The client keeps them hidden until the exact first-period boundary,
            # then reveals them locally without waiting for another snapshot —
            # so period_classes_map must survive here; only the "current period"
            # list is emptied because no period is running yet.
            snap["period_classes"] = []
        else:
            _clear_school_day_boards(snap)

    return snap


def _build_snapshot_payload(
    request,
    settings_obj: SchoolSettings,
    *,
    school_id: int | None = None,
    rev: int | None = None,
) -> tuple[dict, int]:
    school_id = int(school_id or getattr(settings_obj, "school_id", 0) or 0)
    rev = int(rev if rev is not None else (getattr(settings_obj, "schedule_revision", 0) or 0))
    t0 = time.monotonic()

    day_snap = _normalize_snapshot_keys(_call_build_day_snapshot(settings_obj))
    active_window = _is_active_window(day_snap)

    try:
        meta = day_snap.get("meta") if isinstance(day_snap, dict) else None
        if not isinstance(meta, dict):
            meta = {}
            if isinstance(day_snap, dict):
                day_snap["meta"] = meta
        meta["schedule_revision"] = rev
        meta["ws_enabled"] = bool(getattr(dj_settings, "DISPLAY_WS_ENABLED", False))
    except Exception:
        pass

    if active_window:
        snap = _build_final_snapshot(request, settings_obj, day_snap=day_snap, merge_real_data=True)
    else:
        # Steady snapshots are still revision/day cached, so merging dashboard
        # content here keeps after-hours TVs current without adding per-request
        # database load on cache hits.
        snap = _build_final_snapshot(request, settings_obj, day_snap=day_snap, merge_real_data=True)

        meta = day_snap.get("meta") or {}
        is_school_day = bool(meta.get("is_school_day"))
        st = (day_snap.get("state") or {}) if isinstance(day_snap, dict) else {}
        st_type = str(st.get("type") or "").strip().lower()
        st_reason = str(st.get("reason") or "").strip().lower()

        base_refresh = int((day_snap.get("settings") or {}).get("refresh_interval_sec") or 3600)
        if st_reason == "awaiting_setup":
            # A school still entering its timetable is the one case that needs a
            # tight cycle: the manager is watching the TV while typing, and the
            # ten-minute floor below would make the board look broken.
            refresh = max(30, min(base_refresh, 120))
        elif not is_school_day:
            refresh = max(base_refresh, 3600)
        elif st_type == "before" or st_reason == "before_hours":
            refresh = max(base_refresh, 60)
        else:
            refresh = max(base_refresh, 600)

        refresh = max(5, min(86400, int(refresh)))

        if st_reason == "awaiting_setup":
            # Keep the setup wording intact. Relabelling it "after hours" would
            # tell a school with no timetable that its school day has ended.
            snap["state"]["type"] = "AWAITING_SETUP"
            snap["state"]["label"] = str(st.get("label") or "").strip()
            snap["state"]["badge"] = str(st.get("badge") or "").strip()
            snap["state"]["reason"] = "awaiting_setup"
        elif not is_school_day:
            snap["state"]["type"] = "NO_SCHEDULE_TODAY"
            snap["state"]["label"] = str(st.get("label") or "").strip() or settings_obj.get_display_holiday_title()
            snap["state"]["badge"] = str(st.get("badge") or "").strip() or settings_obj.get_display_holiday_badge()
            snap["state"]["reason"] = "holiday"
        else:
            state_label = str(st.get("label") or "").strip()
            state_badge = str(st.get("badge") or "").strip()
            is_before_hours = st_type == "before" or st_reason == "before_hours"
            if not is_before_hours and st_type == "off":
                now_dt = None
                active_window_meta = meta.get("active_window") or {}
                active_start_dt = None
                try:
                    now_dt = datetime.fromisoformat(str(day_snap.get("now") or snap.get("now") or "").strip())
                except Exception:
                    now_dt = None
                try:
                    active_start_dt = datetime.fromisoformat(str((active_window_meta or {}).get("start") or "").strip())
                except Exception:
                    active_start_dt = None
                if now_dt is not None and active_start_dt is not None and now_dt < active_start_dt:
                    is_before_hours = True

            if is_before_hours:
                snap["state"]["type"] = "BEFORE_SCHOOL"
                snap["state"]["label"] = state_label or settings_obj.get_display_before_title()
                snap["state"]["badge"] = state_badge or settings_obj.get_display_before_badge()
                snap["state"]["reason"] = "before_hours"
            else:
                snap["state"]["type"] = "OFF_HOURS"
                snap["state"]["label"] = state_label or settings_obj.get_display_after_title()
                snap["state"]["badge"] = state_badge or settings_obj.get_display_after_badge()
                snap["state"]["reason"] = st_reason or "after_hours"

        try:
            snap["settings"]["refresh_interval_sec"] = int(refresh)
        except Exception:
            pass

    build_ms = int((time.monotonic() - t0) * 1000)
    soft_timeout_ms = _snapshot_build_soft_timeout_ms()
    if build_ms >= soft_timeout_ms:
        _metrics_incr("metrics:snapshot_build:soft_timeout")
    try:
        meta = snap.get("meta") if isinstance(snap, dict) else None
        if not isinstance(meta, dict):
            meta = {}
            if isinstance(snap, dict):
                snap["meta"] = meta
        meta["snapshot_build_ms"] = int(build_ms)
    except Exception:
        pass
    _metrics_incr("metrics:snapshot_cache:build_count")
    _metrics_add("metrics:snapshot_cache:build_sum_ms", build_ms)
    _metrics_set_max("metrics:snapshot_cache:build_max_ms", build_ms)
    if build_ms >= soft_timeout_ms:
        _obs_log_event(
            logger,
            "snapshot_build_budget",
            source="inline" if active_window else "steady",
            school_id=school_id,
            rev=rev,
            day_key=(day_snap.get("meta") or {}).get("date") if isinstance(day_snap, dict) else None,
            duration_ms=build_ms,
            soft_timeout_ms=soft_timeout_ms,
            exceeded=True,
        )
    _metrics_log_maybe()

    return snap, build_ms


@require_http_methods(["GET", "HEAD"])
def snapshot(request, token: str | None = None):
    """
    GET /api/display/snapshot/
    GET /api/display/snapshot/<token>/
    """
    # تعريف جميع المتغيرات المطلوبة لتجنب أخطاء undefined variable
    settings_obj = None
    rev = None
    school_id = None
    display_keys = None
    school_id_fast = None
    rev_fast = None
    day_key_fast = None
    school_snap_key_fast = None
    school_lock_key_fast = None
    SCHOOL_SNAPSHOT_TTL = int(getattr(dj_settings, "SCHOOL_SNAPSHOT_TTL", 1200) or 1200)
    cache_debug = False
    transition_allowed = False
    token_hash = None
    cache_key = None
    tenant_cache_key = None
    hashed_token = None
    cached_school_id = None
    force_nocache = False

    def _finalize(resp, cache_status=None, device_bound=None, school_id=None, rev=None):
        """Finalize snapshot responses with stable headers and lightweight diagnostics."""
        # Hardened cache headers: old TV browsers (Tizen ≤3, WebOS ≤3) and many
        # school/ISP proxies ignore plain `no-store`. Combine with `no-cache`,
        # `must-revalidate`, `max-age=0`, `private` and `Pragma` so intermediate
        # caches cannot serve stale snapshots after a settings change.
        cur_cc = resp.get("Cache-Control") or ""
        if "no-store" not in cur_cc:
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
        elif "no-cache" not in cur_cc:
            resp["Cache-Control"] = cur_cc + ", no-cache, must-revalidate, max-age=0, private"
        resp["Pragma"] = "no-cache"
        resp["Expires"] = "0"
        # Keep compression negotiation; never vary on Cookie.
        resp["Vary"] = "Accept-Encoding"
        if cache_status:
            resp["X-Cache-Status"] = str(cache_status)
            resp["X-Snapshot-Cache"] = str(cache_status)
        if transition_allowed:
            resp["X-Snapshot-Transition"] = "1"
        try:
            resp["X-Server-Time-MS"] = str(int(time.time() * 1000))
        except Exception:
            pass
        if device_bound is not None:
            resp["X-Device-Bound"] = str(int(bool(device_bound)))
            resp["X-Snapshot-Device-Bound"] = "1" if device_bound else "0"
        if school_id is not None:
            resp["X-School-Id"] = str(school_id)
        if rev is not None:
            resp["X-Revision"] = str(rev)
        payload_bytes = 0
        try:
            if int(getattr(resp, "status_code", 0) or 0) == 200:
                payload_bytes = len(getattr(resp, "content", b"") or b"")
                if payload_bytes:
                    resp["X-Snapshot-Bytes"] = str(int(payload_bytes))
        except Exception:
            payload_bytes = 0
        try:
            if school_id is not None:
                cache.set(
                    f"metrics:snapshot_cache:last_cache_status:{int(school_id)}",
                    str(cache_status or ""),
                    timeout=60 * 60 * 24,
                )
                cache.set(
                    f"metrics:snapshot_cache:last_payload_bytes:{int(school_id)}",
                    int(payload_bytes),
                    timeout=60 * 60 * 24,
                )
                if rev is not None:
                    cache.set(
                        f"metrics:snapshot_cache:last_rev:{int(school_id)}",
                        int(rev),
                        timeout=60 * 60 * 24,
                    )
        except Exception:
            pass
        if app_rev:
            resp["X-App-Revision"] = app_rev
        try:
            status_code = int(getattr(resp, "status_code", 0) or 0)
            if _should_log_snapshot_resp(
                school_id=school_id,
                rev=rev,
                cache_status=cache_status,
                status_code=status_code,
            ):
                logger.info(
                    "snapshot_resp school_id=%s rev=%s status=%s cache=%s payload_bytes=%s",
                    school_id,
                    rev,
                    status_code,
                    cache_status,
                    int(payload_bytes),
                )
        except Exception:
            pass
        return resp

    try:
        # IMPORTANT (Production): do not allow query params to defeat caching.
        # Some screens may accidentally run with `?debug=1` / `?nocache=1` and spam the server.
        # We only honor nocache while developing locally (DEBUG=True).
        force_nocache = bool(dj_settings.DEBUG) and (request.GET.get("nocache") or "").strip().lower() in {"1", "true", "yes"}

        # Production-safe transition refresh: used at countdown==0.
        # Unlike nocache, this is allowed in production but is guarded by device binding + per-device rate limit.
        transition_requested = (request.GET.get("transition") or "").strip().lower() in {"1", "true", "yes"}
        transition_allowed = False

        # Diagnostics (off by default): enable extra cache logs to validate cache hit/miss behavior.
        cache_debug = (os.getenv("DISPLAY_SNAPSHOT_CACHE_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"})

        # Log cache env (sanitized) once per request when debug is enabled.
        if cache_debug and (getattr(request, "_cache_env_logged", False) is False):
            _log_cache_env(logger)
            try:
                setattr(request, "_cache_env_logged", True)
            except Exception:
                pass

        path = getattr(request, "path", "") or ""
        is_snapshot_path = path.startswith("/api/display/snapshot/")

        app_rev = _app_revision()

        def _apply_success_cache_headers(resp, snap: dict | None):
            """Allow short edge caching (Cloudflare) while avoiding browser caching."""
            try:
                status = int(getattr(resp, "status_code", 0) or 0)
                if status not in (200, 304):
                    return resp
                if not isinstance(snap, dict):
                    return resp

                edge_cap = _snapshot_edge_cache_max_age_seconds()
                ttl = compute_dynamic_ttl_seconds(snap)
                ttl = _clamp_active_ttl_by_remaining_seconds(snap, ttl)
                edge_ttl = max(1, min(int(edge_cap), int(ttl)))

                # Browser: effectively bypass (must revalidate). Edge: allow small cache.
                resp["Cache-Control"] = f"public, max-age=0, must-revalidate, s-maxage={edge_ttl}"
            except Exception:
                pass
            return resp

        token_value = _extract_token(request, token)
        if not token_value:
            resp = JsonResponse(
                _fallback_payload("رمز الدخول غير صحيح"),
                json_dumps_params={"ensure_ascii": False},
                status=403,
            )
            return _finalize(
                resp,
                cache_status="ERROR",
                device_bound=False if is_snapshot_path else None,
                school_id=None,
                rev=None,
            )

        token_hash = _sha256(token_value)
        cache_key = get_cache_key(token_hash)

        SCHOOL_LOCK_TTL = int(getattr(dj_settings, "SCHOOL_SNAPSHOT_LOCK_TTL", 8) or 8)
        SCHOOL_WAIT_TIMEOUT = float(getattr(dj_settings, "SCHOOL_SNAPSHOT_WAIT_TIMEOUT", 0.7) or 0.7)
        SCHOOL_LOCK_TTL = max(3, min(30, SCHOOL_LOCK_TTL))
        SCHOOL_WAIT_TIMEOUT = max(0.1, min(2.0, SCHOOL_WAIT_TIMEOUT))

        try:
            from display.cache_utils import keys as display_keys, cache_add_lock, cache_wait_for
            from display.services import get_school_id_by_token, get_day_key
        except Exception:
            display_keys = None
            cache_add_lock = None
            cache_wait_for = None
            get_school_id_by_token = None
            get_day_key = None

        if not _snapshot_anti_loop_check(token_hash):
            if dj_settings.DEBUG:
                logger.warning("Anti-loop triggered for token_hash=%s", token_hash[:8])
            payload = _fallback_payload("التحديث متوقف مؤقتًا (حماية النظام)")
            payload["settings"]["refresh_interval_sec"] = 30
            resp = JsonResponse(payload, json_dumps_params={"ensure_ascii": False})
            return _finalize(
                resp,
                cache_status="LOOP",
                device_bound=True if is_snapshot_path else None,
            )

        device_key = ""
        is_preview = False
        if is_snapshot_path:
            device_key = (request.headers.get("X-Display-Device") or "").strip()
            if not device_key:
                device_key = (request.GET.get("dk") or request.GET.get("device_key") or "").strip()

            # A dashboard preview reads the board without claiming the screen.
            from display.services import resolve_preview_screen

            is_preview = resolve_preview_screen(request, token_value) is not None

            if not device_key and not is_preview:
                resp = JsonResponse({"detail": "device_required"}, status=403)
                return _finalize(
                    resp,
                    cache_status="ERROR",
                    device_bound=False,
                    school_id=None,
                    rev=None,
                )

            # Previews still share the per-screen rate limit; they simply do not
            # get a device identity of their own.
            device_hash = _sha256(device_key or f"preview:{token_hash}")

            if transition_requested:
                try:
                    tr_key = f"rl:snapshot_transition:{token_hash[:12]}:{device_hash[:12]}"
                    transition_allowed = bool(cache.add(tr_key, "1", timeout=12))
                except Exception:
                    transition_allowed = True

            if not _snapshot_rate_limit_allow(token_hash, device_hash):
                resp = JsonResponse({"detail": "rate_limited"}, status=429)
                return _finalize(
                    resp,
                    cache_status="MISS",
                    device_bound=True,
                    school_id=None,
                    rev=None,
                )

            try:
                from display.services import (
                    ScreenBoundError,
                    ScreenNotFoundError,
                    bind_device_atomic,
                )

                if is_preview:
                    screen = None
                else:
                    screen = bind_device_atomic(token=token_value, device_id=device_key)
                    touch_display_presence(screen.pk, token=token_value)
            except ScreenNotFoundError:
                resp = JsonResponse(
                    {
                        "detail": "access_denied",
                        "message": "\u062a\u0639\u0630\u0631 \u0627\u0644\u0648\u0635\u0648\u0644",
                    },
                    status=403,
                )
                return _finalize(
                    resp,
                    cache_status="ERROR",
                    device_bound=False,
                    school_id=None,
                    rev=None,
                )
            except ScreenBoundError as e:
                logger.warning(
                    "device_binding_reject token_hash=%s device=%s reason=%s",
                    token_hash[:12],
                    device_key[:8],
                    str(e),
                )
                resp = JsonResponse(
                    {
                        "detail": "screen_bound",
                        "message": "\u0647\u0630\u0647 \u0627\u0644\u0634\u0627\u0634\u0629 \u0645\u0631\u062a\u0628\u0637\u0629 \u0628\u062c\u0647\u0627\u0632 \u0622\u062e\u0631",
                    },
                    status=403,
                )
                return _finalize(
                    resp,
                    cache_status="ERROR",
                    device_bound=True,
                    school_id=None,
                    rev=None,
                )
            except Exception as e:
                logger.exception("Device binding error token_hash=%s: %s", token_hash[:12], e)
                resp = JsonResponse({"detail": "internal_error"}, status=500)
                return _finalize(
                    resp,
                    cache_status="ERROR",
                    device_bound=False,
                    school_id=None,
                    rev=None,
                )

        cached_entry, _ = (
            (None, "missing")
            if (force_nocache or transition_allowed)
            else _validated_snapshot_cache_entry_from_value(cache.get(cache_key))
        )
        if isinstance(cached_entry, dict) and isinstance(cached_entry.get("snap"), dict) and isinstance(cached_entry.get("etag"), str):
            school_id_for_log = None
            try:
                cached_map = cache.get(f"display:token_map:{token_hash}")
                if isinstance(cached_map, dict):
                    school_id_for_log = cached_map.get("school_id")
                else:
                    try:
                        school_id_for_log = int(cached_map) if cached_map else None
                    except Exception:
                        school_id_for_log = None
            except Exception:
                pass

        if isinstance(cached_entry, dict) and isinstance(cached_entry.get("snap"), dict) and isinstance(cached_entry.get("etag"), str):
            _metrics_incr("metrics:snapshot_cache:token_hit")
            _obs_snapshot_cache(logger=logger, outcome="hit", layer="token", school_id=school_id_for_log, cache_key=cache_key)
            _metrics_log_maybe()
            inm = _parse_if_none_match(request.headers.get("If-None-Match"))
            if inm and inm == cached_entry.get("etag"):
                resp = HttpResponseNotModified()
                resp["ETag"] = f"\"{cached_entry['etag']}\""
                _apply_success_cache_headers(resp, cached_entry.get("snap"))
                return _finalize(
                    resp,
                    cache_status="HIT",
                    device_bound=True if is_snapshot_path else None,
                    school_id=school_id_for_log,
                    rev=cached_entry.get("rev"),
                )
            resp = JsonResponse(cached_entry["snap"], json_dumps_params={"ensure_ascii": False})
            resp["ETag"] = f"\"{cached_entry['etag']}\""
            _apply_success_cache_headers(resp, cached_entry.get("snap"))
            return _finalize(
                resp,
                cache_status="HIT",
                device_bound=True if is_snapshot_path else None,
                school_id=school_id_for_log,
                rev=cached_entry.get("rev"),
            )

        _metrics_incr("metrics:snapshot_cache:token_miss")
        _obs_snapshot_cache(logger=logger, outcome="miss", layer="token", cache_key=cache_key, reason="token_cache_miss")
        _metrics_log_maybe()

        if not force_nocache and display_keys is not None:
            school_id_fast = None
            if get_school_id_by_token is not None:
                try:
                    school_id_fast = get_school_id_by_token(token_value)
                except Exception:
                    school_id_fast = None

            if not school_id_fast:
                try:
                    map_cached_fast = cache.get(f"display:token_map:{token_hash}")
                    if isinstance(map_cached_fast, dict):
                        sid_fast = map_cached_fast.get("school_id")
                    else:
                        sid_fast = map_cached_fast
                    if sid_fast:
                        school_id_fast = int(sid_fast)
                except Exception:
                    school_id_fast = None

            if not school_id_fast and is_snapshot_path:
                try:
                    if "screen" in locals() and getattr(screen, "school_id", None):
                        school_id_fast = int(screen.school_id)
                except Exception:
                    school_id_fast = None

            if school_id_fast:
                rev_fast = None
                day_key_fast = None
                try:
                    rev_fast = _get_schedule_revision_for_school_id(int(school_id_fast))
                    day_key_fast = _snapshot_cache_day_key(get_day_key() if get_day_key is not None else None)
                    school_snap_key_fast = _steady_cache_key_for_school_rev(
                        int(school_id_fast),
                        int(rev_fast),
                        day_key=day_key_fast,
                    )
                except Exception:
                    school_snap_key_fast = None

                if cache_debug:
                    try:
                        logger.info(
                            "school_snapshot_inputs school_id=%s rev=%s day_key=%s",
                            int(school_id_fast),
                            int(rev_fast) if rev_fast is not None else None,
                            str(day_key_fast) if day_key_fast is not None else None,
                        )
                    except Exception:
                        pass

            if school_id_fast and school_snap_key_fast:
                try:
                    cached_school_blob = cache.get(school_snap_key_fast)
                except Exception:
                    cached_school_blob = None

                if cached_school_blob is not None:
                    _metrics_incr("metrics:snapshot_cache:school_hit")
                    _obs_snapshot_cache(
                        logger=logger,
                        outcome="hit",
                        layer="school",
                        school_id=int(school_id_fast),
                        rev=int(rev_fast) if rev_fast is not None else None,
                        day_key=str(day_key_fast or ""),
                        cache_key=school_snap_key_fast,
                    )
                    _metrics_log_maybe()
                    if cache_debug:
                        try:
                            logger.info("school_snapshot_get key=%s hit=1", school_snap_key_fast)
                        except Exception:
                            pass

                    entry_fast, _ = _validated_snapshot_cache_entry_from_value(
                        cached_school_blob,
                        min_rev=int(rev_fast or 0),
                        cache_key=school_snap_key_fast,
                    )

                    if isinstance(entry_fast, dict) and isinstance(entry_fast.get("snap"), dict):
                        snap_fast = entry_fast["snap"]
                        etag_fast = entry_fast.get("etag")
                        inm = _parse_if_none_match(request.headers.get("If-None-Match"))
                        if etag_fast and inm and inm == etag_fast:
                            resp = HttpResponseNotModified()
                            resp["ETag"] = f"\"{etag_fast}\""
                            _apply_success_cache_headers(resp, snap_fast)
                            return _finalize(
                                resp,
                                cache_status="HIT",
                                device_bound=True if is_snapshot_path else None,
                                school_id=int(school_id_fast),
                                rev=int(rev_fast) if rev_fast is not None else None,
                            )

                        resp = HttpResponse(
                            _snapshot_entry_body_bytes(entry_fast),
                            content_type="application/json; charset=utf-8",
                        )
                        if etag_fast:
                            resp["ETag"] = f"\"{etag_fast}\""
                        _apply_success_cache_headers(resp, snap_fast)
                        return _finalize(
                            resp,
                            cache_status="HIT",
                            device_bound=True if is_snapshot_path else None,
                            school_id=int(school_id_fast),
                            rev=int(rev_fast) if rev_fast is not None else None,
                        )

                if cache_debug:
                    try:
                        logger.info("school_snapshot_get key=%s hit=0", school_snap_key_fast)
                    except Exception:
                        pass

                if cache_wait_for is not None:
                    try:
                        waited0 = cache_wait_for(school_snap_key_fast, timeout_s=SCHOOL_WAIT_TIMEOUT, step_s=0.05)
                    except Exception:
                        waited0 = None

                    if waited0 is not None:
                        entry_wait0, _ = _validated_snapshot_cache_entry_from_value(
                            waited0,
                            min_rev=int(rev_fast or 0),
                            cache_key=school_snap_key_fast,
                        )
                        if isinstance(entry_wait0, dict) and isinstance(entry_wait0.get("snap"), dict):
                            snap_wait0 = entry_wait0["snap"]
                            _metrics_incr("metrics:snapshot_cache:school_hit")
                            _obs_snapshot_cache(
                                logger=logger,
                                outcome="hit",
                                layer="school_wait",
                                school_id=int(school_id_fast),
                                rev=int(rev_fast) if rev_fast is not None else None,
                                day_key=str(day_key_fast or ""),
                                cache_key=school_snap_key_fast,
                            )
                            _metrics_log_maybe()
                            resp = HttpResponse(
                                _snapshot_entry_body_bytes(entry_wait0),
                                content_type="application/json; charset=utf-8",
                            )
                            if entry_wait0.get("etag"):
                                resp["ETag"] = f"\"{entry_wait0['etag']}\""
                            _apply_success_cache_headers(resp, snap_wait0)
                            return _finalize(
                                resp,
                                cache_status="HIT",
                                device_bound=True if is_snapshot_path else None,
                                school_id=int(school_id_fast),
                                rev=int(rev_fast) if rev_fast is not None else None,
                            )

                try:
                    school_lock_key_fast = display_keys.snapshot_lock(int(school_id_fast), int(rev_fast or 0), str(day_key_fast or ""))
                except Exception:
                    school_lock_key_fast = None

                got_lock = True
                if school_lock_key_fast and cache_add_lock is not None:
                    try:
                        got_lock = bool(cache_add_lock(school_lock_key_fast, ttl=SCHOOL_LOCK_TTL))
                    except Exception:
                        got_lock = True

                if not got_lock:
                    _metrics_incr("metrics:snapshot_cache:build_lock_contention")
                    _obs_snapshot_cache(
                        logger=logger,
                        outcome="miss",
                        layer="school",
                        school_id=int(school_id_fast),
                        rev=int(rev_fast) if rev_fast is not None else None,
                        day_key=str(day_key_fast or ""),
                        cache_key=school_snap_key_fast,
                        reason="fast_path_build_lock_contention",
                    )
                    waited2 = None
                    if cache_wait_for is not None:
                        try:
                            waited2 = cache_wait_for(school_snap_key_fast, timeout_s=SCHOOL_WAIT_TIMEOUT, step_s=0.05)
                        except Exception:
                            waited2 = None

                    if waited2 is not None:
                        entry_wait2, _ = _validated_snapshot_cache_entry_from_value(
                            waited2,
                            min_rev=int(rev_fast or 0),
                            cache_key=school_snap_key_fast,
                        )
                        if isinstance(entry_wait2, dict) and isinstance(entry_wait2.get("snap"), dict):
                            snap_wait2 = entry_wait2["snap"]
                            _metrics_incr("metrics:snapshot_cache:school_hit")
                            _obs_snapshot_cache(
                                logger=logger,
                                outcome="hit",
                                layer="school_wait",
                                school_id=int(school_id_fast),
                                rev=int(rev_fast) if rev_fast is not None else None,
                                day_key=str(day_key_fast or ""),
                                cache_key=school_snap_key_fast,
                            )
                            _metrics_log_maybe()
                            resp = HttpResponse(
                                _snapshot_entry_body_bytes(entry_wait2),
                                content_type="application/json; charset=utf-8",
                            )
                            if entry_wait2.get("etag"):
                                resp["ETag"] = f"\"{entry_wait2['etag']}\""
                            _apply_success_cache_headers(resp, snap_wait2)
                            return _finalize(
                                resp,
                                cache_status="HIT",
                                device_bound=True if is_snapshot_path else None,
                                school_id=int(school_id_fast),
                                rev=int(rev_fast) if rev_fast is not None else None,
                            )

                    try:
                        stale_key = _stale_snapshot_fallback_key(int(school_id_fast), day_key=day_key_fast)
                        stale_blob = cache.get(stale_key)
                    except Exception:
                        stale_blob = None

                    if stale_blob is not None:
                        entry_stale, _ = _validated_snapshot_cache_entry_from_value(
                            stale_blob,
                            cache_key=stale_key,
                            reject_past_wake_boundary=True,
                        )
                        if isinstance(entry_stale, dict) and isinstance(entry_stale.get("snap"), dict):
                            snap_stale = entry_stale["snap"]
                            _metrics_incr("metrics:snapshot_cache:stale_fallback")
                            _obs_snapshot_build(
                                logger=logger,
                                stage="end",
                                source="stale",
                                school_id=int(school_id_fast),
                                rev=int(rev_fast) if rev_fast is not None else None,
                                day_key=str(day_key_fast or ""),
                                duration_ms=0,
                                reason="fast_path_stale_fallback",
                            )
                            _metrics_log_maybe()
                            resp = HttpResponse(
                                _snapshot_entry_body_bytes(entry_stale),
                                content_type="application/json; charset=utf-8",
                            )
                            if entry_stale.get("etag"):
                                resp["ETag"] = f"\"{entry_stale['etag']}\""
                            _apply_success_cache_headers(resp, snap_stale)
                            return _finalize(
                                resp,
                                cache_status="STALE",
                                device_bound=True if is_snapshot_path else None,
                                school_id=int(school_id_fast),
                                rev=int(rev_fast) if rev_fast is not None else None,
                            )

                    resp = JsonResponse(
                        _fallback_building_payload(
                            school_id=int(school_id_fast),
                            rev=int(rev_fast or 0),
                            day_key=str(day_key_fast or ""),
                            reason="snapshot is being prepared",
                            refresh_interval_sec=3,
                        ),
                        json_dumps_params={"ensure_ascii": False},
                        status=200,
                    )
                    return _finalize(
                        resp,
                        cache_status="STALE",
                        device_bound=True if is_snapshot_path else None,
                        school_id=int(school_id_fast),
                        rev=int(rev_fast) if rev_fast is not None else None,
                    )

        settings_obj = None
        hashed_token = token_hash
        cached_school_id = None

        # 0) ✅ Cache Hit: Check if token is already mapped to school_id
        if token_value and len(token_value) > 10:
            # Negative cache check
            neg_key = f"display:token_neg:{hashed_token}"
            if cache.get(neg_key):
                resp = JsonResponse(
                    _fallback_payload("رمز الدخول غير صحيح (cached)"),
                    json_dumps_params={"ensure_ascii": False},
                    status=403,
                )
                resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
                return _finalize(resp, cache_status="ERROR", device_bound=True if is_snapshot_path else None)

            # Positive cache check
            map_key = f"display:token_map:{hashed_token}"
            cached_map = cache.get(map_key)
            if cached_map:
                if isinstance(cached_map, dict):
                    cached_school_id = cached_map.get("school_id")
                else:
                    # Legacy or simple integer fallback
                    try:
                        cached_school_id = int(cached_map)
                    except:
                        pass
        
        # 1) If we have school_id, try to fetch Snapshot directly from cache
        if cached_school_id and not force_nocache:
            # Bump cache version v1 -> v2 to invalidate old stuck "Off" states
            cached_rev = _get_schedule_revision_for_school_id(int(cached_school_id))
            # If we already have a rev-specific tenant cache entry, prefer it.
            # This is especially helpful during stampedes where we may have cached a short-lived
            # response under the tenant key but not yet populated the per-school key.
            try:
                tenant_key_early = get_cache_key_rev(token_hash, int(cached_school_id), int(cached_rev))
                cached_entry_early, _ = _validated_snapshot_cache_entry_from_value(
                    cache.get(tenant_key_early),
                    min_rev=int(cached_rev),
                    cache_key=tenant_key_early,
                )
                if isinstance(cached_entry_early, dict) and isinstance(cached_entry_early.get("snap"), dict) and isinstance(cached_entry_early.get("etag"), str):
                    _metrics_incr("metrics:snapshot_cache:token_hit")
                    _obs_snapshot_cache(
                        logger=logger,
                        outcome="hit",
                        layer="tenant_rev",
                        school_id=int(cached_school_id),
                        rev=int(cached_rev),
                        cache_key=tenant_key_early,
                    )
                    _metrics_log_maybe()
                    inm = _parse_if_none_match(request.headers.get("If-None-Match"))
                    if inm and inm == cached_entry_early.get("etag"):
                        resp = HttpResponseNotModified()
                        resp["ETag"] = f"\"{cached_entry_early['etag']}\""
                        _apply_success_cache_headers(resp, cached_entry_early.get("snap"))
                        return _finalize(resp, cache_status="HIT", device_bound=True if is_snapshot_path else None, school_id=int(cached_school_id), rev=int(cached_rev))
                    resp = JsonResponse(cached_entry_early["snap"], json_dumps_params={"ensure_ascii": False})
                    resp["ETag"] = f"\"{cached_entry_early['etag']}\""
                    _apply_success_cache_headers(resp, cached_entry_early.get("snap"))
                    return _finalize(resp, cache_status="HIT", device_bound=True if is_snapshot_path else None, school_id=int(cached_school_id), rev=int(cached_rev))
            except Exception:
                pass
            snap_key = _steady_cache_key_for_school_rev(int(cached_school_id), int(cached_rev))
            cached_entry, _ = _validated_snapshot_cache_entry_from_value(
                cache.get(snap_key),
                min_rev=int(cached_rev),
                cache_key=snap_key,
            )
            _log_steady_get(
                snap_key,
                hit=isinstance(cached_entry, dict) and isinstance(cached_entry.get("snap"), dict),
                school_id=int(cached_school_id),
                rev=int(cached_rev),
            )
            if isinstance(cached_entry, dict) and isinstance(cached_entry.get("snap"), dict):
                cached_snap = cached_entry["snap"]
                _metrics_incr("metrics:snapshot_cache:school_hit")
                _obs_snapshot_cache(
                    logger=logger,
                    outcome="hit",
                    layer="school",
                    school_id=int(cached_school_id),
                    rev=int(cached_rev),
                    cache_key=snap_key,
                )
                _metrics_log_maybe()
                # Serve directly from school-level cache.
                # IMPORTANT: do not duplicate full snapshot body into token-scoped keys.
                # At fleet scale (tens of thousands of screens), token-level body copies
                # multiply Redis memory usage without adding correctness guarantees.
                etag = cached_entry.get("etag")

                inm = _parse_if_none_match(request.headers.get("If-None-Match"))
                if etag and inm and inm == etag:
                    resp = HttpResponseNotModified()
                    resp["ETag"] = f"\"{etag}\""
                    _apply_success_cache_headers(resp, cached_snap)
                    return _finalize(resp, cache_status="HIT", device_bound=True if is_snapshot_path else None, school_id=int(cached_school_id), rev=int(cached_rev))

                resp = HttpResponse(
                    _snapshot_entry_body_bytes(cached_entry),
                    content_type="application/json; charset=utf-8",
                )
                if etag:
                    resp["ETag"] = f"\"{etag}\""
                _apply_success_cache_headers(resp, cached_snap)
                return _finalize(resp, cache_status="HIT", device_bound=True if is_snapshot_path else None, school_id=int(cached_school_id), rev=int(cached_rev))

            _metrics_incr("metrics:snapshot_cache:school_miss")
            _obs_snapshot_cache(
                logger=logger,
                outcome="miss",
                layer="school",
                school_id=int(cached_school_id),
                rev=int(cached_rev),
                cache_key=snap_key,
                reason="school_cache_miss",
            )
            _metrics_log_maybe()

            # If snapshot missing, we need settings_obj to build it
            try:
                settings_obj = _get_settings_by_school_id(int(cached_school_id))
            except Exception:
                pass

        # 2) DisplayScreen (DB Lookup if not found yet)
        if not settings_obj:
            settings_obj = _match_settings_via_display_screen(token_value) if token_value else None

        # 3) fallback token search
        if not settings_obj and token_value:
            settings_obj = _get_settings_by_token(token_value)

        # 4) school_id param
        if not settings_obj:
            school_id_raw = (request.GET.get("school_id") or request.GET.get("school") or "").strip()
            if school_id_raw.isdigit():
                settings_obj = _get_settings_by_school_id(int(school_id_raw))

        # 5) single settings fallback
        if not settings_obj:
            # ✅ Negative Cache: Cache invalid token to prevent DB hammering
            if token_value and len(token_value) > 10:
                neg_key = f"display:token_neg:{hashed_token}"
                cache.set(neg_key, "1", timeout=60) # 60 seconds

            total = SchoolSettings.objects.count()
            if total == 1:
                settings_obj = SchoolSettings.objects.select_related("school").first()
            else:
                if dj_settings.DEBUG:
                    logger.warning(
                        "snapshot: no match. token=%s school_id=%s total_settings=%s",
                        (token_value[:10] + "...") if token_value else None,
                        request.GET.get("school_id") or request.GET.get("school"),
                        total,
                    )
                resp = JsonResponse(_fallback_payload("إعدادات المدرسة غير مهيأة"), json_dumps_params={"ensure_ascii": False})
                resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
                return _finalize(resp, cache_status="ERROR", device_bound=True if is_snapshot_path else None, school_id=None, rev=None)
        
        # ✅ Cache Update: Store valid token mapping if not cached (24h)
        if token_value and settings_obj and len(token_value) > 10 and not cached_school_id:
            map_key = f"display:token_map:{hashed_token}"
            try:
                if getattr(settings_obj, 'school_id', None):
                   # Store dict compatible with middleware
                   payload = {"school_id": settings_obj.school_id}
                   # We don't have screen ID here easily unless we fetched via _match_settings_via_display_screen
                   # But middleware will update it with full details on next hit.
                   cache.set(map_key, payload, timeout=86400) # 24 hours
            except Exception:
                pass

        # 6) Phase 2: build snapshot once per school (stampede guard)
        school_id = int(getattr(settings_obj, "school_id", None) or 0)
        rev = int(getattr(settings_obj, "schedule_revision", 0) or 0)
        tenant_cache_key = get_cache_key_rev(token_hash, school_id, rev)
        if tenant_cache_key != cache_key:
            cached_entry2, _ = (
                (None, "missing")
                if force_nocache
                else _validated_snapshot_cache_entry_from_value(
                    cache.get(tenant_cache_key),
                    min_rev=int(rev),
                    cache_key=tenant_cache_key,
                )
            )

            if isinstance(cached_entry2, dict) and isinstance(cached_entry2.get("snap"), dict) and isinstance(cached_entry2.get("etag"), str):
                _metrics_incr("metrics:snapshot_cache:token_hit")
                _obs_snapshot_cache(
                    logger=logger,
                    outcome="hit",
                    layer="tenant_rev",
                    school_id=school_id,
                    rev=rev,
                    cache_key=tenant_cache_key,
                )
                _metrics_log_maybe()
                inm = _parse_if_none_match(request.headers.get("If-None-Match"))
                if inm and inm == cached_entry2.get("etag"):
                    resp = HttpResponseNotModified()
                    resp["ETag"] = f"\"{cached_entry2['etag']}\""
                    _apply_success_cache_headers(resp, cached_entry2.get("snap"))
                    return _finalize(resp, cache_status="HIT", device_bound=True if is_snapshot_path else None, school_id=school_id, rev=rev)
                resp = JsonResponse(cached_entry2["snap"], json_dumps_params={"ensure_ascii": False})
                resp["ETag"] = f"\"{cached_entry2['etag']}\""
                _apply_success_cache_headers(resp, cached_entry2.get("snap"))
                return _finalize(resp, cache_status="HIT", device_bound=True if is_snapshot_path else None, school_id=school_id, rev=rev)

        snap_key = _snapshot_cache_key(settings_obj)

        if not force_nocache:
            cached_entry, _ = _validated_snapshot_cache_entry_from_value(
                cache.get(snap_key),
                min_rev=int(rev),
                cache_key=snap_key,
            )
            _log_steady_get(
                snap_key,
                hit=isinstance(cached_entry, dict) and isinstance(cached_entry.get("snap"), dict),
                school_id=school_id,
                rev=rev,
            )
            if isinstance(cached_entry, dict) and isinstance(cached_entry.get("snap"), dict):
                cached_school = cached_entry["snap"]
                _metrics_incr("metrics:snapshot_cache:school_hit")
                _obs_snapshot_cache(
                    logger=logger,
                    outcome="hit",
                    layer="school",
                    school_id=school_id,
                    rev=rev,
                    cache_key=snap_key,
                )
                _metrics_log_maybe()
                etag = cached_entry.get("etag")

                inm = _parse_if_none_match(request.headers.get("If-None-Match"))
                if inm and inm == etag:
                    resp = HttpResponseNotModified()
                    resp["ETag"] = f"\"{etag}\""
                    _apply_success_cache_headers(resp, cached_school)
                    return _finalize(resp, cache_status="HIT", device_bound=True if is_snapshot_path else None, school_id=school_id, rev=rev)
                resp = HttpResponse(
                    _snapshot_entry_body_bytes(cached_entry),
                    content_type="application/json; charset=utf-8",
                )
                resp["ETag"] = f"\"{etag}\""
                _apply_success_cache_headers(resp, cached_school)
                return _finalize(resp, cache_status="HIT", device_bound=True if is_snapshot_path else None, school_id=school_id, rev=rev)

            _metrics_incr("metrics:snapshot_cache:school_miss")
            _obs_snapshot_cache(
                logger=logger,
                outcome="miss",
                layer="school",
                school_id=school_id,
                rev=rev,
                cache_key=snap_key,
                reason="school_cache_miss",
            )
            _metrics_log_maybe()

        try:
            def _build_snapshot_for_school() -> dict:
                snap, build_ms = _build_snapshot_payload(
                    request,
                    settings_obj,
                    school_id=school_id,
                    rev=rev,
                )

                json_bytes = _stable_json_bytes(snap)

                try:
                    logger.info(
                        "snapshot_build school_id=%s rev=%s day_key=%s size_bytes=%s build_ms=%s",
                        int(school_id),
                        int(rev),
                        _snapshot_cache_day_key(timezone.localdate().isoformat()),
                        int(len(json_bytes)),
                        int(build_ms),
                    )
                except Exception:
                    pass

                return snap

            if force_nocache:
                entry = _snapshot_cache_entry(_build_snapshot_for_school())
                cache_status = "BYPASS"
            else:
                entry, cache_status = get_or_build_snapshot(
                    school_id,
                    rev,
                    _build_snapshot_for_school,
                    day_key=timezone.localdate().isoformat(),
                )

            snap = entry["snap"]

            is_stale = bool(
                ((snap.get("meta") or {}) if isinstance(snap, dict) else {}).get("is_stale")
            )

            if is_stale or cache_status == "STALE":
                _metrics_incr("metrics:snapshot_cache:stale_fallback")
                _obs_snapshot_build(
                    logger=logger,
                    stage="end",
                    source="stale",
                    school_id=school_id,
                    rev=rev,
                    day_key=_snapshot_cache_day_key(timezone.localdate().isoformat()),
                    duration_ms=0,
                    reason="served_stale_snapshot",
                )
                _metrics_log_maybe()

            etag = entry.get("etag")

            inm = _parse_if_none_match(request.headers.get("If-None-Match"))
            if inm and inm == etag:
                resp = HttpResponseNotModified()
                resp["ETag"] = f"\"{etag}\""
                _apply_success_cache_headers(resp, snap)
                return _finalize(
                    resp,
                    cache_status=cache_status,
                    device_bound=True if is_snapshot_path else None,
                    school_id=school_id,
                    rev=rev,
                )

            resp = HttpResponse(
                _snapshot_entry_body_bytes(entry),
                content_type="application/json; charset=utf-8",
            )
            resp["ETag"] = f"\"{etag}\""
            _apply_success_cache_headers(resp, snap)
            return _finalize(
                resp,
                cache_status=cache_status,
                device_bound=True if is_snapshot_path else None,
                school_id=school_id,
                rev=rev,
            )
        finally:
            if school_lock_key_fast and not force_nocache:
                try:
                    cache.delete(school_lock_key_fast)
                except Exception:
                    pass

    except Exception as e:
        logger.exception("snapshot error: %s", e)
        resp = JsonResponse(_fallback_payload("حدث خطأ أثناء جلب البيانات"), json_dumps_params={"ensure_ascii": False})
        resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"; resp["Pragma"] = "no-cache"; resp["Expires"] = "0"
        resp["Vary"] = "Accept-Encoding"
        resp["X-Snapshot-Cache"] = "ERROR"
        if app_rev:
            resp["X-App-Revision"] = app_rev
        return resp


