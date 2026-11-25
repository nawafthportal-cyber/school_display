from __future__ import annotations

import logging

from django.core.cache import cache
from django.db.models import BooleanField, Case, F, Q, Value, When
from django.utils import timezone


logger = logging.getLogger(__name__)
PLAN_FEATURES = {
    "announcements": "الإعلانات المدرسية",
    "emergency_alerts": "تنبيهات الطوارئ",
    "standby": "حصص الانتظار",
    "duty": "المناوبات والإشراف",
    "excellence": "لوحة التكريم",
    "excel_import": "استيراد الجداول والبيانات من Excel",
    "smart_import": "الاستيراد الذكي من الصور والملفات",
    "screen_customization": "تخصيص إعدادات كل شاشة",
}

DISPLAY_CONTENT_FEATURES = frozenset({"announcements", "emergency_alerts", "standby", "duty", "excellence"})
DISPLAY_FEATURES = DISPLAY_CONTENT_FEATURES | {"screen_customization"}

# The monthly, half-year and annual plans use the same set of supported switches.
# Keep routes with multiple actions together, including deletion and AJAX.
DASHBOARD_FEATURE_ROUTES = {
    name: feature
    for feature, names in {
        "announcements": ("ann_list", "ann_create", "ann_edit", "ann_delete"),
        "emergency_alerts": (
            "emergency_alert_list", "emergency_alert_create",
            "emergency_alert_cancel", "emergency_alert_delete",
        ),
        "standby": ("standby_list", "standby_create", "standby_delete"),
        "duty": ("duty_list", "duty_create", "duty_edit", "duty_delete", "duty_teacher_search"),
        "excellence": ("exc_list", "exc_create", "exc_edit", "exc_delete"),
        "excel_import": ("school_data_import_template",),
        "smart_import": ("school_document_import",),
        "screen_customization": ("screen_edit", "occasion_assign"),
    }.items()
    for name in names
}


def plan_disabled_features(plan) -> frozenset[str]:
    values = getattr(plan, "disabled_features", ())
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(value for value in values if isinstance(value, str) and value in PLAN_FEATURES)


def feature_rows(disabled, *, has_subscription=True):
    return [
        {"key": key, "label": label, "enabled": has_subscription and key not in disabled}
        for key, label in PLAN_FEATURES.items()
    ]


def display_feature_policy(disabled):
    return ",".join(sorted(DISPLAY_FEATURES & disabled)) or "-"


def effective_feature_subscription(school_id, *, on_date=None):
    """Resolve the school's plan. Paid access takes precedence over a trial."""
    from .models import SchoolSubscription

    today = on_date or timezone.localdate()
    return (
        SchoolSubscription.objects.filter(school_id=school_id, status="active", starts_at__lte=today)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=today))
        .select_related("plan")
        .alias(paid=Case(
            When(plan__price__gt=0, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ))
        .order_by("-paid", F("ends_at").desc(nulls_first=True), "-starts_at", "-id")
        .first()
    )


def _feature_cache_key(school_id):
    return f"subscriptions:features:v1:{int(school_id)}:{timezone.localdate().isoformat()}"


def invalidate_school_feature_cache(school_id):
    if school_id:
        cache.delete(_feature_cache_key(school_id))


def school_disabled_features(school_id) -> frozenset[str]:
    if not school_id:
        return frozenset()

    key = _feature_cache_key(school_id)
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return frozenset(cached)

    subscription = effective_feature_subscription(school_id)
    # Existing subscription gates own access for schools without a current term.
    disabled = plan_disabled_features(getattr(subscription, "plan", None))
    try:
        cache.set(key, tuple(sorted(disabled)), timeout=300)
    except Exception:
        logger.debug("school_feature_cache_write_failed school_id=%s", school_id)
    return disabled


def subscription_feature_rows(subscription):
    return feature_rows(
        plan_disabled_features(getattr(subscription, "plan", None)),
        has_subscription=subscription is not None,
    )


def refresh_school_features(school_id):
    """Refresh policy and ask live screens to pick up new display settings."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from django.conf import settings
    from display.ws_groups import school_group_name
    from schedule.signals import _bump_and_invalidate

    try:
        invalidate_school_feature_cache(school_id)
        _bump_and_invalidate(
            school_id=school_id,
            reason="subscription_features_changed",
            model_label="core.SubscriptionPlan",
        )
        layer = get_channel_layer()
        if layer is not None and getattr(settings, "DISPLAY_WS_ENABLED", False):
            async_to_sync(layer.group_send)(
                school_group_name(school_id),
                {"type": "broadcast_reload", "school_id": school_id},
            )
    except Exception:
        logger.exception("feature_display_refresh_failed school_id=%s", school_id)
