from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal
from io import BytesIO
from urllib.parse import quote, urlencode

import qrcode

from django.contrib import messages
from django.contrib.auth import login
from django.conf import settings as django_settings
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from core import occasions
from core.health import readiness_status
from core.email_verification import EmailVerificationError, mark_verified, verify_token
from core.models import DisplayPairingSession, DisplayScreen, SubscriptionPlan
from dashboard.access import get_active_school_or_redirect
from display.pairing import (
    create_pairing_session,
    format_user_code,
    pairing_secret_matches,
    refresh_expired_status,
)
from schedule.models import SchoolSettings
from subscriptions.email_notifications import queue_email_verification
from subscriptions.plan_catalog import plan_cards
from .services import (
    TrialSignupError,
    activate_trial_if_eligible,
    check_trial_rate_limit,
    create_trial_signup,
    normalize_mobile,
)


logger = logging.getLogger(__name__)


THEME_MAP = {
    # legacy
    "default": "indigo",
    "boys": "emerald",
    "girls": "rose",

    # current
    "indigo": "indigo",
    "emerald": "emerald",
    "rose": "rose",
    "cyan": "cyan",
    "amber": "amber",
    "orange": "orange",
    "violet": "violet",
}


@require_GET
@never_cache
def health(request):
    checks = readiness_status()
    ready = all(checks.values())
    return JsonResponse({"status": "ok" if ready else "unavailable", "checks": checks}, status=200 if ready else 503)


def _pairing_rate_limited(key: str, *, limit: int, window_seconds: int) -> bool:
    """Best-effort fixed-window limiter; cache failure must not strand a TV."""
    cache_key = f"display:pairing:rate:{key}"
    try:
        if cache.add(cache_key, 1, timeout=window_seconds):
            return False
        return int(cache.incr(cache_key)) > int(limit)
    except Exception:
        return False


def _no_store_json(payload: dict, *, status: int = 200) -> JsonResponse:
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store, max-age=0"
    return response


@never_cache
@ensure_csrf_cookie
def tv_pairing(request):
    """A fixed, TV-friendly entry point that starts a one-time pairing flow."""
    return render(
        request,
        "website/tv_pairing.html",
        {
            "connect_url": request.build_absolute_uri(reverse("website:pairing_connect")),
        },
    )


@never_cache
@require_POST
def tv_pairing_start(request):
    device_id = (request.POST.get("device_id") or "").strip()
    remote_addr = (request.META.get("REMOTE_ADDR") or "unknown").strip()
    device_key = hashlib.sha256(device_id.encode("utf-8", errors="ignore")).hexdigest()[:20]
    start_limit = int(getattr(django_settings, "DISPLAY_PAIRING_START_LIMIT", 20) or 20)
    if _pairing_rate_limited(
        f"start:{remote_addr}:{device_key}",
        limit=start_limit,
        window_seconds=10 * 60,
    ):
        response = _no_store_json(
            {
                "error": "rate_limited",
                "message": "تم طلب رموز كثيرة. انتظر قليلًا ثم حاول مجددًا.",
            },
            status=429,
        )
        response["Retry-After"] = "60"
        return response

    try:
        pairing, device_secret = create_pairing_session(device_id)
    except ValueError:
        return _no_store_json(
            {"error": "invalid_device", "message": "تعذر تهيئة هذا المتصفح للربط."},
            status=400,
        )
    except RuntimeError:
        return _no_store_json(
            {"error": "unavailable", "message": "تعذر إنشاء رمز الربط الآن. حاول مجددًا."},
            status=503,
        )

    return _no_store_json(
        {
            "pairing_id": str(pairing.pk),
            "user_code": pairing.user_code,
            "formatted_code": format_user_code(pairing.user_code),
            "device_secret": device_secret,
            "expires_in": max(0, int((pairing.expires_at - timezone.now()).total_seconds())),
            "status_url": reverse("website:tv_pairing_status", args=[pairing.pk]),
            "qr_url": reverse("website:tv_pairing_qr", args=[pairing.pk]),
        }
    )


@never_cache
@require_POST
def tv_pairing_status(request, pairing_id):
    pairing = get_object_or_404(
        DisplayPairingSession.objects.select_related("screen"),
        pk=pairing_id,
    )
    if not pairing_secret_matches(pairing, request.POST.get("device_secret")):
        return _no_store_json(
            {"error": "not_found", "message": "جلسة الربط غير متاحة."},
            status=404,
        )

    refresh_expired_status(pairing)
    payload = {
        "status": pairing.status,
        "expires_in": max(0, int((pairing.expires_at - timezone.now()).total_seconds())),
    }
    if pairing.status == DisplayPairingSession.STATUS_APPROVED and pairing.screen_id:
        screen = pairing.screen
        if screen and screen.is_active and screen.short_code:
            display_path = reverse("website:short_display", args=[screen.short_code])
            payload.update(
                {
                    "screen_name": screen.name,
                    "display_url": f"{display_path}#pair={quote(pairing.device_id, safe='')}",
                }
            )
        else:
            payload.update(
                {
                    "status": DisplayPairingSession.STATUS_CANCELLED,
                    "message": "الشاشة المختارة لم تعد متاحة.",
                }
            )
    elif pairing.status == DisplayPairingSession.STATUS_EXPIRED:
        payload["message"] = "انتهت صلاحية الرمز. أنشئ رمزًا جديدًا للمتابعة."
    elif pairing.status == DisplayPairingSession.STATUS_CANCELLED:
        payload["message"] = "أُلغيت جلسة الربط. أنشئ رمزًا جديدًا للمتابعة."
    return _no_store_json(payload)


@never_cache
@require_GET
def tv_pairing_qr(request, pairing_id):
    pairing = get_object_or_404(DisplayPairingSession, pk=pairing_id)
    pairing = refresh_expired_status(pairing)
    if pairing.status != DisplayPairingSession.STATUS_PENDING:
        raise Http404("Pairing session is no longer active.")

    approval_url = request.build_absolute_uri(
        reverse("dashboard:screen_pairing_confirm", args=[pairing.pk])
    )
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=12,
        border=3,
    )
    qr.add_data(approval_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#0f172a", back_color="#ffffff").convert("RGB")
    output = BytesIO()
    image.save(output, format="PNG")
    response = HttpResponse(output.getvalue(), content_type="image/png")
    response["Cache-Control"] = "no-store, max-age=0"
    response["Content-Disposition"] = 'inline; filename="tv-pairing.png"'
    return response


@never_cache
def pairing_connect(request):
    """Short mobile entry point shown on the TV for manual code entry."""
    return redirect("dashboard:screen_pairing")


def _abs_media_url(request, maybe_url: str | None) -> str | None:
    if not maybe_url:
        return None
    s = str(maybe_url).strip()
    if s.lower() in {"none", "null", "-"}:
        return None
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        return s
    try:
        return request.build_absolute_uri(s)
    except Exception:
        return s


# Smart-TV and set-top browsers, matched against the User-Agent. Kept in sync
# with the client-side list in ``static/js/display.js`` (``isLiteMode``).
_LITE_MODE_USER_AGENTS = (
    "smarttv", "hbbtv", "tizen", "web0s", "webos", "netcast", "viera", "nettv",
    "philipstv", "googletv", "crkey", "firetv", "aftm", "aftt", "roku", "vidaa",
    "saphi", "bravia", "mitv", "android tv",
)


def _display_lite_mode(request) -> str:
    """Decide the board's lite mode before the first byte of HTML is sent.

    ``display.js`` can work this out on its own, but only once a deferred 132 KB
    bundle has parsed and DOMContentLoaded has fired — by which time the screen
    has usually painted the full design once, blurred orbs and all, and then
    thrown it away. Resolving it here means a TV never renders the heavy frame
    at all.

    Returns ``"1"``/``"0"`` when the answer is known server-side, or ``""`` to
    leave the decision to the client's hardware heuristics (core count, device
    memory), which are not visible from here.
    """
    override = (request.GET.get("lite") or request.GET.get("liteMode") or "").strip().lower()
    if override in {"1", "true", "yes"}:
        return "1"
    if override in {"0", "false", "no"}:
        return "0"

    try:
        user_agent = str(request.META.get("HTTP_USER_AGENT", "") or "").lower()
    except Exception:
        return ""
    if not user_agent:
        return ""

    return "1" if any(marker in user_agent for marker in _LITE_MODE_USER_AGENTS) else ""


def _resolve_screen_and_settings(
    key: str | None,
) -> tuple[DisplayScreen | None, SchoolSettings | None, str | None]:
    """
    key قد يكون:
    - token طويل (64)
    - أو short_code قصير (مثل 6)
    نُرجع دائمًا effective_token = screen.token حتى تعتمد الواجهة والـ API على token الحقيقي.
    """
    if not key:
        return None, None, None

    k = str(key).strip()
    if not k:
        return None, None, None

    screen = (
        DisplayScreen.objects.select_related("school")
        .filter(is_active=True)
        .filter(Q(token__iexact=k) | Q(short_code__iexact=k))
        .first()
    )
    if not screen:
        return None, None, None

    try:
        settings_obj = screen.school.schedule_settings
    except SchoolSettings.DoesNotExist:
        # نرجع token الحقيقي حتى لو الإعدادات ناقصة
        return screen, None, screen.token

    return screen, settings_obj, screen.token


def _display_subscription_active(school_id) -> bool:
    """Whether this school may still be served display content.

    Mirrors the API-side gate in ``core.middleware.DisplayTokenMiddleware`` so
    the page and the data it fetches always agree.
    """
    if not getattr(django_settings, "DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION", True):
        return True
    try:
        from subscriptions.access import school_subscription_is_active

        return school_subscription_is_active(school_id)
    except Exception:
        # Never black out a paying customer because billing lookup failed.
        logger.exception("display_page_subscription_gate_failed school_id=%s", school_id)
        return True


def _build_display_context(request, key: str | None) -> dict | None:
    if not key:
        return None

    # ?nocache=1 مفيد أثناء التطوير
    preview_theme = (request.GET.get("preview_theme") or "").strip().lower()
    preview_accent = (request.GET.get("preview_accent") or "").strip()
    preview_lock = bool(preview_theme or preview_accent)
    bypass_cache = (request.GET.get("nocache") == "1") or preview_lock

    screen, settings_obj, effective_token = _resolve_screen_and_settings(key)
    if not screen or not settings_obj or not effective_token:
        return None

    # A manager opening "فتح" or "فتح المعاينة" from the dashboard gets a
    # read-only view that never claims the screen's device slot. The context is
    # per-viewer, so it must stay out of the shared render cache.
    from display.services import resolve_preview_screen

    preview_mode = resolve_preview_screen(request, effective_token) is not None

    try:
        schedule_revision = int(getattr(settings_obj, "schedule_revision", 0) or 0)
    except Exception:
        schedule_revision = 0

    # ✅ اربط الكاش بـ revision حتى ينعكس أي تحديث إعدادات فورًا.
    cache_key = f"display_ctx:{effective_token}:rev:{schedule_revision}"
    if not bypass_cache and not preview_mode:
        cached = cache.get(cache_key)
        if cached:
            return cached

    def _screen_override(field_name: str, fallback):
        value = getattr(screen, field_name, None)
        return fallback if value in (None, "") else value

    # شعار
    logo_url = None
    if getattr(screen, "logo_override", None):
        try:
            logo_url = screen.logo_override.url
        except Exception:
            logo_url = None
    if not logo_url and settings_obj.school and getattr(settings_obj.school, "logo", None):
        try:
            logo_url = settings_obj.school.logo.url
        except Exception:
            logo_url = None
    if not logo_url:
        logo_url = getattr(settings_obj, "logo_url", None)
    logo_url = _abs_media_url(request, logo_url)

    screen_theme_override = (getattr(screen, "theme_override", "") or "").strip()
    raw_theme = screen_theme_override or getattr(settings_obj, "theme", "default")
    theme = THEME_MAP.get(raw_theme, "indigo")

    # Preview overrides (non-persistent, per-request)
    if preview_theme:
        theme = THEME_MAP.get(preview_theme, theme)

    if isinstance(preview_accent, str):
        preview_accent = preview_accent.strip()
    else:
        preview_accent = ""
    if preview_accent and (len(preview_accent) != 7 or not preview_accent.startswith("#")):
        preview_accent = ""

    school_name = getattr(settings_obj, "name", None) or getattr(settings_obj.school, "name", "مدرستنا")
    school_type = getattr(settings_obj.school, "school_type", "") if getattr(settings_obj, "school", None) else ""
    display_accent_color = _screen_override(
        "display_accent_color_override",
        getattr(settings_obj, "display_accent_color", None),
    )
    if preview_accent:
        display_accent_color = preview_accent

    ctx = {
        "screen": screen,
        "settings": settings_obj,
        "school_name": school_name,
        "school_type": school_type,
        "display_accent_color": display_accent_color,
        "logo_url": logo_url,
        "refresh_interval_sec": getattr(settings_obj, "refresh_interval_sec", 30),
        "ws_live_status_check_sec": int(
            getattr(django_settings, "DISPLAY_WS_LIVE_STATUS_CHECK_SEC", 60) or 60
        ),
        "standby_scroll_speed": _screen_override(
            "standby_scroll_speed_override",
            getattr(settings_obj, "standby_scroll_speed", 0.8),
        ),
        "periods_scroll_speed": _screen_override(
            "periods_scroll_speed_override",
            getattr(settings_obj, "periods_scroll_speed", 0.5),
        ),
        "bell_sound_url": static(settings_obj.get_bell_sound_path()),
        "now_hour": timezone.localtime().hour,
        "theme": theme,
        "theme_key": raw_theme,
        "preview_mode": preview_mode,
        "screen_theme_override": screen_theme_override,
        "screen_occasion_theme": getattr(screen, "occasion_theme", "auto") or "auto",
        "screen_featured_panel": (
            getattr(screen, "featured_panel_override", "") or ""
        ),
        "screen_display_copy": {
            "before_title": _screen_override("display_before_title_override", settings_obj.get_display_before_title()),
            "before_badge": _screen_override("display_before_badge_override", settings_obj.get_display_before_badge()),
            "after_title": _screen_override("display_after_title_override", settings_obj.get_display_after_title()),
            "after_badge": _screen_override("display_after_badge_override", settings_obj.get_display_after_badge()),
            "after_holiday_title": _screen_override("display_after_holiday_title_override", settings_obj.get_display_after_holiday_title()),
            "after_holiday_badge": _screen_override("display_after_holiday_badge_override", settings_obj.get_display_after_holiday_badge()),
            "holiday_title": _screen_override("display_holiday_title_override", settings_obj.get_display_holiday_title()),
            "holiday_badge": _screen_override("display_holiday_badge_override", settings_obj.get_display_holiday_badge()),
        },
        "screen_show_announcements": bool(getattr(screen, "show_announcements", True)),
        "screen_show_period_classes": bool(getattr(screen, "show_period_classes", True)),
        "screen_show_standby": bool(getattr(screen, "show_standby", True)),
        "screen_show_duty": bool(getattr(screen, "show_duty", True)),
        "screen_show_excellence": bool(getattr(screen, "show_excellence", True)),
        "preview_lock": preview_lock,
        # ✅ نعطي الواجهة token الحقيقي دائمًا
        "api_token": effective_token,
        "display_token": effective_token,
        "token": effective_token,
        "school_id": settings_obj.school_id if settings_obj.school_id else None,
        "screen_id": screen.pk,
        "schedule_revision": schedule_revision,
        # مهم: هذا هو المسار الذي يستدعيه display.js
        "snapshot_url": f"/api/display/snapshot/{effective_token}/",
        "display_use_minified_js": bool(
            getattr(django_settings, "DISPLAY_USE_MINIFIED_JS", False)
        ),
        # الهوية البصرية للمناسبات تُولَّد من سجل واحد بدل تكرارها في CSS
        # وJavaScript. إدراجها في الصفحة (لا في الـ snapshot) يجعلها متاحة
        # لحظة الإقلاع ومحفوظة ضمن الصفحة في ذاكرة Service Worker، فتعمل
        # المناسبات بلا اتصال أيضًا.
        "occasion_themes": occasions.all_occasions(),
        "occasion_theme_meta_json": json.dumps(
            occasions.theme_map(), ensure_ascii=False, separators=(",", ":")
        ),
    }

    if not preview_mode:
        cache.set(cache_key, ctx, 60)
    return ctx


@never_cache
def home(request):
    """
    الصفحة الرئيسية للشاشة:
    /?token=XXXX
    token قد يكون token طويل أو short_code بعد التحديث.
    """
    key = request.GET.get("token") or None
    ctx = _build_display_context(request, key)
    if not ctx:
        active_plans = list(
            SubscriptionPlan.objects.filter(is_active=True).order_by("sort_order", "price", "id")
        )
        landing_plans = plan_cards(active_plans)

        paid_plans = [plan for plan in landing_plans if not plan["is_trial"] and not plan["is_free"]]
        landing_monthly_plans = [
            plan for plan in paid_plans if 27 <= plan["duration_days"] <= 31
        ]
        landing_annual_plans = [
            plan for plan in paid_plans if 330 <= plan["duration_days"] <= 370
        ]
        landing_semiannual_plans = [
            plan for plan in paid_plans if 150 <= plan["duration_days"] <= 200
        ]

        monthly_by_screens = {
            plan["max_screens"]: Decimal(plan["price"])
            for plan in landing_monthly_plans
            if plan["max_screens"] is not None
            and plan["code"].startswith("school-screen-")
            and plan["code"].endswith("-monthly")
        }
        for plans, comparison_months in (
            (landing_semiannual_plans, 6),
            (landing_annual_plans, 10),
        ):
            for plan in plans:
                if not plan["code"].startswith("school-screen-"):
                    continue
                monthly_price = monthly_by_screens.get(plan["max_screens"])
                if monthly_price is None:
                    continue
                savings = (monthly_price * comparison_months) - Decimal(plan["price"])
                if savings > 0:
                    plan["savings_text"] = (
                        f"توفر {savings:,.0f} ر.س مقارنة بـ{comparison_months} دفعات شهرية"
                    )
        known_cycle_ids = {
            plan["id"]
            for plan in landing_monthly_plans + landing_annual_plans + landing_semiannual_plans
        }
        # Keep unusual dashboard-created durations visible instead of silently
        # dropping them from the public catalog.
        landing_annual_plans.extend(
            plan for plan in paid_plans if plan["id"] not in known_cycle_ids
        )

        landing_trial = next(
            (plan for plan in landing_plans if plan["is_trial"]),
            {
                "name": "تجربة مجانية",
                "price": 0,
                "duration_days": 14,
                "duration_label": "14 يوماً",
                "is_trial": True,
                "is_free": True,
            },
        )
        return render(
            request,
            "website/unconfigured_display.html",
            {
                "token": key,
                "login_url": reverse("dashboard:login"),
                "trial_signup_url": reverse("website:trial_signup"),
                "plan_order_url": reverse("website:plan_order"),
                "dashboard_url": reverse("dashboard:index"),
                "landing_plans": landing_plans,
                "landing_monthly_plans": landing_monthly_plans,
                "landing_annual_plans": landing_annual_plans,
                "landing_semiannual_plans": landing_semiannual_plans,
                "landing_trial": landing_trial,
            },
        )
    # Never fold this into ``ctx``: that dict is cached per token+revision, while
    # lite mode depends on the requesting device.
    return render(request, "website/display.html", {**ctx, "lite_mode": _display_lite_mode(request)})


def subscriptions(request):
    return render(request, "website/subscriptions.html")


@never_cache
@require_GET
def plan_order(request):
    """Resume a landing-page paid-plan journey after authentication."""
    plan_code = (request.GET.get("plan") or "").strip()
    plan = (
        SubscriptionPlan.objects.filter(code=plan_code, is_active=True, price__gt=0)
        .only("code", "name")
        .first()
    )
    if plan is None:
        messages.error(request, "الباقة المطلوبة غير متاحة حاليًا. اختر إحدى الباقات المتاحة.")
        return redirect(f'{reverse("website:home")}#pricing')

    order_path = f'{reverse("website:plan_order")}?{urlencode({"plan": plan.code})}'
    if not request.user.is_authenticated:
        login_url = reverse("dashboard:login")
        return redirect(f'{login_url}?{urlencode({"next": order_path})}')

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    trial_result = activate_trial_if_eligible(request.user, school)
    if trial_result.activated:
        messages.success(
            request,
            "تم تفعيل الباقة المجانية تلقائيًا. يمكنك الآن اختيار الباقة المدفوعة وإكمال الدفع.",
        )

    subscription_url = reverse("dashboard:my_subscription")
    return redirect(
        f'{subscription_url}?{urlencode({"plan": plan.code, "source": "landing"})}#renewal-section'
    )


@require_GET
def verify_email(request, token: str):
    """Confirm ownership of the address used at signup."""
    try:
        user = verify_token(token)
    except EmailVerificationError as exc:
        return render(
            request,
            "website/verify_email_result.html",
            {"ok": False, "message": str(exc)},
            status=400,
        )

    newly_verified = mark_verified(user)
    return render(
        request,
        "website/verify_email_result.html",
        {
            "ok": True,
            "already_verified": not newly_verified,
            "email": user.email,
            "login_url": reverse("dashboard:login"),
        },
    )


@require_POST
@csrf_protect
def trial_signup(request):
    mobile = normalize_mobile(request.POST.get("mobile", ""))

    try:
        check_trial_rate_limit(request, mobile)
        result = create_trial_signup(request.POST)
    except TrialSignupError as exc:
        return JsonResponse({"ok": False, "errors": exc.errors}, status=400)
    except Exception:
        return JsonResponse(
            {
                "ok": False,
                "errors": {
                    "__all__": "حدث خطأ غير متوقع أثناء إنشاء التجربة. الرجاء المحاولة مرة أخرى."
                },
            },
            status=500,
        )

    # Prove the address works so invoices and password resets can reach them.
    # Queued, never sent inline: SMTP latency must not slow down signup.
    try:
        queue_email_verification(result.subscription, result.user)
    except Exception:
        logger.exception("trial_signup_verification_queue_failed user_id=%s", result.user.pk)

    login(request, result.user, backend="django.contrib.auth.backends.ModelBackend")

    requested_plan_code = (request.POST.get("plan_code") or "").strip()
    requested_plan = (
        SubscriptionPlan.objects.filter(
            code=requested_plan_code,
            is_active=True,
            price__gt=0,
        )
        .only("code")
        .first()
    )
    if requested_plan is not None:
        redirect_url = (
            f'{reverse("website:plan_order")}?'
            f'{urlencode({"plan": requested_plan.code})}'
        )
    else:
        redirect_url = reverse("dashboard:help_getting_started")

    return JsonResponse(
        {
            "ok": True,
            "message": "تم تجهيز تجربة المدرسة بنجاح.",
            "redirect_url": redirect_url,
            "login_url": reverse("dashboard:login"),
            "username": result.user.username,
            "mobile": getattr(getattr(result.user, "profile", None), "mobile", "") or mobile,
            "school_name": result.school.name,
        }
    )


def display(request):
    return home(request)


@never_cache
@xframe_options_sameorigin
def display_view(request, screen_key: str):
    """
    /display/<screen_key> (اختياري)
    screen_key قد يكون token أو short_code.
    """
    if not screen_key:
        raise Http404("Missing screen key.")

    ctx = _build_display_context(request, screen_key)
    if not ctx:
        raise Http404("Display is not configured or found.")

    # A lapsed school gets a dignified renewal notice on the TV rather than a
    # display shell that silently fails against a 402 from the snapshot API.
    if not _display_subscription_active(ctx.get("school_id")):
        return render(
            request,
            "website/display_subscription_inactive.html",
            {
                "school_name": ctx.get("school_name") or "",
                "logo_url": ctx.get("logo_url") or "",
            },
            status=402,
        )

    # See ``home``: lite mode is per-device, so it stays out of the cached ctx.
    return render(request, "website/display.html", {**ctx, "lite_mode": _display_lite_mode(request)})


@never_cache
def short_display_redirect(request, short_code: str):
    """
    ✅ الرابط المختصر: /s/<short_code> أو /s/<short_code>/
    بعد التحديث لا نعمل redirect للرابط الطويل،
    بل نعرض الشاشة مباشرة (أفضل للتلفاز وأسهل للمستخدم).
    """
    code = (short_code or "").strip()
    if not code:
        raise Http404("Invalid short code.")
    return display_view(request, code)
