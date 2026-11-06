"""Shared imports, constants and helpers for the dashboard view modules.

``__all__`` deliberately lists the underscore-prefixed helpers too, so the
view modules can pull the whole shared layer in with a single star import
and keep referring to these names exactly as they always have.
"""

from __future__ import annotations

from datetime import datetime, date, time, timedelta
import hashlib
import csv
import io
import math
import logging
import os
import re
from urllib.parse import urlencode
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from django import forms
from django.apps import apps
from django.conf import settings as dj_settings
from django.contrib import messages
from django.contrib.auth import (
    login,
    get_user_model,
)
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied, FieldError
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.db.models import Max, Q, Sum, Count, Prefetch
from django.db.models.functions import TruncMonth
from django.db.utils import OperationalError, ProgrammingError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, NoReverseMatch, get_resolver
from django.templatetags.static import static as build_static_url
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

# Centralized access and permission policy. Aliases preserve the names used by
# legacy code while the implementation lives outside this view module.
from .access import (
    get_active_school_or_redirect,
    get_or_create_user_profile as _get_or_create_profile,
    has_system_permission,
    is_system_staff_user,
    safe_next_url as _safe_next_url,
)
from .auth_views import change_password, login_view, logout_view
from .decorators import (
    manager_or_system_permission_required,
    manager_required,
    superuser_required,
    system_permission_required,
    system_staff_required,
)
from .support_views import (
    customer_support_ticket_create,
    customer_support_ticket_detail,
    customer_support_tickets,
    system_support_ticket_create,
    system_support_ticket_detail,
    system_support_tickets,
)
from .views_screens import (
    request_screen_addon,
    screen_create,
    screen_delete,
    screen_edit,
    screen_list,
    screen_refresh_now,
    screen_reload_now,
    screen_unbind_device,
)
from .forms import (
    SchoolSettingsForm,
    SelfServiceSchoolCreateForm,
    DayScheduleForm,
    LessonForm,
    AnnouncementForm,
    EmergencyAlertForm,
    ExcellenceForm,
    StandbyForm,
    DutyAssignmentForm,
    SchoolSubscriptionForm,
    SystemUserCreateForm,
    SystemEmployeeCreateForm,
    SystemEmployeeUpdateForm,
    SystemUserUpdateForm,
    PeriodFormSet,
    BreakFormSet,
    SubscriptionPlanForm,
    SubscriptionScreenAddonForm,
    SubscriptionRenewalRequestForm,
    SubscriptionNewRequestForm,
)
from core.models import SubscriptionPlan, SystemEmployeeProfile
from core.system_access import (
    PERMISSION_LABELS,
    ROLE_PRESETS,
    grouped_permission_definitions,
    normalize_permission_keys,
    role_label,
)
from core import occasions
from core.display_presence import display_is_live, latest_display_presence, live_display_count
from core.email_verification import user_email_is_verified
from subscriptions.pricing import MAX_EXTRA_SCREENS, prorated_screen_addon_price
from core.two_factor import get_enabled_config
from subscriptions.plan_catalog import plan_card, plan_cards

logger = logging.getLogger(__name__)

from schedule.cache_utils import (
    bump_schedule_revision_for_school_id,
    bump_schedule_revision_for_school_id_debounced,
    get_schedule_revision_for_school_id,
    invalidate_display_snapshot_cache_for_school_id,
)
from schedule.time_engine import build_day_snapshot
from .excel_import import (
    apply_import,
    build_import_impact,
    build_template_bytes,
    parse_workbook,
    undo_import,
)

if TYPE_CHECKING:
    pass

UserModel = get_user_model()

WEEKDAY_MAP = {
    1: "الاثنين",
    2: "الثلاثاء",
    3: "الأربعاء",
    4: "الخميس",
    5: "الجمعة",
    6: "السبت",
    7: "الأحد",
}

# أيام الأسبوع كاملة (الأحد → السبت) بترتيب الأسبوع السعودي
SCHOOL_WEEK = [
    (7, "الأحد"),
    (1, "الاثنين"),
    (2, "الثلاثاء"),
    (3, "الأربعاء"),
    (4, "الخميس"),
    (5, "الجمعة"),
    (6, "السبت"),
]
SCHOOL_WEEKDAY_IDS = [w for w, _ in SCHOOL_WEEK]

# أيام عطلة نهاية الأسبوع الافتراضية (تُنشأ بـ is_active=False)
WEEKEND_WEEKDAY_IDS = {5, 6}

# ترتيب العرض: الأحد أولاً ثم الاثنين … السبت
WEEKDAY_SORT = {7: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}


# ======================
# URL helpers (حل NoReverseMatch للـ namespaces)
# ======================

def _namespace_exists(ns: str) -> bool:
    try:
        return ns in (get_resolver().namespace_dict or {})
    except Exception:
        return False


def _safe_reverse(name: str, *, kwargs: dict | None = None, fallback: str | None = None) -> str:
    """
    reverse آمن: يرجع رابط '#' بدل كسر الصفحة عند NoReverseMatch
    """
    try:
        return reverse(name, kwargs=kwargs)
    except NoReverseMatch:
        if fallback:
            try:
                return reverse(fallback, kwargs=kwargs)
            except NoReverseMatch:
                return "#"
        return "#"


def _invalidate_display_cache(school, *, force_bump: bool = False):
    """
    Helper: Bump schedule revision, invalidate display cache, and broadcast
    WebSocket invalidation after any data change.
    Call this after saving/deleting lessons, announcements, excellence, standby, etc.
    """
    try:
        school_id = int(getattr(school, 'id', 0) or 0)
        if not school_id:
            return

        did_bump = bump_schedule_revision_for_school_id_debounced(school_id=school_id)
        if force_bump and not did_bump:
            forced_rev = bump_schedule_revision_for_school_id(school_id)
            did_bump = forced_rev is not None

        new_rev = int(get_schedule_revision_for_school_id(school_id) or 0)
        invalidate_display_snapshot_cache_for_school_id(school_id)

        # Force active screens to fetch once on next /status poll and clear stale
        # server-rendered display context cache keys.
        try:
            DisplayScreen = DisplayScreenModel()
            screens = DisplayScreen.objects.filter(school_id=school_id, is_active=True).only("token")
        except Exception:
            screens = []

        for screen in screens:
            token_value = (getattr(screen, "token", "") or "").strip()
            if not token_value:
                continue

            try:
                cache.delete(f"display_ctx:{token_value}")
                cache.delete(f"display_ctx:{token_value}:rev:{new_rev}")
            except Exception:
                pass

            try:
                token_hash = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
                cache.set(f"display:force_refresh:{token_hash}", "1", timeout=120)
            except Exception:
                pass

        try:
            from schedule.signals import _broadcast_invalidate_ws
            transaction.on_commit(
                lambda _sid=school_id, _rev=new_rev: _broadcast_invalidate_ws(_sid, _rev)
            )
        except Exception:
            logger.warning(
                "WS broadcast import/schedule failed for school_id=%s", school_id
            )

        if not did_bump:
            logger.info(
                "schedule_revision debounce skip in dashboard invalidation school_id=%s force_bump=%s",
                school_id,
                bool(force_bump),
            )
    except Exception:
        logger.exception("_invalidate_display_cache failed for school=%s", school)


# ======================
# Model loader (حل ImportError نهائياً)
# ======================

@lru_cache(maxsize=128)
def _get_model(app_label: str, model_name: str):
    return apps.get_model(app_label, model_name)


def _get_model_first(*candidates: tuple[str, str]):
    """
    جرّب أكثر من مكان للموديل لتفادي تغيّر بنية التطبيقات.
    (مُثبت على هيكل مشروع school_display الحالي)
    """
    last_err = None
    for app_label, model_name in candidates:
        try:
            return _get_model(app_label, model_name)
        except Exception as e:
            last_err = e
    raise LookupError(
        f"تعذر العثور على الموديل من المرشحين: {candidates}. آخر خطأ: {last_err}"
    )


@lru_cache(maxsize=32)
def SchoolModel():
    return _get_model_first(("core", "School"))


@lru_cache(maxsize=32)
def UserProfileModel():
    return _get_model_first(("core", "UserProfile"))


@lru_cache(maxsize=32)
def SchoolSettingsModel():
    return _get_model_first(("schedule", "SchoolSettings"))


@lru_cache(maxsize=32)
def SchoolClassModel():
    return _get_model_first(("schedule", "SchoolClass"))


@lru_cache(maxsize=32)
def SubjectModel():
    return _get_model_first(("schedule", "Subject"))


@lru_cache(maxsize=32)
def TeacherModel():
    return _get_model_first(("schedule", "Teacher"))


@lru_cache(maxsize=32)
def DayScheduleModel():
    return _get_model_first(("schedule", "DaySchedule"))


@lru_cache(maxsize=32)
def PeriodModel():
    return _get_model_first(("schedule", "Period"))


@lru_cache(maxsize=32)
def BreakModel():
    return _get_model_first(("schedule", "Break"))


@lru_cache(maxsize=32)
def ClassLessonModel():
    return _get_model_first(("schedule", "ClassLesson"))


@lru_cache(maxsize=32)
def AnnouncementModel():
    return _get_model_first(("notices", "Announcement"))


@lru_cache(maxsize=32)
def ExcellenceModel():
    return _get_model_first(("notices", "Excellence"))


@lru_cache(maxsize=32)
def StandbyAssignmentModel():
    return _get_model_first(("standby", "StandbyAssignment"))


@lru_cache(maxsize=32)
def DutyAssignmentModel():
    return _get_model_first(("schedule", "DutyAssignment"))


@lru_cache(maxsize=32)
def DisplayScreenModel():
    return _get_model_first(("core", "DisplayScreen"))


# ======================
# Helpers
# ======================

def _model_has_field(model, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)
        return True
    except Exception:
        return False


def _join_unique_msgs(msgs: list[str]) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for m in msgs:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            ordered.append(m)
    return " | ".join(ordered)


def _collect_form_errors(*forms_or_formsets) -> str:
    msgs: list[str] = []
    for obj in forms_or_formsets:
        if obj is None:
            continue

        # Form
        if hasattr(obj, "errors") and hasattr(obj, "fields"):
            if obj.errors:
                for field, errs in obj.errors.items():
                    if field == "__all__":
                        for e in errs:
                            msgs.append(str(e))
                    else:
                        label = obj.fields.get(field).label if field in obj.fields else field
                        for e in errs:
                            msgs.append(f"{label}: {e}")

        # FormSet
        if hasattr(obj, "forms") and hasattr(obj, "non_form_errors"):
            for f in getattr(obj, "forms", []):
                if getattr(f, "errors", None):
                    for field, errs in f.errors.items():
                        if field == "__all__":
                            for e in errs:
                                msgs.append(str(e))
                        else:
                            label = f.fields.get(field).label if field in f.fields else field
                            for e in errs:
                                msgs.append(f"{label}: {e}")
            for e in obj.non_form_errors():
                msgs.append(str(e))

    return _join_unique_msgs(msgs)


def _to_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return int(default)


def _rev_manager(obj, preferred: str, fallback: str):
    mgr = getattr(obj, preferred, None)
    if mgr is None:
        mgr = getattr(obj, fallback, None)
    return mgr


def _parse_hhmm_or_hhmmss(s: str) -> time:
    raw = (s or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    raise ValueError("صيغة الوقت غير صحيحة. استخدم HH:MM أو HH:MM:SS.")


def _time_to_hhmm(value: time | None) -> str:
    if value is None:
        return ""
    return value.strftime("%H:%M")


def _minutes_diff_wrap(start_t: time, end_t: time) -> int:
    start_total = (start_t.hour * 60) + start_t.minute
    end_total = (end_t.hour * 60) + end_t.minute
    diff = end_total - start_total
    if diff < 0:
        diff += 24 * 60
    return diff


def _build_day_autofill_seed(day) -> dict[str, Any]:
    seed: dict[str, Any] = {
        "target_periods_count": int(getattr(day, "periods_count", 0) or 0),
        "start_time": "",
        "period_minutes": "",
        "gap_minutes": "",
        "break_after": "",
        "break_minutes": "",
        "has_actual_values": False,
    }

    periods_mgr = _rev_manager(day, "periods", "period_set")
    breaks_mgr = _rev_manager(day, "breaks", "break_set")

    periods = list(periods_mgr.all()) if periods_mgr is not None else []
    periods.sort(key=lambda p: (int(getattr(p, "index", 0) or 0), getattr(p, "starts_at", time.min)))

    breaks = list(breaks_mgr.all().order_by("starts_at")) if breaks_mgr is not None else []

    if not periods:
        return seed

    seed["has_actual_values"] = True

    first_period = periods[0]
    seed["start_time"] = _time_to_hhmm(getattr(first_period, "starts_at", None))

    durations = [
        _minutes_diff_wrap(p.starts_at, p.ends_at)
        for p in periods
        if getattr(p, "starts_at", None) and getattr(p, "ends_at", None)
    ]
    if durations:
        seed["period_minutes"] = max(1, int(durations[0]))

    intervals: list[int] = []
    for idx in range(len(periods) - 1):
        current_end = getattr(periods[idx], "ends_at", None)
        next_start = getattr(periods[idx + 1], "starts_at", None)
        if current_end and next_start:
            intervals.append(_minutes_diff_wrap(current_end, next_start))

    if breaks:
        first_break = breaks[0]
        seed["break_minutes"] = int(getattr(first_break, "duration_min", 0) or 0)

        break_after = 0
        for pos, period in enumerate(periods, start=1):
            if getattr(period, "ends_at", None) == getattr(first_break, "starts_at", None):
                break_after = pos
                break
        if break_after == 0:
            best_idx = 0
            best_gap = None
            b_start = getattr(first_break, "starts_at", None)
            if b_start:
                for pos, period in enumerate(periods, start=1):
                    end_t = getattr(period, "ends_at", None)
                    if end_t is None:
                        continue
                    gap_val = _minutes_diff_wrap(end_t, b_start)
                    if best_gap is None or gap_val < best_gap:
                        best_gap = gap_val
                        best_idx = pos
            break_after = best_idx
        seed["break_after"] = int(max(0, break_after))
    else:
        seed["break_after"] = 0
        seed["break_minutes"] = 0

    if intervals:
        gap_candidates = [max(0, int(v)) for v in intervals]
        break_after = int(seed.get("break_after") or 0)
        break_minutes = int(seed.get("break_minutes") or 0)
        if break_minutes > 0 and 1 <= break_after <= len(intervals):
            adjusted_gap = intervals[break_after - 1] - break_minutes
            gap_candidates.append(max(0, int(adjusted_gap)))
        seed["gap_minutes"] = min(gap_candidates) if gap_candidates else 0
    else:
        seed["gap_minutes"] = 0

    return seed


def _classes_qs_from_settings(settings_obj):
    """
    بعض المشاريع تسمي علاقة الفصول: settings.school_classes
    وبعضها: settings.classes أو schoolclass_set
    نخليها مرنة حتى لا تتكسر الصفحات.
    """
    SchoolClass = SchoolClassModel()
    if settings_obj is None:
        return SchoolClass.objects.none()

    for preferred, fallback in (("school_classes", "schoolclass_set"), ("classes", "schoolclass_set")):
        mgr = _rev_manager(settings_obj, preferred, fallback)
        if mgr is not None:
            try:
                return mgr.all()
            except Exception:
                continue

    try:
        return SchoolClass.objects.filter(settings=settings_obj)
    except Exception:
        return SchoolClass.objects.none()


def _dashboard_help_image(step_key: str, fallback_path: str) -> str:
    screenshot_dir = os.path.join("img", "dashboard-help", "real")
    candidate_names = [
        f"{step_key}.png",
        f"{step_key}.webp",
        f"{step_key}.jpg",
        f"{step_key}.jpeg",
    ]

    search_roots: list[str] = []
    for static_dir in getattr(dj_settings, "STATICFILES_DIRS", []) or []:
        search_roots.append(str(static_dir))

    base_static_dir = os.path.join(str(getattr(dj_settings, "BASE_DIR", "")), "static")
    if base_static_dir:
        search_roots.append(base_static_dir)

    seen_roots: set[str] = set()
    for root in search_roots:
        normalized_root = os.path.normpath(root)
        if normalized_root in seen_roots:
            continue
        seen_roots.add(normalized_root)

        for filename in candidate_names:
            abs_path = os.path.join(normalized_root, screenshot_dir, filename)
            try:
                if os.path.exists(abs_path):
                    rel_path = "/".join(["img", "dashboard-help", "real", filename])
                    return build_static_url(rel_path)
            except Exception:
                continue

    return build_static_url(fallback_path)


def _build_dashboard_onboarding_context(request, school, settings_obj=None):
    SchoolSettings = SchoolSettingsModel()
    Subject = SubjectModel()
    Teacher = TeacherModel()
    Period = PeriodModel()
    DisplayScreen = DisplayScreenModel()

    settings_obj = settings_obj or SchoolSettings.objects.filter(school=school).first()

    screens_qs = DisplayScreen.objects.filter(school=school)
    screens_count = screens_qs.count()
    launch_screen = screens_qs.filter(is_active=True).order_by("id").first()
    launch_screen_url = (
        # Opened from the dashboard, so it is a preview: looking at the board
        # from here must never take the device slot away from the TV.
        reverse("website:short_display", args=[launch_screen.short_code]) + "?preview=1"
        if launch_screen is not None and getattr(launch_screen, "short_code", "")
        else reverse("dashboard:screen_list")
    )
    classes_count = _classes_qs_from_settings(settings_obj).count() if settings_obj else 0
    subjects_count = Subject.objects.filter(school=school).count()
    teachers_count = Teacher.objects.filter(school=school).count()
    periods_count = Period.objects.filter(day__settings__school=school).count()

    theme_value = (getattr(settings_obj, "theme", "") or "").strip()
    accent_value = (getattr(settings_obj, "display_accent_color", "") or "").strip()
    featured_panel_value = (getattr(settings_obj, "featured_panel", "") or "").strip()
    featured_panel_label = dict(getattr(SchoolSettings, "FEATURE_PANEL_CHOICES", [])).get(
        featured_panel_value,
        "لوحة الشرف",
    )
    settings_customized = bool(accent_value or (theme_value and theme_value != "default"))

    def _step_status(is_complete: bool, *, optional: bool = False):
        if is_complete:
            return ("مكتمل", "emerald")
        if optional:
            return ("اختياري", "amber")
        return ("ابدأ الآن", "slate")

    # The old checklist ended at "choose a theme", so it could read 100% while
    # nothing was on the TV at all. Completion has to mean what the manager came
    # here for: a screen that is actually playing.
    bound_screens = [
        screen
        for screen in screens_qs.filter(is_active=True)
        if (getattr(screen, "bound_device_id", "") or "").strip()
    ]
    screen_is_live = any(display_is_live(screen) for screen in bound_screens)
    if screen_is_live:
        activation_metric = "تعرض الآن"
    elif bound_screens:
        activation_metric = "مرتبطة بلا اتصال"
    else:
        activation_metric = "لم تُشغَّل بعد"

    screens_status, screens_tone = _step_status(screens_count > 0)
    data_status, data_tone = _step_status(classes_count > 0 and subjects_count > 0 and teachers_count > 0)
    schedule_status, schedule_tone = _step_status(periods_count > 0)
    settings_status, settings_tone = _step_status(settings_customized, optional=True)
    activate_status, activate_tone = _step_status(screen_is_live)

    setup_steps = [
        {
            "key": "screens",
            "title": "إنشاء شاشة عرض",
            "eyebrow": "الخطوة 1",
            "description": "أنشئ شاشة جديدة لتحصل على رابط التشغيل الذي ستفتحه على التلفاز أو الشاشة الذكية داخل المدرسة.",
            "image_url": _dashboard_help_image("screens", "img/dashboard-help/create-screen.svg"),
            "cta_url": reverse("dashboard:screen_list"),
            "cta_label": "الذهاب إلى الشاشات",
            "metric_value": str(screens_count),
            "metric_label": "شاشة مسجلة",
            "status_label": screens_status,
            "status_tone": screens_tone,
            "is_complete": screens_count > 0,
            "required": True,
            "tips": [
                "اضغط على شاشة جديدة ثم احفظ الشاشة.",
                "انسخ الرابط المختصر وافتحه على التلفاز.",
                "إذا أردت نقلها لجهاز آخر استخدم فك ارتباط الجهاز أولاً.",
            ],
        },
        {
            "key": "school-data",
            "title": "إدخال بيانات الفصول والمواد والمعلمين",
            "eyebrow": "الخطوة 2",
            "description": "جهّز البنية الأساسية للجدول بإضافة الفصول ثم المواد ثم المعلمين أو المعلمات من صفحة واحدة.",
            "image_url": _dashboard_help_image("school-data", "img/dashboard-help/school-data.svg"),
            "cta_url": reverse("dashboard:school_data"),
            "cta_label": "إدارة بيانات المدرسة",
            "metric_value": f"{classes_count}/{subjects_count}/{teachers_count}",
            "metric_label": "فصول / مواد / معلمون",
            "status_label": data_status,
            "status_tone": data_tone,
            "is_complete": classes_count > 0 and subjects_count > 0 and teachers_count > 0,
            "required": True,
            "tips": [
                "ابدأ بالفصول لأن الجدول يعتمد عليها.",
                "أضف المواد الرئيسية قبل توزيع الحصص اليومية.",
                "أدخل المعلمين بنفس الأسماء التي تريد ظهورها على الشاشة.",
            ],
        },
        {
            "key": "schedule",
            "title": "ضبط توقيت الحصص والأيام",
            "eyebrow": "الخطوة 3",
            "description": "حدد أيام الدراسة وأوقات بداية ونهاية الحصص، ثم افتح الجدول اليومي لتوزيع المادة والمعلم على كل حصة.",
            "image_url": _dashboard_help_image("schedule", "img/dashboard-help/day-schedule.svg"),
            "cta_url": reverse("dashboard:days_list"),
            "cta_label": "ضبط الأيام والحصص",
            "metric_value": str(periods_count),
            "metric_label": "حصة مضبوطة",
            "status_label": schedule_status,
            "status_tone": schedule_tone,
            "is_complete": periods_count > 0,
            "required": True,
            "tips": [
                "فعّل الأيام الدراسية وألغِ أيام الإجازة.",
                "من داخل كل يوم اضبط عدد الحصص والأوقات.",
                "بعد ذلك افتح الجدول اليومي لإسناد المادة والمعلم لكل توقيت.",
            ],
        },
        {
            "key": "activate",
            "title": "تشغيل الشاشة على التلفاز",
            "eyebrow": "الخطوة 4",
            "description": (
                "افتح رابط الشاشة على التلفاز — أو اربطه برمز من صفحة الربط — "
                "ولا تُعدّ الخطوة مكتملة حتى يصل الجهاز فعليًا."
            ),
            "image_url": _dashboard_help_image("activate", "img/dashboard-help/create-screen.svg"),
            "cta_url": reverse("dashboard:screen_pairing"),
            "cta_label": "ربط التلفاز برمز",
            "metric_value": activation_metric,
            "metric_label": "حالة التشغيل",
            "status_label": activate_status,
            "status_tone": activate_tone,
            "is_complete": screen_is_live,
            "required": True,
            "tips": [
                "أسهل طريقة: افتح school-display.com/tv على التلفاز وأدخل الرمز الظاهر في اللوحة.",
                "زر «معاينة» في صفحة الشاشات للاطلاع فقط ولا يحجز الشاشة عن التلفاز.",
                "إن ظهرت رسالة «مفعّلة على جهاز آخر»، اضغط فك ارتباط الجهاز ثم أعد المحاولة.",
            ],
        },
        {
            "key": "settings",
            "title": "اختيار لون الثيم والكرت المميز",
            "eyebrow": "إعداد إضافي",
            "description": "من صفحة الإعدادات تستطيع تخصيص لون الثيم واختيار الكرت البارز في الشاشة بين لوحة التميز أو الإشراف والمناوبة.",
            "image_url": _dashboard_help_image("settings", "img/dashboard-help/theme-settings.svg"),
            "cta_url": reverse("dashboard:settings"),
            "cta_label": "فتح الإعدادات",
            "metric_value": featured_panel_label,
            "metric_label": "الكرت الحالي",
            "status_label": settings_status,
            "status_tone": settings_tone,
            "is_complete": settings_customized,
            "required": False,
            "tips": [
                "اختر اللون الذي يناسب هوية المدرسة بصريًا.",
                "حدد هل الكرت المميز هو التميز أو الإشراف والمناوبة.",
                "أي تعديل هنا ينعكس مباشرة على شاشة العرض.",
            ],
        },
    ]

    required_steps = [step for step in setup_steps if step["required"]]
    completed_required = sum(1 for step in required_steps if step["is_complete"])
    total_required = len(required_steps)
    progress_percent = int(round((completed_required / total_required) * 100)) if total_required else 0
    next_step = next((step for step in required_steps if not step["is_complete"]), None)

    return {
        "setup_steps": setup_steps,
        "setup_summary": {
            "completed_required": completed_required,
            "total_required": total_required,
            "progress_percent": progress_percent,
            "next_step_title": next_step["title"] if next_step else "تم تجهيز الأساسيات",
            "next_step_url": next_step["cta_url"] if next_step else launch_screen_url,
            "next_step_cta_label": next_step["cta_label"] if next_step else "افتح شاشة مدرستك الآن",
            "launch_screen_url": launch_screen_url,
            "guide_url": reverse("dashboard:help_getting_started"),
        },
    }


# ======================
# مصادقة ولوحة المدير
# ======================


def _admin_school_form_class():
    School = SchoolModel()

    class AdminSchoolForm(forms.ModelForm):
        class Meta:
            model = School

            fields = ["name", "slug", "school_type", "is_active", "display_suspended"]

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fields["is_active"].disabled = True
            self.fields["is_active"].help_text = "محسوب من الاشتراك؛ استخدم الإيقاف الإداري للتحكم في العرض دون تغيير السجلات المالية."
            if "school_type" in self.fields:
                self.fields["school_type"].required = True
                # خيار توضيحي بدل "---------"
                choices = list(self.fields["school_type"].choices)
                if choices and choices[0][0] in ("", None):
                    choices[0] = ("", "اختر نوع المدرسة")
                else:
                    choices = [("", "اختر نوع المدرسة")] + choices
                self.fields["school_type"].choices = choices

    return AdminSchoolForm

def _admin_model_field_name(model, *field_names: str) -> str | None:
    """Return the first existing concrete field name from a compatibility list."""
    for field_name in field_names:
        try:
            model._meta.get_field(field_name)
            return field_name
        except Exception:
            continue
    return None

def _admin_order_by_existing(model, queryset, *candidates: str):
    fields: list[str] = []
    for candidate in candidates:
        field_name = candidate[1:] if candidate.startswith("-") else candidate
        if _admin_model_field_name(model, field_name):
            fields.append(candidate)
    if fields:
        return queryset.order_by(*fields)
    return queryset.order_by("-id")

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
                "No subscription model available (subscriptions/core). Subscription admin views may be degraded."
            )
            return None

def _get_subscription_model():
    return _get_subscription_model_robust()

def _get_screen_addon_model():
    try:
        return apps.get_model("subscriptions", "SubscriptionScreenAddon")
    except Exception:
        return None

def _get_subscription_request_model():
    try:
        return apps.get_model("subscriptions", "SubscriptionRequest")
    except Exception:
        return None


__all__ = [
    'datetime',
    'date',
    'time',
    'timedelta',
    'hashlib',
    'csv',
    'io',
    'math',
    'logging',
    'os',
    're',
    'urlencode',
    'lru_cache',
    'TYPE_CHECKING',
    'Any',
    'forms',
    'apps',
    'dj_settings',
    'messages',
    'login',
    'get_user_model',
    'login_required',
    'user_passes_test',
    'PermissionDenied',
    'FieldError',
    'cache',
    'Paginator',
    'transaction',
    'ProtectedError',
    'Max',
    'Q',
    'Sum',
    'Count',
    'Prefetch',
    'TruncMonth',
    'OperationalError',
    'ProgrammingError',
    'HttpResponse',
    'JsonResponse',
    'get_object_or_404',
    'redirect',
    'render',
    'reverse',
    'NoReverseMatch',
    'get_resolver',
    'build_static_url',
    'timezone',
    'get_random_string',
    'url_has_allowed_host_and_scheme',
    'never_cache',
    'require_POST',
    'get_active_school_or_redirect',
    '_get_or_create_profile',
    'has_system_permission',
    'is_system_staff_user',
    '_safe_next_url',
    'change_password',
    'login_view',
    'logout_view',
    'manager_or_system_permission_required',
    'manager_required',
    'superuser_required',
    'system_permission_required',
    'system_staff_required',
    'customer_support_ticket_create',
    'customer_support_ticket_detail',
    'customer_support_tickets',
    'system_support_ticket_create',
    'system_support_ticket_detail',
    'system_support_tickets',
    'request_screen_addon',
    'screen_create',
    'screen_delete',
    'screen_edit',
    'screen_list',
    'screen_refresh_now',
    'screen_reload_now',
    'screen_unbind_device',
    'SchoolSettingsForm',
    'SelfServiceSchoolCreateForm',
    'DayScheduleForm',
    'LessonForm',
    'AnnouncementForm',
    'EmergencyAlertForm',
    'ExcellenceForm',
    'StandbyForm',
    'DutyAssignmentForm',
    'SchoolSubscriptionForm',
    'SystemUserCreateForm',
    'SystemEmployeeCreateForm',
    'SystemEmployeeUpdateForm',
    'SystemUserUpdateForm',
    'PeriodFormSet',
    'BreakFormSet',
    'SubscriptionPlanForm',
    'SubscriptionScreenAddonForm',
    'SubscriptionRenewalRequestForm',
    'SubscriptionNewRequestForm',
    'SubscriptionPlan',
    'SystemEmployeeProfile',
    'PERMISSION_LABELS',
    'ROLE_PRESETS',
    'grouped_permission_definitions',
    'normalize_permission_keys',
    'role_label',
    'occasions',
    'display_is_live',
    'latest_display_presence',
    'live_display_count',
    'user_email_is_verified',
    'MAX_EXTRA_SCREENS',
    'prorated_screen_addon_price',
    'get_enabled_config',
    'plan_card',
    'plan_cards',
    'logger',
    'bump_schedule_revision_for_school_id',
    'bump_schedule_revision_for_school_id_debounced',
    'get_schedule_revision_for_school_id',
    'invalidate_display_snapshot_cache_for_school_id',
    'build_day_snapshot',
    'apply_import',
    'build_import_impact',
    'build_template_bytes',
    'parse_workbook',
    'undo_import',
    'UserModel',
    'WEEKDAY_MAP',
    'SCHOOL_WEEK',
    'SCHOOL_WEEKDAY_IDS',
    'WEEKEND_WEEKDAY_IDS',
    'WEEKDAY_SORT',
    '_namespace_exists',
    '_safe_reverse',
    '_invalidate_display_cache',
    '_get_model',
    '_get_model_first',
    'SchoolModel',
    'UserProfileModel',
    'SchoolSettingsModel',
    'SchoolClassModel',
    'SubjectModel',
    'TeacherModel',
    'DayScheduleModel',
    'PeriodModel',
    'BreakModel',
    'ClassLessonModel',
    'AnnouncementModel',
    'ExcellenceModel',
    'StandbyAssignmentModel',
    'DutyAssignmentModel',
    'DisplayScreenModel',
    '_model_has_field',
    '_join_unique_msgs',
    '_collect_form_errors',
    '_to_int',
    '_rev_manager',
    '_parse_hhmm_or_hhmmss',
    '_time_to_hhmm',
    '_minutes_diff_wrap',
    '_build_day_autofill_seed',
    '_classes_qs_from_settings',
    '_dashboard_help_image',
    '_build_dashboard_onboarding_context',
    '_admin_school_form_class',
    '_admin_model_field_name',
    '_admin_order_by_existing',
    '_get_subscription_model_robust',
    '_get_subscription_model',
    '_get_screen_addon_model',
    '_get_subscription_request_model',
]
