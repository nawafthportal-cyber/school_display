import logging
import os

from django.apps import AppConfig
from django.conf import settings
from django.core.cache import caches

from core.redis_topology import redis_topology_summary

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        """
        تهيئة تطبيق core.
        """
        # Register authentication signals even when startup diagnostics are
        # disabled.  The import is intentionally local to Django's ready hook.
        from core import session_security  # noqa: F401
        from core import deployment_checks  # noqa: F401

        # Keep this log OFF by default in dev; in production it can be useful.
        # Prints once per process/worker.
        try:
            enabled = (not bool(getattr(settings, "DEBUG", False))) or (
                (os.getenv("LOG_CACHE_BACKEND_ON_START", "").strip() or "0") in {"1", "true", "yes", "on"}
            )
        except Exception:
            enabled = False

        if not enabled:
            return

        try:
            default_cfg = dict(getattr(settings, "CACHES", {}).get("default", {}) or {})
        except Exception:
            default_cfg = {}

        def _redact_location(value: object) -> object:
            if not isinstance(value, str):
                return value
            v = value.strip()
            if not v:
                return v
            # Best-effort redact password in redis URLs.
            # Examples:
            #   redis://:password@host:6379/0
            #   rediss://:password@host:6379/0
            try:
                if "@" in v and "://" in v:
                    scheme, rest = v.split("://", 1)
                    if rest.startswith(":") and "@" in rest:
                        _cred, hostpart = rest.split("@", 1)
                        return f"{scheme}://:***@{hostpart}"
            except Exception:
                pass
            return v

        try:
            if "LOCATION" in default_cfg:
                default_cfg["LOCATION"] = _redact_location(default_cfg.get("LOCATION"))
        except Exception:
            pass

        try:
            backend = caches["default"]
            backend_name = f"{backend.__class__.__module__}.{backend.__class__.__name__}"
        except Exception:
            backend_name = "unknown"

        try:
            logger.info(
                "cache_backend backend=%s redis_url_configured=%s caches_default=%s",
                backend_name,
                bool(
                    os.getenv("REDIS_CACHE_URL", "").strip()
                    or os.getenv("REDIS_CHANNELS_URL", "").strip()
                    or os.getenv("CACHE_REDIS_URL", "").strip()
                    or os.getenv("CHANNELS_REDIS_URL", "").strip()
                    or os.getenv("CHANNEL_REDIS_URL", "").strip()
                    or os.getenv("REDIS_URL", "").strip()
                ),
                default_cfg,
            )
        except Exception:
            # Never fail app startup due to logging.
            pass

        try:
            topology = redis_topology_summary()
            if topology.get("split"):
                logger.info(
                    "redis_topology split=1 cache=%s channels=%s",
                    topology.get("cache_url_redacted"),
                    topology.get("channels_url_redacted"),
                )
            elif topology.get("shared"):
                level = logger.error if bool(getattr(settings, "DISPLAY_REQUIRE_REDIS_SPLIT", False)) else logger.warning
                level(
                    "redis_topology split=0 warning=shared cache=%s channels=%s",
                    topology.get("cache_url_redacted"),
                    topology.get("channels_url_redacted"),
                )
            else:
                logger.warning(
                    "redis_topology split=0 warning=incomplete cache=%s channels=%s",
                    topology.get("cache_url_redacted"),
                    topology.get("channels_url_redacted"),
                )
        except Exception:
            pass
