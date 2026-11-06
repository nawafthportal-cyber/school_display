# core/middleware.py
from __future__ import annotations

from typing import Optional
import re
import logging
import secrets
import hashlib
import json
from urllib.parse import urlencode

from django.apps import apps
from django.db.models import Q
from django.http import JsonResponse, HttpResponsePermanentRedirect, HttpResponseRedirect
from django.conf import settings as dj_settings
from django.contrib.auth import logout
from django.urls import reverse
from django.utils import timezone
from django.core.cache import cache

from core.static_assets import build_static_response
from core.tenant_access import authorized_active_school


logger = logging.getLogger(__name__)


class SingleSessionMiddleware:
    """Allow only the most recently authenticated session for each account."""

    REPLACED_REASON = "session_replaced"

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _clear_session_cookie(response):
        response.delete_cookie(
            dj_settings.SESSION_COOKIE_NAME,
            path=getattr(dj_settings, "SESSION_COOKIE_PATH", "/"),
            domain=getattr(dj_settings, "SESSION_COOKIE_DOMAIN", None),
            samesite=getattr(dj_settings, "SESSION_COOKIE_SAMESITE", None),
        )
        return response

    def _replacement_response(self, request):
        path = (getattr(request, "path", "") or "").lower()
        if path.startswith("/api/") or "application/json" in (request.headers.get("Accept") or ""):
            response = JsonResponse(
                {
                    "detail": "session_replaced",
                    "message": "تم تسجيل الخروج لأن الحساب فُتح من جهاز آخر.",
                },
                status=401,
            )
            response["X-Session-Replaced"] = "1"
            return self._clear_session_cookie(response)

        login_url = reverse("dashboard:login")
        response = HttpResponseRedirect(f"{login_url}?{urlencode({'reason': self.REPLACED_REASON})}")
        return self._clear_session_cookie(response)

    def __call__(self, request):
        path = getattr(request, "path", "") or ""
        if path.startswith(("/static/", "/media/")):
            return self.get_response(request)

        from core.models import UserSessionState
        from core.session_security import (
            active_session_for_user,
            cache_active_session,
            was_session_replaced,
        )

        user = getattr(request, "user", None)
        current_key = (getattr(request.session, "session_key", None) or "").strip()

        if user is not None and getattr(user, "is_authenticated", False) and current_key:
            active_key = active_session_for_user(user.pk)
            if not active_key:
                state, _ = UserSessionState.objects.get_or_create(
                    user_id=user.pk,
                    defaults={"active_session_key": current_key},
                )
                active_key = (state.active_session_key or "").strip()
                cache_active_session(user.pk, active_key)

            if active_key and active_key != current_key:
                logout(request)
                return self._replacement_response(request)

        elif current_key and was_session_replaced(current_key):
            login_path = reverse("dashboard:login")
            if path == login_path and request.GET.get("reason") == self.REPLACED_REASON:
                response = self.get_response(request)
                return self._clear_session_cookie(response)
            return self._replacement_response(request)

        return self.get_response(request)


# ==========================================================
# Favicon Redirect (Hard block /static/favicon.ico via WhiteNoise)
# ==========================================================
class StaticFaviconRedirectMiddleware:
    """Permanently redirect legacy /static/favicon.ico to /favicon.ico.

    هدفه: منع WhiteNoise من خدمة /static/favicon.ico (الذي قد يكون StreamingHttpResponse تحت ASGI)
    وإغلاق التحذير نهائيًا حتى لو كانت أجهزة/كاش قديم يطلب المسار القديم.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = getattr(request, "path", "") or ""
        if path == "/static/favicon.ico" or path == "/static/favicon.ico/":
            resp = HttpResponsePermanentRedirect("/favicon.ico")
            # Make the redirect cheap and cacheable.
            resp["Cache-Control"] = "public, max-age=31536000"
            return resp
        return self.get_response(request)


class DirectStaticAssetMiddleware:
    """Serve static assets as regular HttpResponse under ASGI.

    WhiteNoise can emit FileResponse/StreamingHttpResponse objects, which trigger
    Django's async warning noise under ASGI. We short-circuit common static
    requests here with in-memory responses and let WhiteNoise remain as a
    fallback for any misses.
    """

    STATIC_PREFIX = "/static/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = getattr(request, "path", "") or ""
        method = (getattr(request, "method", "") or "GET").upper()

        if method not in {"GET", "HEAD"}:
            return self.get_response(request)
        if not path.startswith(self.STATIC_PREFIX):
            return self.get_response(request)

        static_name = path[len(self.STATIC_PREFIX):]
        response = build_static_response(
            static_name,
            method=method,
            is_versioned=bool(getattr(request, "GET", None) and request.GET.get("v")),
            range_header=request.headers.get("Range") if not request.headers.get("If-Range") else None,
        )
        if response is not None:
            return response
        return self.get_response(request)


# ==========================================================
# Streaming Response Probe (Diagnostics)
# ==========================================================
class StreamingResponseProbeMiddleware:
    """Logs the true source of any streaming response.

    هدفه: تحديد المسار الحقيقي الذي ينتج StreamingHttpResponse (غالباً static عبر WhiteNoise عند التشغيل على ASGI).

    ملاحظة:
    - نستخدم throttle عبر cache لتفادي إغراق اللوجز.
    - في حال كانت الاستجابة من WhiteNoise (static) لن يمر request عبر URL resolver غالباً،
      لذلك قد يكون resolver_match = None.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Opt-in only to avoid noisy logs in production.
        if not getattr(dj_settings, "MIDDLEWARE_DEBUG", False):
            return response

        try:
            is_streaming = bool(getattr(response, "streaming", False))
        except Exception:
            is_streaming = False

        if is_streaming:
            path = getattr(request, "path", "") or ""
            method = getattr(request, "method", "") or ""
            status = getattr(response, "status_code", None)
            resp_cls = response.__class__.__name__

            # View identification (may be None for middleware-served responses e.g. WhiteNoise)
            view_name = None
            view_func = None
            rm = getattr(request, "resolver_match", None)
            if rm is not None:
                try:
                    view_name = getattr(rm, "view_name", None)
                except Exception:
                    view_name = None
                try:
                    func = getattr(rm, "func", None)
                    view_func = f"{getattr(func, '__module__', '')}.{getattr(func, '__name__', '')}" if func else None
                except Exception:
                    view_func = None

            # Throttle identical warnings (5 minutes)
            key = f"diag:streaming:{method}:{path}:{resp_cls}:{status}:{view_name or '-'}"
            should_log = True
            try:
                should_log = bool(cache.add(key, "1", timeout=300))
            except Exception:
                should_log = True

            if should_log:
                logger.warning(
                    "STREAMING_RESPONSE path=%s method=%s status=%s response=%s view=%s func=%s",
                    path,
                    method,
                    status,
                    resp_cls,
                    view_name,
                    view_func,
                )

            # Debug headers (only when probe is enabled)
            try:
                response["X-Streaming-Response"] = "1"
                response["X-Streaming-Class"] = resp_cls
            except Exception:
                pass

        return response


# ==========================================================
# Snapshot Edge Cache Middleware (Cloudflare)
# ==========================================================
class SnapshotEdgeCacheMiddleware:
    """Hard-enforce cache headers for /api/display/snapshot/*.

    هدفه Phase 1:
    - منع أي Set-Cookie أو Vary: Cookie على مسار snapshot (حتى لا يصبح CF-Cache-Status: DYNAMIC)
    - منع كاش المتصفح نهائيًا (Cache-Control: no-store)
    - السماح لـ Cloudflare Edge بالكاش القصير عبر Cloudflare-CDN-Cache-Control (افتراضيًا 10 ثواني)
    """

    SNAPSHOT_PREFIX = "/api/display/snapshot/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        path = getattr(request, "path", "") or ""
        if not (path == self.SNAPSHOT_PREFIX or path.startswith(self.SNAPSHOT_PREFIX)):
            return response

        # Only apply to GET requests for snapshot.
        if request.method != "GET":
            return response

        # 1) Never set cookies on snapshot.
        try:
            response.cookies.clear()
        except Exception:
            pass

        # 2) Never vary by Cookie on snapshot (but keep Accept-Encoding for compression).
        vary = response.get("Vary")
        if vary:
            parts = [p.strip() for p in vary.split(",") if p.strip()]
            parts = [p for p in parts if p.lower() != "cookie"]
            if parts:
                response["Vary"] = ", ".join(parts)
            else:
                try:
                    del response["Vary"]
                except Exception:
                    pass

        # 3) Origin-only caching strategy: do not emit Cloudflare cache headers.
        # The view is responsible for Cache-Control/ETag; this middleware just sanitizes.
        try:
            if "Cloudflare-CDN-Cache-Control" in response:
                del response["Cloudflare-CDN-Cache-Control"]
        except Exception:
            pass

        if not response.get("Cache-Control"):
            response["Cache-Control"] = "no-store"
        return response


# ==========================================================
# Display Token Middleware (API فقط)
# ==========================================================
class DisplayTokenMiddleware:
    """
    يحقن:
      - request.display_screen
      - request.school

    يدعم التوكن من:
      - /api/display/snapshot/<token>/
      - ?token=...
      - Header: X-Display-Token
      - Authorization: Display <token>
    """

    API_PREFIX = "/api/display/"
    TOKEN_RE = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})$")
    TOKEN_PATH_RE = re.compile(
        r"^/api/display/(?:snapshot|today|live|status|goodbye)/(?P<token>[0-9a-fA-F]{32}|[0-9a-fA-F]{64})/?$"
    )

    # مسارات لا تتطلب توكن (اختياري)
    NO_TOKEN_PATHS = {
        "/api/display/ping/",
        "/api/display/metrics/",
        "/api/display/ws-metrics/",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def _extract_token(self, request) -> Optional[str]:
        # 1) token في المسار (snapshot|today|live|status/<token>/)
        m = self.TOKEN_PATH_RE.match(request.path or "")
        if m:
            return (m.group("token") or "").strip()

        # 2) token في QueryString
        t = (request.GET.get("token") or "").strip()
        if t:
            return t

        # 3) token في Header
        t = (request.headers.get("X-Display-Token") or "").strip()
        if t:
            return t

        # 4) Authorization: Display <token>
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("display "):
            return auth.split(" ", 1)[1].strip()

        return None

    def _pick_token_field(self, DisplayScreen) -> str:
        """
        مشاريعك السابقة ظهر فيها api_token أحيانًا.
        هنا ندعم أكثر من اسم حقل لتجنب FieldError.
        """
        candidates = ["token", "api_token", "display_token"]
        for f in candidates:
            try:
                DisplayScreen._meta.get_field(f)
                return f
            except Exception:
                continue
        raise LookupError("لم أجد حقل توكن مناسب في DisplayScreen (token/api_token/display_token).")

    def __call__(self, request):
        path = request.path or ""

        is_snapshot_path = path == "/api/display/snapshot/" or path.startswith("/api/display/snapshot/")
        is_display_client_path = (
            is_snapshot_path
            or path == "/api/display/status/"
            or path.startswith("/api/display/status/")
            or path == "/api/display/today/"
            or path.startswith("/api/display/today/")
            or path == "/api/display/live/"
            or path.startswith("/api/display/live/")
            # The farewell beacon comes from the television itself on teardown:
            # the token is its identity, and re-running the device-binding dance
            # (or attaching Set-Cookie) on a dying page would only lose the hint.
            or path == "/api/display/goodbye/"
            or path.startswith("/api/display/goodbye/")
        )

        if not path.startswith(self.API_PREFIX):
            return self.get_response(request)

        if path in self.NO_TOKEN_PATHS:
            return self.get_response(request)

        token = self._extract_token(request)
        if not token or not self.TOKEN_RE.match(token):
            resp = JsonResponse({"error": "Invalid display token"}, status=403)
            if is_display_client_path:
                resp["Cache-Control"] = "no-store"
            return resp

        # ------------------------------------------------------------------
        # Performance: Search Redis First
        # ------------------------------------------------------------------
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        neg_key = f"display:token_neg:{token_hash}"
        map_key = f"display:token_map:{token_hash}"

        # 1. Negative Cache (Invalid Token)
        if cache.get(neg_key):
            resp = JsonResponse({"error": "Invalid or inactive display token (cached)"}, status=403)
            if is_display_client_path:
                resp["Cache-Control"] = "no-store"
            return resp

        # 2. Positive Cache (Valid Token)
        cached_data = cache.get(map_key)
        screen = None
        DisplayScreen = apps.get_model("core", "DisplayScreen")
        School = apps.get_model("core", "School") # Might be needed

        if isinstance(cached_data, dict):
            # Reconstruct minimal objects to avoid DB
            try:
                screen = DisplayScreen()
                screen.id = cached_data["id"]
                screen.pk = cached_data["id"]
                screen.school_id = cached_data["school_id"]
                screen.bound_device_id = cached_data.get("bound_device_id")
                # Also attach a dummy school object if needed
                request.school = School()
                request.school.id = screen.school_id
                request.school.pk = screen.school_id
            except Exception:
                screen = None

        if not screen:
            token_field = self._pick_token_field(DisplayScreen)
            lookup = {f"{token_field}__iexact": token}

            # is_active اختياري حسب موديلك
            try:
                DisplayScreen._meta.get_field("is_active")
                lookup["is_active"] = True
            except Exception:
                pass

            try:
                screen = DisplayScreen.objects.select_related("school").get(**lookup)
                
                # Cache Success
                cache_payload = {
                    "id": screen.pk,
                    "school_id": screen.school_id,
                    "bound_device_id": getattr(screen, "bound_device_id", None)
                }
                cache.set(map_key, cache_payload, timeout=86400) # 24 hours

                request.school = screen.school

            except DisplayScreen.DoesNotExist:
                # Cache Failure
                cache.set(neg_key, "1", timeout=60)
                resp = JsonResponse({"error": "Invalid or inactive display token"}, status=403)
                if is_display_client_path:
                    resp["Cache-Control"] = "no-store"
                return resp
            except Exception:
                logger.exception("DisplayTokenMiddleware failed while fetching screen")
                resp = JsonResponse({"error": "Display token lookup failed"}, status=500)
                if is_display_client_path:
                    resp["Cache-Control"] = "no-store"
                return resp

        # ======================================================
        # ✅ Subscription gate
        #
        # The screen row and the token→school map are cached for hours, so a
        # lapsed subscription must be enforced here on every request rather
        # than relying on a screen being flipped inactive at some later point.
        # The lookup itself is cached per school and invalidated on any
        # subscription change, so this stays cheap at fleet scale.
        # ======================================================
        from subscriptions.access import school_display_is_suspended
        if school_display_is_suspended(getattr(screen, "school_id", None)):
            return JsonResponse({"error": "school_suspended", "message": "أُوقف العرض إداريًا؛ يرجى التواصل مع إدارة المنصة."}, status=403)

        if getattr(dj_settings, "DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION", True):
            try:
                from subscriptions.access import school_subscription_is_active

                subscription_ok = school_subscription_is_active(getattr(screen, "school_id", None))
            except Exception:
                # Never black out a paying customer's screens because the
                # billing lookup itself failed.
                logger.exception("display_subscription_gate_failed token_hash=%s", token_hash[:12])
                subscription_ok = True

            if not subscription_ok:
                resp = JsonResponse(
                    {
                        "error": "subscription_inactive",
                        "message": "اشتراك المدرسة غير نشط. الرجاء تجديد الاشتراك لاستئناف العرض.",
                    },
                    status=402,
                )
                resp["Cache-Control"] = "no-store"
                return resp

        request.display_screen = screen
        if not hasattr(request, "school") or not request.school:
             # Should be set above, but safe fallback (though caching dummy object avoids DB here)
             # If we are here from cache hit, request.school is a dummy.
             pass

        # ======================================================
        # ✅ Device Binding (منع مشاركة رابط الشاشة)
        #
        # ملاحظة (Phase 1 caching): مسار snapshot يجب ألا يرسل Set-Cookie حتى يكون قابل للكاش على Cloudflare.
        # لذلك نتجنب إنشاء/تعيين كوكي sd_device على snapshot.
        # ======================================================
        new_device_id: Optional[str] = None
        if not is_display_client_path:
            device_id = (request.COOKIES.get("sd_device") or "").strip()
            if not device_id:
                try:
                    new_device_id = secrets.token_hex(16)
                    device_id = new_device_id
                    # حتى ترى الـ view نفس الـ id في نفس الطلب
                    request.COOKIES["sd_device"] = device_id
                except Exception:
                    device_id = ""

            if device_id:
                has_binding_fields = True
                try:
                    DisplayScreen._meta.get_field("bound_device_id")
                    DisplayScreen._meta.get_field("bound_at")
                except Exception:
                    has_binding_fields = False

                if has_binding_fields:
                    bound = (getattr(screen, "bound_device_id", None) or "").strip()
                    if not bound:
                        updated = (
                            DisplayScreen.objects.filter(pk=screen.pk)
                            .filter(Q(bound_device_id__isnull=True) | Q(bound_device_id=""))
                            .update(bound_device_id=device_id, bound_at=timezone.now())
                        )
                        if updated:
                            bound = device_id
                            # ✅ Update Cache to reflect binding
                            try:
                                new_payload = {
                                    "id": screen.pk,
                                    "school_id": getattr(screen, "school_id", getattr(request.school, "pk", None)),
                                    "bound_device_id": bound
                                }
                                # Ensure we have a valid map number
                                if map_key:
                                    cache.set(map_key, new_payload, timeout=86400)
                            except Exception:
                                pass
                        else:
                            # تم ربطها من جهاز آخر قبلنا
                            try:
                                # Re-fetch from DB if concurrency issue
                                screen = DisplayScreen.objects.select_related("school").get(pk=screen.pk)
                                request.display_screen = screen
                                request.school = screen.school
                                bound = (getattr(screen, "bound_device_id", None) or "").strip()
                            except Exception:
                                bound = bound

                    if bound and bound != device_id:
                        return JsonResponse(
                            {
                                "error": "screen_bound",
                                "message": "هذه الشاشة مفعّلة حاليًا على جهاز آخر. لفك الارتباط وتفعيلها هنا، انتقل إلى لوحة التحكم ثم شاشات العرض.",
                            },
                            status=403,
                        )

        response = self.get_response(request)
        if (not is_display_client_path) and new_device_id and hasattr(response, "set_cookie"):
            try:
                # كوكي طويل المدى للجهاز (٥ سنوات تقريبًا)
                response.set_cookie(
                    "sd_device",
                    new_device_id,
                    max_age=60 * 60 * 24 * 365 * 5,
                    samesite="Lax",
                )
            except Exception:
                pass

        return response


# ==========================================================
# Active School Context (Context ONLY)
# ==========================================================
class ActiveSchoolMiddleware:
    """
    ✅ يحدد request.school للمستخدم المسجل (مدرسته النشطة)
    ❌ لا Redirect
    ❌ لا منع دخول

    مهم: لا يطغى على request.school إذا كان محدد مسبقًا (مثل display API).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # لا تكتب فوق request.school إذا موجودة
        if not hasattr(request, "school"):
            request.school = None

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return self.get_response(request)

        # لو سبق وتحددّت school من Middleware آخر، اتركها
        if getattr(request, "school", None) is not None:
            return self.get_response(request)

        profile = getattr(user, "profile", None)
        if not profile:
            return self.get_response(request)

        # عيّن active_school تلقائيًا عند عدم وجودها
        try:
            if not getattr(profile, "active_school_id", None) and profile.schools.exists():
                profile.active_school = profile.schools.first()
                profile.save(update_fields=["active_school"])
        except Exception:
            return self.get_response(request)

        request.school = authorized_active_school(profile, user=user)
        return self.get_response(request)


# ==========================================================
# Security Headers
# ==========================================================
class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Generated before the view runs so templates can read it, and unique
        # per response so it cannot be replayed.
        request.csp_nonce = secrets.token_urlsafe(16)

        response = self.get_response(request)
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
            "accelerometer=(), gyroscope=(), magnetometer=()"
        )

        if getattr(dj_settings, "CONTENT_SECURITY_POLICY_ENABLED", True):
            report_uri = str(
                getattr(dj_settings, "CONTENT_SECURITY_POLICY_REPORT_URI", "/csp-report/") or ""
            ).strip()
            # Templates that still carry an inline <script> opt in with
            # nonce="{{ csp_nonce }}". Without this, enforcing the policy would
            # silently break every such page.
            nonce = getattr(request, "csp_nonce", "")
            script_src = "script-src 'self' https://static.cloudflareinsights.com https://cdn.jsdelivr.net"
            if nonce:
                script_src = f"{script_src} 'nonce-{nonce}'"
            # Card checkout hands off to the gateway for 3-D Secure, which
            # renders in an iframe and posts back. Omitting these hosts makes
            # an enforced policy silently block payments.
            payment_hosts = "https://*.moyasar.com"
            directives = [
                "default-src 'self'",
                "base-uri 'self'",
                "object-src 'none'",
                "frame-ancestors 'self'",
                f"form-action 'self' {payment_hosts}",
                script_src,
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net",
                "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com",
                "img-src 'self' data: blob: https:",
                "media-src 'self' data: blob: https:",
                "connect-src 'self' ws: wss: https:",
                f"frame-src 'self' https://www.youtube-nocookie.com {payment_hosts}",
                "worker-src 'self' blob:",
                "manifest-src 'self'",
            ]
            if report_uri:
                directives.append(f"report-uri {report_uri}")
            header_name = (
                "Content-Security-Policy-Report-Only"
                if getattr(dj_settings, "CONTENT_SECURITY_POLICY_REPORT_ONLY", True)
                else "Content-Security-Policy"
            )
            response[header_name] = "; ".join(directives)
        return response
