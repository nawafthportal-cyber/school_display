from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from urllib.parse import quote

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from display.ws_groups import school_group_name
from core.display_presence import display_live_threshold_seconds, latest_display_presence
from core.screen_monitoring import format_duration
from .access import get_active_school_or_redirect
from .decorators import manager_required
from .forms import DisplayScreenForm, SchoolSettingsForm, ScreenDisplayCustomizationForm

logger = logging.getLogger(__name__)


def _school_settings_model():
    return apps.get_model("schedule", "SchoolSettings")


def _refresh_school_displays(school, *, target_screen=None) -> None:
    """Bump the shared revision and reload the affected display pages."""
    school_id = int(getattr(school, "pk", 0) or 0)
    if not school_id:
        return
    try:
        from schedule.cache_utils import (
            bump_schedule_revision_for_school_id,
            invalidate_display_snapshot_cache_for_school_id,
        )

        bump_schedule_revision_for_school_id(school_id)
        invalidate_display_snapshot_cache_for_school_id(school_id)
    except Exception:
        logger.exception("screen_customization_cache_invalidation_failed school_id=%s", school_id)

    screens = [target_screen] if target_screen is not None else list(
        _display_screen_model().objects.filter(school_id=school_id, is_active=True)
    )
    for screen in screens:
        _queue_screen_reload(screen, school_id=school_id)


def _display_screen_model():
    return apps.get_model("core", "DisplayScreen")


def _model_has_field(model, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)
        return True
    except Exception:
        return False


def _screen_command_ttl_seconds() -> int:
    """
    Manual screen commands must outlive the client's fallback status checks.

    The display can stay in ws-live mode and only hit `/status` every 5 minutes,
    and outside the active window that heartbeat can stretch to 30 minutes.
    If the token-scoped cache key expires earlier, a missed WebSocket broadcast
    makes the command effectively disappear before the screen can observe it.
    """
    default_ttl = 65 * 60  # 65 minutes: safely above the 30-minute off-hours heartbeat.
    try:
        raw = int(getattr(settings, "DISPLAY_REMOTE_COMMAND_TTL_SEC", default_ttl) or default_ttl)
    except Exception:
        raw = default_ttl
    return max(30 * 60, min(raw, 24 * 60 * 60))


def _broadcast_reload_screen_ws(*, school_id: int, screen_id: int) -> None:
    try:
        if not getattr(settings, "DISPLAY_WS_ENABLED", False):
            return
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            school_group_name(int(school_id)),
            {
                "type": "broadcast_reload",
                "school_id": int(school_id),
                "target_screen_id": int(screen_id),
            },
        )
    except Exception:
        logger.exception(
            "screen_reload_broadcast_failed school_id=%s screen_id=%s",
            school_id,
            screen_id,
        )


def _queue_screen_reload(screen, *, school_id: int) -> bool:
    """Invalidate the rendered page and ask only this TV to reload."""
    token_value = (
        (getattr(screen, "token", None) or getattr(screen, "api_token", None) or "").strip()
    )
    if not token_value:
        return False

    token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    try:
        cache.set(
            f"display:force_reload:{token_hash}",
            "1",
            timeout=_screen_command_ttl_seconds(),
        )
        cache.delete(f"display_ctx:{token_value}")
        try:
            revision = int(screen.school.schedule_settings.schedule_revision or 0)
            cache.delete(f"display_ctx:{token_value}:rev:{revision}")
        except Exception:
            pass
    except Exception:
        pass

    try:
        transaction.on_commit(
            lambda: _broadcast_reload_screen_ws(
                school_id=int(school_id),
                screen_id=int(screen.pk),
            )
        )
    except Exception:
        _broadcast_reload_screen_ws(school_id=int(school_id), screen_id=int(screen.pk))
    return True


def _get_subscription_model_robust():
    """
    Source of truth: subscriptions.SchoolSubscription.
    Fallback to core.SchoolSubscription exists for legacy deployments only.
    """
    try:
        return apps.get_model("subscriptions", "SchoolSubscription")
    except Exception:
        logger.warning(
            "subscriptions.SchoolSubscription not available; falling back to legacy core.SchoolSubscription"
        )
        try:
            return apps.get_model("core", "SchoolSubscription")
        except Exception:
            logger.error(
                "No subscription model available (subscriptions/core). Screen limits may be degraded."
            )
            return None


def get_school_active_subscriptions_qs(school):
    """يرجع QuerySet للاشتراكات السارية (قد تكون أكثر من اشتراك)."""
    sub_model = _get_subscription_model_robust()
    if sub_model is None:
        return None

    today = timezone.localdate()
    qs = sub_model.objects.all()

    # school filter
    try:
        sub_model._meta.get_field("school")
        qs = qs.filter(school=school)
    except Exception:
        try:
            sub_model._meta.get_field("school_id")
            qs = qs.filter(school_id=getattr(school, "id", school))
        except Exception:
            return None

    # active status
    try:
        sub_model._meta.get_field("status")
        qs = qs.filter(status="active")
    except Exception:
        try:
            sub_model._meta.get_field("is_active")
            qs = qs.filter(is_active=True)
        except Exception:
            pass

    # start date
    for f in ("starts_at", "start_date"):
        try:
            sub_model._meta.get_field(f)
            qs = qs.filter(**{f"{f}__lte": today})
            break
        except Exception:
            continue

    # end date
    for f in ("ends_at", "end_date"):
        try:
            sub_model._meta.get_field(f)
            qs = qs.filter(Q(**{f"{f}__isnull": True}) | Q(**{f"{f}__gte": today}))
            break
        except Exception:
            continue

    # select plan if possible
    try:
        sub_model._meta.get_field("plan")
        qs = qs.select_related("plan").defer("plan__duration_days")
    except Exception:
        pass

    return qs


def get_school_active_subscription(school):
    """يرجع اشتراك المدرسة الساري (إن وجد) بشكل مرن بين subscriptions و legacy."""
    qs = get_school_active_subscriptions_qs(school)
    if qs is None:
        return None
    subs = list(qs)
    if not subs:
        return None

    def _key(sub):
        plan = getattr(sub, "plan", None)
        ms = getattr(plan, "max_screens", None) if plan else None
        return (1 if ms is None else 0, int(ms or 0))

    subs.sort(key=_key, reverse=True)
    return subs[0]


def get_school_max_screens_limit(school) -> int | None:
    """يرجع الحد الأقصى للشاشات حسب خطة الاشتراك. None = غير محدود."""
    try:
        from subscriptions.utils import school_effective_max_screens

        return school_effective_max_screens(getattr(school, "id", None))
    except Exception:
        pass

    qs = get_school_active_subscriptions_qs(school)
    if qs is None:
        return 0
    subs = list(qs)
    if not subs:
        return 0

    for sub in subs:
        plan = getattr(sub, "plan", None)
        if plan is not None and getattr(plan, "max_screens", None) is None:
            return None

    best = 0
    for sub in subs:
        plan = getattr(sub, "plan", None)
        ms = getattr(plan, "max_screens", None) if plan else None
        try:
            ms_i = int(ms or 0)
        except Exception:
            ms_i = 0
        if ms_i > best:
            best = ms_i
    return best


def get_school_effective_plan_label(school) -> str | None:
    """اسم الخطة المستخدمة لعرض المعلومات في الواجهة (أفضل اشتراك)."""
    sub = get_school_active_subscription(school)
    if not sub:
        return None
    plan = getattr(sub, "plan", None)
    return getattr(plan, "name", None) if plan else None


@manager_required
def screen_list(request):
    display_screen = _display_screen_model()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    try:
        from core.screen_limits import enforce_school_screen_limit

        enforce_school_screen_limit(int(getattr(school, "id", 0) or 0))
    except Exception:
        pass

    auto_disabled_count = 0
    try:
        if _model_has_field(display_screen, "auto_disabled_by_limit"):
            auto_disabled_count = display_screen.objects.filter(
                school=school,
                auto_disabled_by_limit=True,
            ).count()
    except Exception:
        auto_disabled_count = 0

    qs = display_screen.objects.filter(school=school).order_by("-created_at", "-id")
    current_count = qs.count()
    max_screens = get_school_max_screens_limit(school)
    plan_name = get_school_effective_plan_label(school)
    try:
        from subscriptions.utils import school_has_paid_active_subscription

        can_purchase_screen_addon = school_has_paid_active_subscription(school.pk)
    except Exception:
        can_purchase_screen_addon = False

    now = timezone.now()
    live_threshold_seconds = display_live_threshold_seconds()
    live_threshold_minutes = max(1, int(round(live_threshold_seconds / 60)))
    live_count = 0
    linked_count = 0
    offline_count = 0
    waiting_count = 0
    disabled_count = 0
    uptime_values = []
    seven_days_ago = now - timedelta(days=7)
    screen_ids = list(qs.values_list("pk", flat=True))
    outage_model = apps.get_model("core", "ScreenOutage")
    recent_outages = list(
        outage_model.objects.filter(screen_id__in=screen_ids)
        .filter(
            Q(detected_at__gte=seven_days_ago)
            | Q(resolved_at__gte=seven_days_ago)
            | Q(resolved_at__isnull=True)
        )
        .select_related("screen")
        .order_by("-detected_at")
    )
    outages_by_screen = {}
    for outage in recent_outages:
        outages_by_screen.setdefault(outage.screen_id, []).append(outage)

    monitoring_settings = getattr(school, "schedule_settings", None)
    monitoring_enabled = bool(
        getattr(monitoring_settings, "screen_offline_alerts_enabled", True)
    )
    monitoring_threshold_minutes = int(
        getattr(monitoring_settings, "screen_offline_threshold_minutes", 10) or 10
    )
    monitor_heartbeat = cache.get("screen-monitor:heartbeat")
    monitor_worker_live = False
    monitor_heartbeat_display = "غير متاح"
    if monitor_heartbeat:
        try:
            if isinstance(monitor_heartbeat, str):
                monitor_heartbeat = datetime.fromisoformat(monitor_heartbeat)
            if timezone.is_naive(monitor_heartbeat):
                monitor_heartbeat = timezone.make_aware(monitor_heartbeat)
            monitor_age = max(0, int((now - monitor_heartbeat).total_seconds()))
            monitor_worker_live = monitor_age <= 180
            monitor_heartbeat_display = (
                "قبل أقل من دقيقة"
                if monitor_age < 60
                else f"قبل {max(1, monitor_age // 60)} دقيقة"
            )
        except (TypeError, ValueError, OverflowError):
            monitor_worker_live = False
    screen_rows = []
    for screen in qs:
        last_seen = latest_display_presence(screen)
        bound_at = getattr(screen, "bound_at", None)
        last_activity = last_seen or bound_at
        bound_device = bool((getattr(screen, "bound_device_id", "") or "").strip())
        is_enabled = bool(getattr(screen, "is_active", False))
        is_live = False
        last_seen_seconds = None
        last_seen_display = "لم تتصل بعد"
        last_seen_full = ""
        if last_activity:
            try:
                delta = now - last_activity
                last_seen_seconds = max(0, int(delta.total_seconds()))
                is_live = bool(
                    last_seen
                    and is_enabled
                    and bound_device
                    and last_seen_seconds <= live_threshold_seconds
                )
                local_seen = timezone.localtime(last_activity)
                last_seen_full = local_seen.strftime("%Y-%m-%d %H:%M")
                if last_seen_seconds < 60:
                    last_seen_display = "قبل أقل من دقيقة"
                elif last_seen_seconds < 3600:
                    last_seen_display = f"قبل {max(1, last_seen_seconds // 60)} دقيقة"
                elif last_seen_seconds < 86400:
                    last_seen_display = f"قبل {max(1, last_seen_seconds // 3600)} ساعة"
                else:
                    last_seen_display = local_seen.strftime("%Y-%m-%d %H:%M")
            except Exception:
                is_live = False

        if bound_device:
            linked_count += 1
        if is_live:
            live_count += 1

        if not is_enabled:
            status_key = "disabled"
            status_label = "متوقفة"
            status_hint = "الشاشة غير مفعلة من لوحة التحكم"
            disabled_count += 1
        elif is_live:
            status_key = "live"
            status_label = "متصلة الآن"
            status_hint = "الشاشة تعمل وتستقبل التحديثات"
        elif bound_device and last_seen:
            status_key = "offline"
            status_label = "منقطعة الآن"
            status_hint = "الجهاز مرتبط، لكن لم يصل نبض حديث"
            offline_count += 1
        elif bound_device:
            status_key = "pending"
            status_label = "مرتبطة وجاهزة"
            status_hint = "تم ربط الجهاز؛ سيظهر الاتصال عند أول نبض"
            waiting_count += 1
        else:
            status_key = "waiting"
            status_label = "بانتظار الربط"
            status_hint = "افتح الرابط على التلفاز لربط الشاشة"
            waiting_count += 1

        screen_outages = outages_by_screen.get(screen.pk, [])
        open_outage = next(
            (outage for outage in screen_outages if outage.resolved_at is None),
            None,
        )
        downtime_seconds = 0
        for outage in screen_outages:
            outage_start = outage.last_seen_at or outage.detected_at
            overlap_start = max(outage_start, seven_days_ago)
            overlap_end = min(outage.resolved_at or now, now)
            if overlap_end > overlap_start:
                downtime_seconds += int((overlap_end - overlap_start).total_seconds())
        if is_enabled and bound_device and last_seen and not is_live and open_outage is None:
            fallback_start = max(last_seen, seven_days_ago)
            if now > fallback_start:
                downtime_seconds += int((now - fallback_start).total_seconds())
        period_seconds = 7 * 24 * 60 * 60
        uptime_percent = None
        if last_seen:
            uptime_percent = max(
                0.0,
                min(100.0, round((period_seconds - min(downtime_seconds, period_seconds)) * 100 / period_seconds, 1)),
            )
            uptime_values.append(uptime_percent)

        screen.dashboard_status_key = status_key
        screen.dashboard_status_label = status_label
        screen.dashboard_status_hint = status_hint
        screen.dashboard_last_seen_display = last_seen_display
        screen.dashboard_last_seen_full = last_seen_full
        screen.dashboard_is_live = is_live
        screen.dashboard_bound_device = bound_device
        screen.dashboard_last_seen_seconds = last_seen_seconds
        screen.dashboard_open_outage = open_outage
        screen.dashboard_incidents_7d = len(screen_outages)
        screen.dashboard_uptime_percent = uptime_percent
        screen.dashboard_outage_duration = (
            format_duration(now - (open_outage.last_seen_at or open_outage.detected_at))
            if open_outage
            else ""
        )
        screen.dashboard_outage_cause = (
            open_outage.get_cause_display() if open_outage else ""
        )
        screen.dashboard_outage_detail = (
            (open_outage.cause_detail or "").strip() if open_outage else ""
        )
        # مجموعة موروثة = كل حقول التخصيص فيها فارغة، أي أن الشاشة ما زالت
        # تتبع "تخصيص جميع الشاشات" وتصلها تعديلاته تلقائيًا.
        inherited_groups = set(ScreenDisplayCustomizationForm.inherited_groups_for(screen))
        screen.dashboard_inherited_groups = sorted(inherited_groups)
        screen.dashboard_follows_school_display = (
            len(inherited_groups) == len(ScreenDisplayCustomizationForm.INHERIT_GROUP_FIELDS)
            and not bool(getattr(screen, "logo_override", None))
        )
        screen.dashboard_has_customization = bool(
            not screen.dashboard_follows_school_display
            or (getattr(screen, "occasion_theme", "auto") or "auto") != "auto"
            or not bool(getattr(screen, "show_announcements", True))
            or not bool(getattr(screen, "show_period_classes", True))
            or not bool(getattr(screen, "show_standby", True))
            or not bool(getattr(screen, "show_duty", True))
            or not bool(getattr(screen, "show_excellence", True))
        )
        screen_rows.append(screen)

    recent_incidents = []
    for outage in recent_outages[:8]:
        outage.dashboard_started_at = timezone.localtime(outage.detected_at).strftime(
            "%Y-%m-%d %H:%M"
        )
        outage.dashboard_is_open = outage.resolved_at is None
        outage.dashboard_duration = format_duration(
            (outage.resolved_at or now) - (outage.last_seen_at or outage.detected_at)
        )
        outage.dashboard_cause = outage.get_cause_display()
        recent_incidents.append(outage)

    if max_screens is None:
        screens_remaining = None
    else:
        try:
            screens_remaining = max(int(max_screens) - int(current_count), 0)
        except Exception:
            screens_remaining = 0

    if max_screens is None:
        can_create_screen = True
        show_screen_limit_message = False
        screen_limit_message = None
    else:
        can_create_screen = current_count < int(max_screens)
        show_screen_limit_message = not can_create_screen
        if max_screens <= 0:
            screen_limit_message = "لا يمكن إضافة شاشات لهذه المدرسة (لا يوجد اشتراك نشط)."
        else:
            screen_limit_message = f"لا يمكن إضافة أكثر من {int(max_screens)} شاشة لهذه المدرسة"

    return render(
        request,
        "dashboard/screen_list.html",
        {
            "screens": screen_rows,
            "can_create_screen": can_create_screen,
            "show_screen_limit_message": show_screen_limit_message,
            "screen_limit": None if max_screens is None else int(max_screens),
            "screen_limit_message": screen_limit_message,
            "screens_count": current_count,
            "screens_live_count": live_count,
            "screens_linked_count": linked_count,
            "screens_offline_count": offline_count,
            "screens_waiting_count": waiting_count,
            "screens_disabled_count": disabled_count,
            "screens_attention_count": offline_count + waiting_count + disabled_count,
            "screens_uptime_7d": (
                round(sum(uptime_values) / len(uptime_values), 1)
                if uptime_values
                else None
            ),
            "screen_live_threshold_seconds": live_threshold_seconds,
            "screen_live_threshold_minutes": live_threshold_minutes,
            "monitoring_enabled": monitoring_enabled,
            "monitoring_threshold_minutes": monitoring_threshold_minutes,
            "monitor_worker_live": monitor_worker_live,
            "monitor_heartbeat_display": monitor_heartbeat_display,
            "recent_incidents": recent_incidents,
            "plan_name": plan_name,
            "can_purchase_screen_addon": can_purchase_screen_addon,
            "screens_remaining": screens_remaining,
            "auto_disabled_count": auto_disabled_count,
        },
    )


def _screen_limit_message(max_screens: int) -> str:
    if max_screens <= 0:
        return "لا يمكن إنشاء شاشة بدون اشتراك نشط."
    return f"لا يمكن إنشاء أكثر من {int(max_screens)} شاشة لهذه المدرسة."


def _seed_screen_appearance_overrides(screen, school) -> None:
    """Start new screens with their own visual identity controls enabled."""
    settings_obj, _ = _school_settings_model().objects.get_or_create(
        school=school,
        defaults={"name": school.name},
    )
    theme = (getattr(settings_obj, "theme", "") or "indigo").strip().lower()
    theme = {
        "default": "indigo",
        "boys": "emerald",
        "girls": "rose",
    }.get(theme, theme)
    screen.theme_override = theme
    screen.custom_theme_override = settings_obj.custom_theme
    screen.display_accent_color_override = (
        getattr(settings_obj, "display_accent_color", "")
        or SchoolSettingsForm.THEME_ACCENTS.get(theme, "#6366F1")
    )
    screen.featured_panel_override = (
        getattr(settings_obj, "featured_panel", "")
        or _school_settings_model().FEATURE_PANEL_EXCELLENCE
    )


@manager_required
def screen_create(request):
    display_screen = _display_screen_model()
    school_model = apps.get_model("core", "School")
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    current_count = display_screen.objects.filter(school=school).count()
    max_screens = get_school_max_screens_limit(school)
    if (max_screens is not None) and (current_count >= int(max_screens)):
        messages.warning(request, _screen_limit_message(int(max_screens)))
        return redirect("dashboard:screen_list")

    if request.method == "POST":
        form = DisplayScreenForm(request.POST)
        if form.is_valid():
            # الفحص أعلاه يخدم الواجهة فقط. طلبان متزامنان يمكن أن يجتازاه معًا،
            # لذلك نعيد الفحص داخل معاملة تقفل صف المدرسة: القفل يجعل الإنشاء
            # متسلسلًا لكل مدرسة، فلا تتجاوز أي مدرسة حد باقتها.
            with transaction.atomic():
                school_model.objects.select_for_update().filter(pk=school.pk).first()
                locked_count = display_screen.objects.filter(school=school).count()
                locked_limit = get_school_max_screens_limit(school)
                if (locked_limit is not None) and (locked_count >= int(locked_limit)):
                    messages.warning(request, _screen_limit_message(int(locked_limit)))
                    return redirect("dashboard:screen_list")

                screen = form.save(commit=False)
                screen.school = school
                _seed_screen_appearance_overrides(screen, school)
                screen.save()
            messages.success(
                request,
                "تم إضافة شاشة جديدة.\n\n"
                "تنبيه مهم:\n"
                "- سيتم حفظ الشاشة على أول تلفاز/متصفح يتم فتح الرابط عليه، ولا يمكن فتحها على جهاز آخر إلا بعد فصل الجهاز من لوحة التحكم.\n"
                "- يمكنك تعديل الثيم والمحتوى الظاهر لهذه الشاشة بشكل مستقل من زر تخصيص.",
            )
            return redirect("dashboard:screen_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = DisplayScreenForm()

    return render(request, "dashboard/screen_form.html", {"form": form, "title": "إضافة شاشة"})


@manager_required
def screens_customize_all(request):
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj, _ = _school_settings_model().objects.get_or_create(
        school=school,
        defaults={"name": school.name},
    )
    if request.method == "POST":
        form = SchoolSettingsForm(
            request.POST,
            request.FILES,
            instance=settings_obj,
            user=request.user,
            mode="display",
        )
        if form.is_valid():
            form.save()
            _refresh_school_displays(school)
            messages.success(request, "تم حفظ إعدادات العرض العامة لجميع الشاشات.")
            return redirect("dashboard:screens_customize_all")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = SchoolSettingsForm(
            instance=settings_obj,
            user=request.user,
            mode="display",
        )

    preview_screen = (
        _display_screen_model().objects.filter(school=school, is_active=True).order_by("id").first()
    )
    preview_url = (
        f"/s/{preview_screen.short_code}/?preview=1"
        if preview_screen and preview_screen.short_code
        else None
    )
    logo_url = None
    try:
        logo_url = school.logo.url if school.logo else None
    except Exception:
        pass

    return render(
        request,
        "dashboard/settings.html",
        {
            "form": form,
            "school": school,
            "display_preview_url": preview_url,
            "initial_settings_tab": "appearance",
            "show_display_settings": True,
            "show_account_settings": False,
            "display_scope_title": "تخصيص جميع الشاشات",
            "display_scope_description": "هذه القيم هي الإعداد الافتراضي لكل شاشات المدرسة، ويمكن تجاوزها من أي شاشة بشكل مستقل.",
            "current_logo_url": logo_url,
            "back_to_screens": True,
        },
    )


@manager_required
def screen_edit(request, pk: int):
    display_screen = _display_screen_model()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    screen = get_object_or_404(display_screen, pk=pk, school=school)
    settings_obj, _ = _school_settings_model().objects.get_or_create(
        school=school,
        defaults={"name": school.name},
    )

    if request.method == "POST" and request.POST.get("action") == "reset_display_customization":
        if getattr(screen, "logo_override", None):
            screen.logo_override.delete(save=False)
        screen.logo_override = None
        for override_names in ScreenDisplayCustomizationForm.INHERIT_GROUP_FIELDS.values():
            for override_name in override_names:
                setattr(
                    screen,
                    override_name,
                    None if override_name.endswith(("_speed_override", "_id")) else "",
                )
        screen.occasion_theme = "auto"
        screen.show_announcements = True
        screen.show_period_classes = True
        screen.show_standby = True
        screen.show_duty = True
        screen.show_excellence = True
        screen.save()
        _refresh_school_displays(school, target_screen=screen)
        messages.success(request, f"عادت شاشة ({screen.name}) إلى إعدادات جميع الشاشات.")
        return redirect("dashboard:screen_edit", pk=screen.pk)

    if request.method == "POST":
        form = ScreenDisplayCustomizationForm(
            request.POST,
            request.FILES,
            instance=screen,
            school_settings=settings_obj,
        )
        if form.is_valid():
            screen = form.save()
            _refresh_school_displays(school, target_screen=screen)
            messages.success(
                request,
                f"تم حفظ تخصيص الشاشة ({screen.name}) وإرسال أمر تحديث لها.",
            )
            return redirect("dashboard:screen_list")
        messages.error(request, "الرجاء تصحيح الأخطاء.")
    else:
        form = ScreenDisplayCustomizationForm(instance=screen, school_settings=settings_obj)

    logo_url = None
    try:
        if screen.logo_override:
            logo_url = screen.logo_override.url
        elif school.logo:
            logo_url = school.logo.url
    except Exception:
        pass

    preview_url = f"/s/{screen.short_code}/?preview=1" if screen.short_code else None

    # القيم المعروضة في مفاتيح "اتباع إعداد جميع الشاشات": ما أرسله المدير عند
    # وجود أخطاء، وإلا حالة الشاشة المحفوظة.
    if form.is_bound and hasattr(form.data, "getlist"):
        inherit_selected = list(form.data.getlist("inherit_groups"))
    elif form.is_bound:
        inherit_selected = list(form.data.get("inherit_groups") or ())
    else:
        inherit_selected = ScreenDisplayCustomizationForm.inherited_groups_for(screen)

    return render(
        request,
        "dashboard/settings.html",
        {
            "form": form,
            "screen": screen,
            "school": school,
            "display_preview_url": preview_url,
            "initial_settings_tab": "appearance",
            "show_display_settings": True,
            "show_account_settings": False,
            "is_screen_scope": True,
            "inherit_selected": inherit_selected,
            "display_scope_title": f"تخصيص شاشة: {screen.name}",
            "display_scope_description": "التعديلات هنا تخص هذه الشاشة فقط، ويمكن إعادتها إلى إعدادات جميع الشاشات في أي وقت.",
            "current_logo_url": logo_url,
            "back_to_screens": True,
        },
    )


@manager_required
@require_POST
def screen_refresh_now(request, pk: int):
    """Force a single screen to fetch new data."""
    display_screen = _display_screen_model()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    from schedule.cache_utils import get_schedule_revision_for_school_id

    obj = get_object_or_404(display_screen, pk=pk, school=school)

    token_value = ((getattr(obj, "token", None) or getattr(obj, "api_token", None) or "").strip())
    if not token_value:
        messages.error(request, "تعذر تحديث الشاشة: لا يوجد token صالح.")
        return redirect("dashboard:screen_list")

    token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    try:
        cache.set(
            f"display:force_refresh:{token_hash}",
            "1",
            timeout=_screen_command_ttl_seconds(),
        )
    except Exception:
        pass

    school_id = int(getattr(school, "id", 0) or 0)
    cur_rev = int(get_schedule_revision_for_school_id(school_id) or 0)

    def _broadcast_invalidate_screen_ws(*, screen_id: int, revision: int) -> None:
        try:
            from django.conf import settings

            if not getattr(settings, "DISPLAY_WS_ENABLED", False):
                return
        except Exception:
            return

        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            channel_layer = get_channel_layer()
            if not channel_layer:
                return

            # Every connected display must join its school group. Token-group
            # membership is best-effort and can expire independently on very
            # long-running TV connections, so target the required group and let
            # the consumer enforce the screen id.
            group = school_group_name(school_id)
            async_to_sync(channel_layer.group_send)(
                group,
                {
                    "type": "broadcast_invalidate",
                    "school_id": int(school_id),
                    "target_screen_id": int(screen_id),
                    "revision": int(revision or 0),
                    "reason": "manual_refresh",
                },
            )
        except Exception:
            return

    try:
        transaction.on_commit(
            lambda: _broadcast_invalidate_screen_ws(screen_id=int(obj.pk), revision=cur_rev)
        )
    except Exception:
        try:
            _broadcast_invalidate_screen_ws(screen_id=int(obj.pk), revision=cur_rev)
        except Exception:
            pass

    logger.info(
        "screen_refresh_now school_id=%s screen_id=%s rev=%s",
        int(school_id),
        int(getattr(obj, "id", 0) or 0),
        int(cur_rev),
    )

    messages.success(request, f"تم إرسال أمر تحديث لهذه الشاشة ({obj.name}).")

    next_url = (request.POST.get("next") or "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)

    return redirect("dashboard:screen_list")


@manager_required
@require_POST
def screen_reload_now(request, pk: int):
    """Force a single screen to reload the page (equivalent to pressing F5)."""
    display_screen = _display_screen_model()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    obj = get_object_or_404(display_screen, pk=pk, school=school)

    token_value = ((getattr(obj, "token", None) or getattr(obj, "api_token", None) or "").strip())
    if not token_value:
        messages.error(request, "تعذر إعادة تحميل الشاشة: لا يوجد token صالح.")
        return redirect("dashboard:screen_list")

    school_id = int(getattr(school, "id", 0) or 0)
    _queue_screen_reload(obj, school_id=school_id)

    logger.info(
        "screen_reload_now school_id=%s screen_id=%s",
        int(school_id),
        int(getattr(obj, "id", 0) or 0),
    )

    messages.success(request, f"تم إرسال أمر إعادة تحميل لهذه الشاشة ({obj.name}).")

    next_url = (request.POST.get("next") or "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)

    return redirect("dashboard:screen_list")


@manager_required
@require_POST
def screen_delete(request, pk: int):
    display_screen = _display_screen_model()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    obj = get_object_or_404(display_screen, pk=pk, school=school)
    obj.delete()
    messages.success(request, "تم حذف الشاشة.")
    return redirect("dashboard:screen_list")


@manager_required
@require_POST
def screen_unbind_device(request, pk: int):
    display_screen = _display_screen_model()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    obj = get_object_or_404(display_screen, pk=pk, school=school)

    try:
        display_screen._meta.get_field("bound_device_id")
        display_screen._meta.get_field("bound_at")
    except Exception:
        messages.error(request, "ميزة ربط الأجهزة غير متاحة حالياً.")
        return redirect("dashboard:screen_list")

    display_screen.objects.filter(pk=obj.pk).update(bound_device_id=None, bound_at=None)

    try:
        token_value = ((getattr(obj, "token", None) or getattr(obj, "api_token", None) or "").strip())
        if token_value:
            token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
            cache.delete(f"display:token_map:{token_hash}")
    except Exception:
        pass

    messages.success(request, "تم فصل الجهاز. افتح الرابط على التلفاز الجديد ليتم ربطه تلقائياً.")
    return redirect("dashboard:screen_list")


@manager_required
def request_screen_addon(request):
    """زر/صفحة طلب زيادة شاشات: يفتح تذكرة دعم مُعبأة تلقائيًا."""
    display_screen = _display_screen_model()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    try:
        from subscriptions.utils import school_has_paid_active_subscription

        has_paid_subscription = school_has_paid_active_subscription(school.pk)
    except Exception:
        has_paid_subscription = False
    if not has_paid_subscription:
        messages.warning(request, "فعّل اشتراكًا مدفوعًا أولًا قبل طلب شاشات إضافية.")
        return redirect("dashboard:my_subscription")

    current_count = display_screen.objects.filter(school=school).count()
    max_screens = get_school_max_screens_limit(school)
    plan_name = get_school_effective_plan_label(school) or "—"

    school_name = (getattr(school, "name", "") or "").strip()
    subject = f"طلب زيادة شاشات - {school_name}" if school_name else "طلب زيادة شاشات"
    msg_lines = [
        f"المدرسة: {getattr(school, 'name', '')}",
        f"الخطة الحالية: {plan_name}",
        f"عدد الشاشات الحالية: {current_count}",
        f"الحد الحالي: {'غير محدود' if max_screens is None else int(max_screens)}",
        "",
        "المطلوب:",
        "- عدد الشاشات الإضافية: ",
        "- المدة: (شهر / نصف سنوي / سنوي)",
        "- ملاحظات: ",
    ]
    message_text = "\n".join(msg_lines)

    url = reverse("dashboard:customer_support_ticket_create")
    return redirect(f"{url}?subject={quote(subject)}&message={quote(message_text)}")
