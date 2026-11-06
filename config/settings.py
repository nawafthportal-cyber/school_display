# config/settings.py
from __future__ import annotations

import os
import sys
import socket
import logging
from pathlib import Path

import dj_database_url

logger = logging.getLogger(__name__)

# تحميل .env لو موجود (محليًا)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


# =========================
# Helpers
# =========================
def env_bool(key: str, default: str = "False") -> bool:
    return os.getenv(key, default).strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: str) -> int:
    try:
        return int(os.getenv(key, default))
    except Exception:
        return int(default)


def env_list(key: str, default: str = "") -> list[str]:
    raw = os.getenv(key, default).strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_secret(key: str, file_key: str) -> str:
    """Read a secret from the environment or an externally mounted file."""
    value = os.getenv(key, "").strip()
    if value:
        return value

    file_path = os.getenv(file_key, "").strip()
    if not file_path:
        return ""
    try:
        return Path(file_path).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def exists_dir(p: Path) -> bool:
    try:
        return p.exists() and p.is_dir()
    except Exception:
        return False


def env_int_clamped(name: str, default: int, min_v: int, max_v: int) -> int:
    try:
        v = int(os.getenv(name, str(default)).strip())
    except Exception:
        v = int(default)
    return max(int(min_v), min(int(max_v), int(v)))


def env_float_clamped(name: str, default: float, min_v: float, max_v: float) -> float:
    try:
        v = float(os.getenv(name, str(default)).strip())
    except Exception:
        v = float(default)
    return max(float(min_v), min(float(max_v), float(v)))


# =========================
# Base
# =========================
BASE_DIR = Path(__file__).resolve().parent.parent

# مهم: خلي DEBUG True افتراضيًا محليًا لتفادي مشاكل SSL/Redirect إذا ما عندك .env
DEBUG = env_bool("DEBUG", "True")

# Unified test mode detection (Django test runner + pytest)
RUNNING_TESTS = bool(
    os.getenv("PYTEST_CURRENT_TEST")
    or "pytest" in sys.modules
    or any(arg in {"test", "pytest"} for arg in sys.argv)
)

# Optional: enable noisy middleware debug prints (default off)
MIDDLEWARE_DEBUG = env_bool("MIDDLEWARE_DEBUG", "False")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-key-change-me")
if not DEBUG and (not SECRET_KEY or SECRET_KEY == "dev-insecure-key-change-me"):
    raise RuntimeError("SECRET_KEY must be set in production!")


# =========================
# Build / Revision
# =========================
APP_REVISION = (
    os.getenv("APP_REVISION")
    or os.getenv("GIT_COMMIT")
    or os.getenv("SOURCE_VERSION")
    or ""
).strip()


# =========================
# Snapshot cache TTL (seconds)
# Used by schedule.api_views.snapshot server-side caching.
# This is origin-side/app-side caching, NOT Cloudflare API caching.
# =========================
DISPLAY_SNAPSHOT_CACHE_TTL = env_int_clamped(
    "DISPLAY_SNAPSHOT_CACHE_TTL",
    900,
    5,
    900,
)


# =========================
# Feature Flags: Realtime WebSocket Push
# =========================
# Push-first architecture:
# - WS enabled by default
# - dashboard changes are pushed via WS
# - time-based transitions handled client-side where possible
DISPLAY_WS_ENABLED = env_bool("DISPLAY_WS_ENABLED", "True")

# Allow multiple devices per screen token (HTTP + WS must respect this)
DISPLAY_ALLOW_MULTI_DEVICE = env_bool("DISPLAY_ALLOW_MULTI_DEVICE", "False")

# =========================
# Display bundle delivery
# =========================
# The minified bundle is ~54% smaller to parse, which matters most on weak TV
# SoCs. Set to False to serve the readable bundle when debugging a screen.
DISPLAY_USE_MINIFIED_JS = env_bool("DISPLAY_USE_MINIFIED_JS", "True")


# =========================
# Screen add-on pricing (SAR)
# =========================
# Price per extra screen for one month. Longer cycles multiply this figure, so
# a pricing change is a config change rather than a code deploy.
SCREEN_ADDON_MONTHLY_PRICE = env_int_clamped("SCREEN_ADDON_MONTHLY_PRICE", 60, 0, 100000)
# Semiannual bills 6 months; annual bills 10 (two months free).
SCREEN_ADDON_SEMIANNUAL_MULTIPLIER = env_int_clamped("SCREEN_ADDON_SEMIANNUAL_MULTIPLIER", 6, 1, 12)
SCREEN_ADDON_ANNUAL_MULTIPLIER = env_int_clamped("SCREEN_ADDON_ANNUAL_MULTIPLIER", 10, 1, 24)

# Commercial gate: screens stop serving content once a school's subscription
# lapses. Disable only for diagnostics — leaving it off gives away the product.
DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION = env_bool("DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION", "True")

# How long a subscription access answer stays cached (seconds). Subscription
# changes invalidate the entry explicitly, so this is only a safety ceiling.
SUBSCRIPTION_ACCESS_CACHE_TTL = env_int_clamped("SUBSCRIPTION_ACCESS_CACHE_TTL", 300, 30, 3600)

# Mobile-first TV pairing. Codes are one-time and deliberately short-lived.
DISPLAY_PAIRING_TTL_SEC = env_int_clamped("DISPLAY_PAIRING_TTL_SEC", 600, 180, 1800)
DISPLAY_PAIRING_START_LIMIT = env_int_clamped("DISPLAY_PAIRING_START_LIMIT", 20, 3, 100)
DISPLAY_PAIRING_CODE_ATTEMPTS = env_int_clamped("DISPLAY_PAIRING_CODE_ATTEMPTS", 8, 3, 30)

# A healthy WebSocket remains the primary update path. This sparse HTTP check
# is only a safety net for missed cross-process events.
#
# It is deliberately hourly. A socket that dies silently is caught in 75 seconds
# by the client's own heartbeat watchdog (it stops seeing the server ping and
# drops to fallback polling), and the screen reconciles once right after every
# reconnect — so this check is the third line of defence, not the first. At one
# check per minute it was the single largest source of display traffic: roughly
# 420 requests per screen per school day, all of them answering "nothing
# changed". Screens running an older bundle clamp this to their own 300s
# ceiling, which keeps a mixed-version fleet safe during a rollout.
DISPLAY_WS_LIVE_STATUS_CHECK_SEC = env_int_clamped(
    "DISPLAY_WS_LIVE_STATUS_CHECK_SEC",
    3600,
    15,
    3600,
)

# Display presence shown in the manager dashboard. WebSocket/HTTP heartbeats
# refresh shared cache continuously while DB writes are throttled.
DASHBOARD_SCREEN_LIVE_THRESHOLD_SEC = env_int_clamped(
    "DASHBOARD_SCREEN_LIVE_THRESHOLD_SEC",
    120,
    60,
    86400,
)

# How often a connected screen refreshes the shared presence cache. The
# dashboard calls a screen live within DASHBOARD_SCREEN_LIVE_THRESHOLD_SEC
# (120s), so touching it once a minute keeps that indicator exact while cutting
# the work the WebSocket ping loop used to do on every single ping (every 20s).
DISPLAY_PRESENCE_TOUCH_INTERVAL_SEC = env_int_clamped(
    "DISPLAY_PRESENCE_TOUCH_INTERVAL_SEC",
    60,
    20,
    300,
)

# Presence survives a cache restart through this periodic DB write — the single
# heaviest write in the system, one row per connected screen per minute, around
# the clock.
#
# The ceiling is not a preference. When the cache is empty the monitor falls
# back to this column, and a school may set its offline threshold as low as five
# minutes: a value at or above that would make every screen in such a school
# look dead for the moment after a Redis restart. Four minutes keeps a clear
# margin under the floor while still cutting the write rate four-fold.
# core.display_presence enforces the same ceiling.
DISPLAY_LAST_SEEN_DB_INTERVAL_SEC = env_int_clamped(
    "DISPLAY_LAST_SEEN_DB_INTERVAL_SEC",
    240,
    30,
    240,
)

# Operational history kept before pruning: resolved screen outages and delivered
# alerts. Both are append-only logs that nothing reads after the fact, and
# neither had any retention policy at all.
DISPLAY_OPERATIONAL_RETENTION_DAYS = env_int_clamped(
    "DISPLAY_OPERATIONAL_RETENTION_DAYS",
    180,
    30,
    1095,
)


# =========================
# Origin snapshot TTL (seconds)
# Used by schedule.api_views.snapshot origin-only caching / ETag semantics.
# This is not Cloudflare edge caching because /api/ is bypassed in CF rules.
# =========================
DISPLAY_SNAPSHOT_TTL = env_int_clamped(
    "DISPLAY_SNAPSHOT_TTL",
    DISPLAY_SNAPSHOT_CACHE_TTL,
    1,
    60,
)


# =========================
# Phase 2: Dynamic Snapshot TTLs
# =========================
# Active window school-snapshot TTL.
#
# لماذا رفعنا الافتراضي؟
# - على أسطول شاشات كبير، poll كل ~20s مع TTL قصير جدًا يؤدي إلى cache-miss متكرر.
# - الواجهة تعتمد على X-Server-Time-MS والهندسة الزمنية، لذلك يمكننا كاش الجسم مدة أطول بأمان.
DISPLAY_SNAPSHOT_ACTIVE_TTL = env_int_clamped(
    "DISPLAY_SNAPSHOT_ACTIVE_TTL",
    30,
    15,
    60,
)

# Add TTL jitter to reduce thundering herd at fleet scale (seconds)
DISPLAY_SNAPSHOT_TTL_JITTER_SEC = env_int_clamped(
    "DISPLAY_SNAPSHOT_TTL_JITTER_SEC",
    30,
    0,
    300,
)

# Enforce a warm origin cache floor for school/day snapshot artifacts.
DISPLAY_SNAPSHOT_TTL_MIN_SECONDS = env_int_clamped(
    "DISPLAY_SNAPSHOT_TTL_MIN_SECONDS",
    300,
    300,
    3600,
)

DISPLAY_SNAPSHOT_ACTIVE_TTL_MAX = env_int_clamped(
    "DISPLAY_SNAPSHOT_ACTIVE_TTL_MAX",
    60,
    15,
    60,
)

# Clamp active TTL to safe bounds
DISPLAY_SNAPSHOT_ACTIVE_TTL = max(
    15,
    min(DISPLAY_SNAPSHOT_ACTIVE_TTL_MAX, DISPLAY_SNAPSHOT_ACTIVE_TTL),
)

# Safe rollout control
SNAPSHOT_STEADY_CACHE_V2 = env_bool("SNAPSHOT_STEADY_CACHE_V2", "False")

DISPLAY_SNAPSHOT_ACTIVE_TTL_SAFE_MIN = env_int_clamped(
    "DISPLAY_SNAPSHOT_ACTIVE_TTL_SAFE_MIN",
    30,
    15,
    60,
)

# Outside active window: steady snapshot TTL cap (1h–24h)
DISPLAY_SNAPSHOT_STEADY_MAX_TTL = env_int_clamped(
    "DISPLAY_SNAPSHOT_STEADY_MAX_TTL",
    86400,
    3600,
    86400,
)

# Throttled cache metrics log interval (seconds)
DISPLAY_SNAPSHOT_CACHE_METRICS_INTERVAL_SEC = env_int_clamped(
    "DISPLAY_SNAPSHOT_CACHE_METRICS_INTERVAL_SEC",
    600,
    60,
    3600,
)


# =========================
# Status polling log throttles (seconds)
# Used by schedule.api_views.status to avoid log storms at scale.
# =========================
DISPLAY_STATUS_LOG_INTERVAL_SEC = env_int_clamped(
    "DISPLAY_STATUS_LOG_INTERVAL_SEC",
    300,
    30,
    3600,
)

DISPLAY_STATUS_200_LOG_INTERVAL_SEC = env_int_clamped(
    "DISPLAY_STATUS_200_LOG_INTERVAL_SEC",
    120,
    10,
    3600,
)

DISPLAY_STATUS_WARN_LOG_INTERVAL_SEC = env_int_clamped(
    "DISPLAY_STATUS_WARN_LOG_INTERVAL_SEC",
    300,
    30,
    3600,
)


# =========================
# Status polling metrics (best-effort; cache-only)
# Used to confirm /api/display/status is cache-only at scale.
# =========================
DISPLAY_STATUS_METRICS_ENABLED = env_bool("DISPLAY_STATUS_METRICS_ENABLED", "False")
DISPLAY_STATUS_METRICS_SAMPLE_EVERY = env_int_clamped(
    "DISPLAY_STATUS_METRICS_SAMPLE_EVERY",
    50,
    1,
    1000,
)
DISPLAY_STATUS_METRICS_KEY_TTL = env_int_clamped(
    "DISPLAY_STATUS_METRICS_KEY_TTL",
    86400,
    60,
    86400 * 14,
)


# =========================
# Snapshot edge/browser max-age (diagnostic/header only)
# IMPORTANT:
# Cloudflare currently bypasses /api/* via Cache Rules,
# so this does NOT enable Cloudflare edge caching for snapshot endpoints.
# Keep only if used by middleware/headers for client or diagnostics.
# =========================
DISPLAY_SNAPSHOT_EDGE_MAX_AGE = env_int_clamped(
    "DISPLAY_SNAPSHOT_EDGE_MAX_AGE",
    10,
    1,
    60,
)


# =========================
# School-level shared snapshot cache
# Shared between tokens/devices for the same school/revision.
# This is the real scaling lever for many screens.
# =========================
SCHOOL_SNAPSHOT_TTL = env_int_clamped(
    "SCHOOL_SNAPSHOT_TTL",
    1200,   # 20 minutes
    60,
    3600,
)

SCHOOL_SNAPSHOT_LOCK_TTL = env_int_clamped(
    "SCHOOL_SNAPSHOT_LOCK_TTL",
    8,
    3,
    30,
)

SCHOOL_SNAPSHOT_WAIT_TIMEOUT = env_float_clamped(
    "SCHOOL_SNAPSHOT_WAIT_TIMEOUT",
    0.7,
    0.1,
    2.0,
)


# =========================
# Hosts / CSRF
# =========================
_default_allowed_hosts = [
    "school-display.com",
    "www.school-display.com",
    ".school-display.com",
    "localhost",
    "127.0.0.1",
]
_env_hosts = env_list("ALLOWED_HOSTS", "")
ALLOWED_HOSTS: list[str] = []
for _h in [*_default_allowed_hosts, *_env_hosts]:
    if _h and _h not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_h)

if RUNNING_TESTS:
    for _h in ("testserver", "localhost", "127.0.0.1"):
        if _h not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(_h)

# CSRF_TRUSTED_ORIGINS يجب أن تحتوي scheme
_csrf_env = env_list("CSRF_TRUSTED_ORIGINS", "")
if _csrf_env:
    CSRF_TRUSTED_ORIGINS = _csrf_env
else:
    if DEBUG:
        CSRF_TRUSTED_ORIGINS = [
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ]
    else:
        CSRF_TRUSTED_ORIGINS = [
            "https://school-display.com",
            "https://www.school-display.com",
        ]


# =========================
# Apps
# =========================
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Thousands separators for the money figures in the admin console.
    "django.contrib.humanize",

    # Media
    "cloudinary_storage",
    "cloudinary",

    # REST
    "rest_framework",

    # WebSocket support
    "channels",

    # Project apps
    "core.apps.CoreConfig",
    "schedule.apps.ScheduleConfig",
    "standby",
    "notices.apps.NoticesConfig",
    "website",
    "dashboard",
    "subscriptions.apps.SubscriptionsConfig",
    "mailcenter.apps.MailcenterConfig",
    "telegram_alerts.apps.TelegramAlertsConfig",
]


# =========================
# Middleware
# =========================
MIDDLEWARE = [
    # ✅ CRITICAL FOR TV PERFORMANCE: Gzip compression FIRST
    # Reduces display.js and static payload sizes significantly
    "django.middleware.gzip.GZipMiddleware",

    "django.middleware.security.SecurityMiddleware",

    # Hard redirect legacy favicon path before WhiteNoise.
    "core.middleware.StaticFaviconRedirectMiddleware",

    # Snapshot headers only. Does NOT imply Cloudflare API caching when /api/ is bypassed.
    # Placing before WhiteNoise ensures proper response-order behavior.
    "core.middleware.SnapshotEdgeCacheMiddleware",

    # Diagnostics: detect which endpoint actually produces StreamingHttpResponse under ASGI.
    "core.middleware.StreamingResponseProbeMiddleware",

    # Serve /static/* without streaming under ASGI; WhiteNoise remains a fallback.
    "core.middleware.DirectStaticAssetMiddleware",

    "whitenoise.middleware.WhiteNoiseMiddleware",

    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",

    # The newest successful login owns the account. Older browser sessions are
    # rejected before any dashboard or API view can run.
    "core.middleware.SingleSessionMiddleware",

    # Project middleware
    "dashboard.middleware.TwoFactorRequiredMiddleware",
    "core.middleware.ActiveSchoolMiddleware",
    "dashboard.middleware.SubscriptionRequiredMiddleware",
    "dashboard.middleware.SupportDashboardOnlyMiddleware",
    "core.middleware.DisplayTokenMiddleware",

    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
]

# CSP is enforced: every inline event handler has been migrated to delegated
# listeners in static/js/csp-actions.js, and the remaining inline <script>
# blocks carry nonce="{{ csp_nonce }}". Flip REPORT_ONLY back to True if a
# violation surfaces in production; reports keep flowing either way.
CONTENT_SECURITY_POLICY_ENABLED = env_bool("CONTENT_SECURITY_POLICY_ENABLED", "True")
CONTENT_SECURITY_POLICY_REPORT_ONLY = env_bool("CONTENT_SECURITY_POLICY_REPORT_ONLY", "False")
CONTENT_SECURITY_POLICY_REPORT_URI = os.getenv("CONTENT_SECURITY_POLICY_REPORT_URI", "/csp-report/").strip()
CSP_REPORT_LOG_LIMIT_PER_MINUTE = env_int_clamped("CSP_REPORT_LOG_LIMIT_PER_MINUTE", 5, 1, 60)


# =========================
# URLs / WSGI / ASGI
# =========================
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# =========================
# Templates
# =========================
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.csp_nonce",
                "dashboard.context_processors.admin_support_ticket_badges",
                "dashboard.context_processors.school_whatsapp_contact",
            ],
        },
    },
]


# =========================
# Database
# =========================
DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=env_int("CONN_MAX_AGE", "600"),
    )
}

try:
    engine = DATABASES["default"].get("ENGINE", "")
    is_postgres = "postgres" in engine
except Exception:
    is_postgres = False

if is_postgres and not DEBUG:
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"].setdefault(
        "sslmode",
        os.getenv("PGSSLMODE", "require"),
    )


# =========================
# Cache / Channels Redis
# =========================
REDIS_URL = os.getenv("REDIS_URL", "").strip()
REDIS_CACHE_URL = (
    os.getenv("REDIS_CACHE_URL", "").strip()
    or os.getenv("CACHE_REDIS_URL", "").strip()
    or REDIS_URL
)
REDIS_CHANNELS_URL = (
    os.getenv("REDIS_CHANNELS_URL", "").strip()
    or os.getenv("CHANNELS_REDIS_URL", "").strip()
    or os.getenv("CHANNEL_REDIS_URL", "").strip()
    or REDIS_URL
)

REDIS_CONFIGURED = bool(REDIS_CACHE_URL and REDIS_CHANNELS_URL)
if not REDIS_CONFIGURED and not (DEBUG or RUNNING_TESTS):
    raise ValueError("Redis URLs are not configured properly")

# Backward-compatible aliases used by existing health checks and diagnostics.
CACHE_REDIS_URL = REDIS_CACHE_URL
CHANNELS_REDIS_URL = REDIS_CHANNELS_URL

if RUNNING_TESTS:
    logger.info("test_runtime cache=locmem channels=in-memory")
elif REDIS_CONFIGURED:
    logger.info(
        "redis_config configured=True split=%s",
        REDIS_CACHE_URL != REDIS_CHANNELS_URL,
    )
else:
    logger.warning("Redis URLs are not configured; using local in-memory cache/channel layer for development.")

CACHE_REDIS_MAX_CONNECTIONS = env_int(
    "CACHE_REDIS_MAX_CONNECTIONS",
    os.getenv("REDIS_MAX_CONNECTIONS", "50"),
)

# Default cache TTL as a safety net (seconds)
DEFAULT_CACHE_TIMEOUT = env_int("CACHE_DEFAULT_TIMEOUT", str(60 * 30))  # 30 minutes

if RUNNING_TESTS:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "school-display-tests",
            "TIMEOUT": DEFAULT_CACHE_TIMEOUT,
            "KEY_PREFIX": "school_display_tests",
        }
    }
elif REDIS_CONFIGURED:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_CACHE_URL,
            "TIMEOUT": DEFAULT_CACHE_TIMEOUT,
            "KEY_PREFIX": os.getenv("CACHE_KEY_PREFIX", "school_display"),
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "SOCKET_CONNECT_TIMEOUT": env_int("REDIS_CONNECT_TIMEOUT", "2"),
                "SOCKET_TIMEOUT": env_int("REDIS_SOCKET_TIMEOUT", "2"),
                "CONNECTION_POOL_KWARGS": {
                    "max_connections": CACHE_REDIS_MAX_CONNECTIONS,
                    "retry_on_timeout": True,
                    "health_check_interval": env_int("REDIS_HEALTHCHECK_INTERVAL", "30"),
                    "socket_keepalive": True,
                    "socket_keepalive_options": {
                        socket.TCP_KEEPIDLE: 60,
                        socket.TCP_KEEPINTVL: 10,
                        socket.TCP_KEEPCNT: 3,
                    },
                },
            },
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "school-display-dev",
            "TIMEOUT": DEFAULT_CACHE_TIMEOUT,
            "KEY_PREFIX": os.getenv("CACHE_KEY_PREFIX", "school_display"),
        }
    }


# =========================
# Channels Layer (WebSocket)
# =========================
if RUNNING_TESTS:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        },
    }
elif REDIS_CONFIGURED:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [REDIS_CHANNELS_URL],
                # Capacity: max messages per channel before blocking
                "capacity": env_int("WS_CHANNEL_CAPACITY", "2000"),
                # Expiry: messages auto-delete after N seconds
                "expiry": env_int("WS_MESSAGE_EXPIRY", "60"),
                # Group entries expire even while the socket itself stays open.
                # Consumers renew membership periodically before this deadline.
                "group_expiry": env_int("WS_GROUP_EXPIRY", "86400"),
                # Optional encryption:
                # "symmetric_encryption_keys": [SECRET_KEY[:32]],
            },
        },
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        },
    }

# WebSocket scaling knobs
WS_MAX_CONNECTIONS_PER_INSTANCE = env_int("WS_MAX_CONNECTIONS", "2000")
WS_PING_INTERVAL_SECONDS = env_int("WS_PING_INTERVAL", "20")
WS_GROUP_REFRESH_INTERVAL_SECONDS = env_int("WS_GROUP_REFRESH_INTERVAL_SECONDS", str(6 * 60 * 60))
WS_METRICS_LOG_INTERVAL = env_int("WS_METRICS_LOG_INTERVAL", "300")  # 5 minutes

# Login brute-force protection (shared cache in production).
LOGIN_RATE_LIMIT_WINDOW_SECONDS = env_int_clamped("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 900, 60, 86400)
LOGIN_RATE_LIMIT_ACCOUNT_ATTEMPTS = env_int_clamped("LOGIN_RATE_LIMIT_ACCOUNT_ATTEMPTS", 8, 1, 100)
LOGIN_RATE_LIMIT_IP_ATTEMPTS = env_int_clamped("LOGIN_RATE_LIMIT_IP_ATTEMPTS", 30, 1, 1000)

# Mandatory TOTP for system administrators and Support group members.
TWO_FACTOR_REQUIRED_FOR_PRIVILEGED = env_bool("TWO_FACTOR_REQUIRED_FOR_PRIVILEGED", "True")
TWO_FACTOR_ISSUER = os.getenv("TWO_FACTOR_ISSUER", "School Display").strip() or "School Display"
TWO_FACTOR_ENCRYPTION_KEY = os.getenv("TWO_FACTOR_ENCRYPTION_KEY", "").strip()
TWO_FACTOR_CHALLENGE_TTL_SECONDS = env_int_clamped("TWO_FACTOR_CHALLENGE_TTL_SECONDS", 300, 60, 1800)
TWO_FACTOR_MAX_ATTEMPTS = env_int_clamped("TWO_FACTOR_MAX_ATTEMPTS", 8, 3, 20)

# Snapshot materialization / async build
DISPLAY_SNAPSHOT_ASYNC_BUILD = env_bool("DISPLAY_SNAPSHOT_ASYNC_BUILD", "True")
DISPLAY_SNAPSHOT_INLINE_FALLBACK = env_bool("DISPLAY_SNAPSHOT_INLINE_FALLBACK", "True")
DISPLAY_SNAPSHOT_QUEUE_WAIT_TIMEOUT = env_float_clamped("DISPLAY_SNAPSHOT_QUEUE_WAIT_TIMEOUT", 0.35, 0.0, 2.0)
DISPLAY_SNAPSHOT_JOB_DEDUPE_TTL = env_int_clamped("DISPLAY_SNAPSHOT_JOB_DEDUPE_TTL", 30, 5, 300)
DISPLAY_SNAPSHOT_REBUILD_DEBOUNCE_SEC = env_int_clamped("DISPLAY_SNAPSHOT_REBUILD_DEBOUNCE_SEC", 3, 2, 5)
DISPLAY_SNAPSHOT_DEBOUNCE_SEC = env_int_clamped("DISPLAY_SNAPSHOT_DEBOUNCE_SEC", DISPLAY_SNAPSHOT_REBUILD_DEBOUNCE_SEC, 0, 60)
DISPLAY_SNAPSHOT_PENDING_TTL_SEC = env_int_clamped("DISPLAY_SNAPSHOT_PENDING_TTL_SEC", DISPLAY_SNAPSHOT_JOB_DEDUPE_TTL, 5, 600)
DISPLAY_SNAPSHOT_LATEST_REV_TTL_SEC = env_int_clamped("DISPLAY_SNAPSHOT_LATEST_REV_TTL_SEC", 120, 30, 3600)
DISPLAY_SNAPSHOT_REQUIRE_WORKER_ALIVE = env_bool("DISPLAY_SNAPSHOT_REQUIRE_WORKER_ALIVE", "True")
DISPLAY_SNAPSHOT_WORKER_HEARTBEAT_TTL = env_int_clamped("DISPLAY_SNAPSHOT_WORKER_HEARTBEAT_TTL", 45, 10, 300)
DISPLAY_START_EMBEDDED_SNAPSHOT_WORKER = env_bool("DISPLAY_START_EMBEDDED_SNAPSHOT_WORKER", "True")

# Runtime topology validation
DISPLAY_REQUIRE_REDIS_SPLIT = env_bool("DISPLAY_REQUIRE_REDIS_SPLIT", "False")

# Cluster-wide WS metrics windows
WS_CLUSTER_ACTIVE_TTL = env_int_clamped("WS_CLUSTER_ACTIVE_TTL", 120, 30, 900)
WS_CLUSTER_EVENT_RETENTION = env_int_clamped("WS_CLUSTER_EVENT_RETENTION", 3600, 300, 86400)


# =========================
# Password validators
# =========================
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# =========================
# Locale
# =========================
LANGUAGE_CODE = "ar"
TIME_ZONE = "Asia/Riyadh"
USE_I18N = True
USE_TZ = True


# =========================
# Sessions
# =========================
SESSION_COOKIE_AGE = env_int("SESSION_IDLE_TIMEOUT_SECONDS", "3600")
SESSION_SAVE_EVERY_REQUEST = False
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_FAILURE_VIEW = "core.csrf_views.csrf_failure"


# =========================
# Static / Media
# =========================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

_static_dir = BASE_DIR / "static"
STATICFILES_DIRS = [_static_dir] if exists_dir(_static_dir) else []

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

USE_CLOUD_STORAGE = (
    (not DEBUG)
    and bool(CLOUDINARY_CLOUD_NAME)
    and bool(CLOUDINARY_API_KEY)
    and bool(CLOUDINARY_API_SECRET)
)

if USE_CLOUD_STORAGE:
    CLOUDINARY_STORAGE = {
        "CLOUD_NAME": CLOUDINARY_CLOUD_NAME,
        "API_KEY": CLOUDINARY_API_KEY,
        "API_SECRET": CLOUDINARY_API_SECRET,
        "TRANSFORMATION": {
            "quality": "auto:good",
            "fetch_format": "auto",
        },
    }

STORAGES = {
    "default": {
        "BACKEND": (
            "cloudinary_storage.storage.MediaCloudinaryStorage"
            if USE_CLOUD_STORAGE
            else "django.core.files.storage.FileSystemStorage"
        ),
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

WHITENOISE_AUTOREFRESH = DEBUG
WHITENOISE_MANIFEST_STRICT = env_bool("WHITENOISE_MANIFEST_STRICT", "True")

if RUNNING_TESTS:
    WHITENOISE_MANIFEST_STRICT = False
    try:
        STORAGES["staticfiles"]["BACKEND"] = "django.contrib.staticfiles.storage.StaticFilesStorage"
    except Exception:
        pass


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# =========================
# DRF
# =========================
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "120/minute",
    },
    "DEFAULT_RENDERER_CLASSES": (
        ("rest_framework.renderers.JSONRenderer",)
        if not DEBUG
        else (
            "rest_framework.renderers.JSONRenderer",
            "rest_framework.renderers.BrowsableAPIRenderer",
        )
    ),
}


# =========================
# Security / Proxy
# =========================
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

SECURE_SSL_REDIRECT = False if (DEBUG or RUNNING_TESTS) else env_bool("SECURE_SSL_REDIRECT", "True")
SESSION_COOKIE_SECURE = (not DEBUG) and (not RUNNING_TESTS)
CSRF_COOKIE_SECURE = (not DEBUG) and (not RUNNING_TESTS)

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = (not DEBUG)
SECURE_HSTS_PRELOAD = (not DEBUG)


# =========================
# Logging
# =========================
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "snapshot_request_noise": {
            "()": "core.logging_filters.SnapshotRequestNoiseFilter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "request_console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
            "filters": ["snapshot_request_noise"],
        },
    },
    "formatters": {
        "simple": {
            "format": "[{levelname}] {message}",
            "style": "{",
        },
    },
    "loggers": {
        "django.request": {
            "handlers": ["request_console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
    },
}


# =========================
# Auth redirects
# =========================
LOGIN_URL = "dashboard:login"
LOGIN_REDIRECT_URL = "dashboard:index"
LOGOUT_REDIRECT_URL = "dashboard:login"
PASSWORD_RESET_TIMEOUT = env_int_clamped("PASSWORD_RESET_TIMEOUT", 3600, 300, 86400)

# How long a signed email-verification link stays valid (seconds). Generous by
# design: a school administrator may not open the mailbox the same day.
EMAIL_VERIFICATION_TIMEOUT = env_int_clamped(
    "EMAIL_VERIFICATION_TIMEOUT",
    7 * 24 * 3600,
    3600,
    30 * 24 * 3600,
)


# =========================
# Site base URL
# =========================
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://school-display.com")


# =========================
# Tamara checkout
# =========================
TAMARA_ENABLED = env_bool("TAMARA_ENABLED", "False")
TAMARA_API_BASE_URL = os.getenv(
    "TAMARA_API_BASE_URL",
    "https://api.tamara.co",
).strip().rstrip("/")
TAMARA_ENVIRONMENT = os.getenv("TAMARA_ENVIRONMENT", "production").strip().lower()
TAMARA_API_TOKEN = env_secret("TAMARA_API_TOKEN", "TAMARA_API_TOKEN_FILE")
TAMARA_NOTIFICATION_TOKEN = env_secret(
    "TAMARA_NOTIFICATION_TOKEN",
    "TAMARA_NOTIFICATION_TOKEN_FILE",
)
TAMARA_CALLBACK_BASE_URL = (
    os.getenv("TAMARA_CALLBACK_BASE_URL", SITE_BASE_URL).strip().rstrip("/")
    or SITE_BASE_URL.rstrip("/")
)
TAMARA_HTTP_TIMEOUT_SECONDS = env_int_clamped("TAMARA_HTTP_TIMEOUT_SECONDS", 15, 3, 60)
TAMARA_ELIGIBILITY_TIMEOUT_SECONDS = env_float_clamped(
    "TAMARA_ELIGIBILITY_TIMEOUT_SECONDS", 0.5, 0.1, 2.0
)
TAMARA_CAPTURE_DIGITAL_ORDERS = env_bool("TAMARA_CAPTURE_DIGITAL_ORDERS", "True")
TAMARA_RECONCILIATION_INTERVAL_SECONDS = env_int_clamped(
    "TAMARA_RECONCILIATION_INTERVAL_SECONDS", 10, 5, 300
)
TAMARA_RECONCILIATION_BATCH_SIZE = env_int_clamped(
    "TAMARA_RECONCILIATION_BATCH_SIZE", 20, 1, 100
)

if TAMARA_ENABLED and not DEBUG and not RUNNING_TESTS:
    if TAMARA_ENVIRONMENT not in {"production", "sandbox"}:
        raise RuntimeError("TAMARA_ENVIRONMENT must be either production or sandbox")
    if not TAMARA_API_TOKEN or TAMARA_API_TOKEN.startswith("CHANGE_ME"):
        raise RuntimeError("TAMARA_API_TOKEN or TAMARA_API_TOKEN_FILE is required when Tamara is enabled")
    if not TAMARA_NOTIFICATION_TOKEN or TAMARA_NOTIFICATION_TOKEN.startswith("CHANGE_ME"):
        raise RuntimeError("TAMARA_NOTIFICATION_TOKEN or TAMARA_NOTIFICATION_TOKEN_FILE is required when Tamara is enabled")
    expected_tamara_base_url = (
        "https://api-sandbox.tamara.co"
        if TAMARA_ENVIRONMENT == "sandbox"
        else "https://api.tamara.co"
    )
    if TAMARA_API_BASE_URL != expected_tamara_base_url:
        raise RuntimeError(
            f"Tamara {TAMARA_ENVIRONMENT} checkout must use {expected_tamara_base_url}"
        )
    if not TAMARA_CALLBACK_BASE_URL.startswith("https://"):
        raise RuntimeError("TAMARA_CALLBACK_BASE_URL must use HTTPS in production")


# =========================
# Moyasar card checkout
# =========================
MOYASAR_ENABLED = env_bool("MOYASAR_ENABLED", "False")
MOYASAR_LIVE_MODE = env_bool("MOYASAR_LIVE_MODE", "False")
MOYASAR_ACTIVATE_TEST_PAYMENTS = env_bool("MOYASAR_ACTIVATE_TEST_PAYMENTS", "False")
MOYASAR_API_BASE_URL = os.getenv(
    "MOYASAR_API_BASE_URL",
    "https://api.moyasar.com/v1",
).strip().rstrip("/")
MOYASAR_PUBLISHABLE_KEY = env_secret(
    "MOYASAR_PUBLISHABLE_KEY",
    "MOYASAR_PUBLISHABLE_KEY_FILE",
)
MOYASAR_SECRET_KEY = env_secret("MOYASAR_SECRET_KEY", "MOYASAR_SECRET_KEY_FILE")
MOYASAR_WEBHOOK_SECRET = env_secret(
    "MOYASAR_WEBHOOK_SECRET",
    "MOYASAR_WEBHOOK_SECRET_FILE",
)
MOYASAR_CALLBACK_BASE_URL = (
    os.getenv("MOYASAR_CALLBACK_BASE_URL", SITE_BASE_URL).strip().rstrip("/")
    or SITE_BASE_URL.rstrip("/")
)
MOYASAR_HTTP_TIMEOUT_SECONDS = env_int_clamped(
    "MOYASAR_HTTP_TIMEOUT_SECONDS",
    15,
    3,
    60,
)

# Every value must be a method Moyasar accepts for a hosted payment form.
# `stcpay` stays in this set but is deliberately absent from the default below:
# it is switched off for now, and keeping it supported means bringing it back is
# a one-line environment change rather than a code change.
#   MOYASAR_PAYMENT_METHODS=creditcard,applepay,stcpay,googlepay
_MOYASAR_SUPPORTED_METHODS = {"creditcard", "applepay", "stcpay", "googlepay"}
MOYASAR_PAYMENT_METHODS = [
    method
    for method in env_list("MOYASAR_PAYMENT_METHODS", "creditcard,applepay,googlepay")
    if method in _MOYASAR_SUPPORTED_METHODS
] or ["creditcard"]
MOYASAR_SUPPORTED_NETWORKS = env_list("MOYASAR_SUPPORTED_NETWORKS", "mada,visa,mastercard,unionpay")
MOYASAR_APPLE_PAY_COUNTRY = os.getenv("MOYASAR_APPLE_PAY_COUNTRY", "SA").strip().upper() or "SA"
MOYASAR_APPLE_PAY_LABEL = os.getenv("MOYASAR_APPLE_PAY_LABEL", "لوحة العرض الذكية").strip() or "لوحة العرض الذكية"
MOYASAR_APPLE_PAY_VALIDATE_MERCHANT_URL = (
    os.getenv(
        "MOYASAR_APPLE_PAY_VALIDATE_MERCHANT_URL",
        f"{MOYASAR_API_BASE_URL}/applepay/initiate",
    )
    .strip()
    .rstrip("/")
)
MOYASAR_GOOGLE_PAY_MERCHANT_ID = os.getenv("MOYASAR_GOOGLE_PAY_MERCHANT_ID", "").strip()
MOYASAR_GOOGLE_PAY_COUNTRY = os.getenv("MOYASAR_GOOGLE_PAY_COUNTRY", "SA").strip().upper() or "SA"
MOYASAR_GOOGLE_PAY_LABEL = os.getenv("MOYASAR_GOOGLE_PAY_LABEL", "لوحة العرض الذكية").strip() or "لوحة العرض الذكية"
MOYASAR_GOOGLE_PAY_ENVIRONMENT = os.getenv("MOYASAR_GOOGLE_PAY_ENVIRONMENT", "PRODUCTION").strip().upper() or "PRODUCTION"

# Reconciliation closes the "customer paid but the callback never arrived" gap.
MOYASAR_RECONCILIATION_INTERVAL_SECONDS = env_int_clamped(
    "MOYASAR_RECONCILIATION_INTERVAL_SECONDS",
    60,
    5,
    300,
)
MOYASAR_RECONCILIATION_BATCH_SIZE = env_int_clamped(
    "MOYASAR_RECONCILIATION_BATCH_SIZE",
    20,
    1,
    100,
)
MOYASAR_RECONCILIATION_LOOKBACK_HOURS = env_int_clamped(
    "MOYASAR_RECONCILIATION_LOOKBACK_HOURS",
    72,
    1,
    720,
)
MOYASAR_RECONCILIATION_MAX_PAGES = env_int_clamped(
    "MOYASAR_RECONCILIATION_MAX_PAGES",
    5,
    1,
    50,
)

if MOYASAR_ENABLED and not DEBUG and not RUNNING_TESTS:
    expected_publishable_prefix = "pk_live_" if MOYASAR_LIVE_MODE else "pk_test_"
    expected_secret_prefix = "sk_live_" if MOYASAR_LIVE_MODE else "sk_test_"
    if not MOYASAR_PUBLISHABLE_KEY.startswith(expected_publishable_prefix):
        raise RuntimeError("MOYASAR_PUBLISHABLE_KEY does not match MOYASAR_LIVE_MODE")
    if not MOYASAR_SECRET_KEY.startswith(expected_secret_prefix):
        raise RuntimeError("MOYASAR_SECRET_KEY does not match MOYASAR_LIVE_MODE")
    if not MOYASAR_CALLBACK_BASE_URL.startswith("https://"):
        raise RuntimeError("MOYASAR_CALLBACK_BASE_URL must use HTTPS in production")
    if MOYASAR_ACTIVATE_TEST_PAYMENTS and not MOYASAR_LIVE_MODE:
        raise RuntimeError("MOYASAR_ACTIVATE_TEST_PAYMENTS must remain False in production")


# =========================
# Transactional email
# =========================
# Local development prints messages to the terminal unless explicitly
# configured. Production uses SMTP and must provide the credentials below.
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    (
        "django.core.mail.backends.console.EmailBackend"
        if DEBUG
        else "django.core.mail.backends.smtp.EmailBackend"
    ),
).strip()
EMAIL_HOST = os.getenv("EMAIL_HOST", "localhost").strip()
EMAIL_PORT = env_int_clamped("EMAIL_PORT", 587, 1, 65535)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = env_secret("EMAIL_HOST_PASSWORD", "EMAIL_HOST_PASSWORD_FILE")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", "True")
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", "False")
EMAIL_TIMEOUT = env_int_clamped("EMAIL_TIMEOUT", 15, 1, 60)
DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL",
    "لوحة العرض الذكية <no-reply@mail.school-display.com>",
).strip()
SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL).strip()
EMAIL_REPLY_TO = os.getenv(
    "EMAIL_REPLY_TO",
    "دعم لوحة العرض الذكية <support@mail.school-display.com>",
).strip()
TRANSACTIONAL_EMAIL_ENABLED = env_bool("TRANSACTIONAL_EMAIL_ENABLED", "False")
RESEND_API_KEY = env_secret("RESEND_API_KEY", "RESEND_API_KEY_FILE")
RESEND_WEBHOOK_SECRET = env_secret(
    "RESEND_WEBHOOK_SECRET", "RESEND_WEBHOOK_SECRET_FILE"
)
RESEND_API_BASE_URL = os.getenv(
    "RESEND_API_BASE_URL", "https://api.resend.com"
).strip().rstrip("/")
RESEND_INBOUND_ADDRESS = os.getenv(
    "RESEND_INBOUND_ADDRESS", "support@mail.school-display.com"
).strip().casefold()
RESEND_INBOUND_ENABLED = env_bool("RESEND_INBOUND_ENABLED", "False")
RESEND_HTTP_TIMEOUT_SECONDS = env_int_clamped(
    "RESEND_HTTP_TIMEOUT_SECONDS", 10, 2, 30
)
EMAIL_NOTIFICATION_POLL_INTERVAL_SECONDS = env_int_clamped(
    "EMAIL_NOTIFICATION_POLL_INTERVAL_SECONDS", 30, 5, 300
)
EMAIL_NOTIFICATION_BATCH_SIZE = env_int_clamped("EMAIL_NOTIFICATION_BATCH_SIZE", 20, 1, 100)
EMAIL_NOTIFICATION_MAX_ATTEMPTS = env_int_clamped("EMAIL_NOTIFICATION_MAX_ATTEMPTS", 8, 1, 20)
EMAIL_SUBSCRIPTION_EXPIRY_DAYS = tuple(
    sorted(
        {
            max(0, min(365, int(value)))
            for value in env_list("EMAIL_SUBSCRIPTION_EXPIRY_DAYS", "14,7,3,1,0")
            if value.lstrip("+-").isdigit()
        },
        reverse=True,
    )
)

if TRANSACTIONAL_EMAIL_ENABLED and not DEBUG and not RUNNING_TESTS:
    if not EMAIL_HOST or not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD:
        raise RuntimeError(
            "EMAIL_HOST, EMAIL_HOST_USER, and EMAIL_HOST_PASSWORD are required "
            "when transactional email is enabled"
        )


# =========================
# Telegram administrator alerts
# =========================
# Secrets must live in the runtime environment only. Never commit the bot token.
TELEGRAM_ALERTS_ENABLED = env_bool("TELEGRAM_ALERTS_ENABLED", "False")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
TELEGRAM_API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org").strip().rstrip("/")
TELEGRAM_ALERTS_BASE_URL = (
    os.getenv("TELEGRAM_ALERTS_BASE_URL", SITE_BASE_URL).strip().rstrip("/")
    or SITE_BASE_URL.rstrip("/")
)
TELEGRAM_ALERT_EXPIRY_DAYS = tuple(
    sorted(
        {
            max(0, min(365, int(value)))
            for value in env_list("TELEGRAM_ALERT_EXPIRY_DAYS", "30,14,7,3,1,0")
            if value.lstrip("+-").isdigit()
        },
        reverse=True,
    )
)
TELEGRAM_ALERT_HTTP_TIMEOUT_SECONDS = env_int_clamped(
    "TELEGRAM_ALERT_HTTP_TIMEOUT_SECONDS",
    10,
    2,
    30,
)
TELEGRAM_ALERT_POLL_INTERVAL_SECONDS = env_int_clamped(
    "TELEGRAM_ALERT_POLL_INTERVAL_SECONDS",
    10,
    2,
    300,
)
TELEGRAM_ALERT_BATCH_SIZE = env_int_clamped(
    "TELEGRAM_ALERT_BATCH_SIZE",
    20,
    1,
    100,
)
TELEGRAM_ALERT_MAX_ATTEMPTS = env_int_clamped(
    "TELEGRAM_ALERT_MAX_ATTEMPTS",
    10,
    1,
    30,
)

# Provider-state reconciliation is read-only and never retries deliveries.
MAIL_DELIVERY_RECONCILE_ENABLED = env_bool("MAIL_DELIVERY_RECONCILE_ENABLED", "False" if RUNNING_TESTS else "True")
