from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import School, SubscriptionPlan
from .features import effective_feature_subscription, plan_disabled_features, refresh_school_features
from .models import SchoolSubscription, SubscriptionPaymentOperation, SubscriptionScreenAddon

logger = logging.getLogger(__name__)


def sync_school_active(school_id: int) -> None:
    """
    ✅ المصدر الوحيد لتفعيل/تعطيل المدرسة هو subscriptions.SchoolSubscription
    تحدّث School.is_active بناءً على وجود اشتراك ساري.
    """
    today = timezone.localdate()

    has_active = (
        SchoolSubscription.objects.filter(
            school_id=school_id,
            status="active",
            starts_at__lte=today,
        )
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=today))
        .exists()
    )

    # تحديث مباشر بدون تحميل كائن School كامل
    School.objects.filter(id=school_id).update(is_active=has_active)


def _invalidate_access_cache(school_id: int) -> None:
    from .access import invalidate_school_subscription_cache

    invalidate_school_subscription_cache(school_id)


def _current_restrictions(school_id):
    term = effective_feature_subscription(school_id)
    return plan_disabled_features(getattr(term, "plan", None))


def _remember_term_features(sender, instance, raw=False, **kwargs):
    if raw:
        return
    previous_school = (
        sender.objects.filter(pk=instance.pk).values_list("school_id", flat=True).first()
        if instance.pk else None
    )
    instance._previous_feature_state = {
        school_id: _current_restrictions(school_id)
        for school_id in {instance.school_id, previous_school} - {None}
    }


def _update_term_features(instance):
    for school_id, previous in getattr(instance, "_previous_feature_state", {}).items():
        if school_id != instance.school_id:
            sync_school_active(school_id)
            _invalidate_access_cache(school_id)
        if previous != _current_restrictions(school_id):
            transaction.on_commit(lambda school_id=school_id: refresh_school_features(school_id))


def _remember_plan_features(sender, instance, update_fields=None, raw=False, **kwargs):
    instance._features_changed = False
    if raw or not instance.pk:
        return
    updates_switches = update_fields is None or "disabled_features" in update_fields
    updates_price = update_fields is None or "price" in update_fields
    if not (updates_switches or updates_price):
        return
    previous = sender.objects.only("disabled_features", "price").filter(pk=instance.pk).first()
    instance._features_changed = (
        updates_switches and plan_disabled_features(previous) != plan_disabled_features(instance)
    ) or (
        updates_price and previous is not None and bool(previous.price) != bool(instance.price)
    )


def _plan_features_changed(sender, instance, **kwargs):
    if not getattr(instance, "_features_changed", False):
        return
    today = timezone.localdate()
    school_ids = (
        instance.subscriptions.filter(status="active", starts_at__lte=today)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gte=today))
        .order_by()
        .values_list("school_id", flat=True)
        .distinct()
    )
    for school_id in school_ids:
        _invalidate_access_cache(school_id)
        transaction.on_commit(lambda school_id=school_id: refresh_school_features(school_id))


def _safe_sync(sender, instance, **kwargs):
    try:
        if instance.school_id:
            sync_school_active(instance.school_id)
            _invalidate_access_cache(instance.school_id)
            _update_term_features(instance)
    except Exception:
        logger.exception("Failed to sync school.is_active for school_id=%s", getattr(instance, "school_id", None))


def _safe_sync_delete(sender, instance, **kwargs):
    try:
        if instance.school_id:
            sync_school_active(instance.school_id)
            _invalidate_access_cache(instance.school_id)
            _update_term_features(instance)
    except Exception:
        logger.exception("Failed to sync school.is_active after delete for school_id=%s", getattr(instance, "school_id", None))


def _safe_sync_addon(sender, instance, **kwargs):
    """A paid screen add-on changes the effective screen limit."""
    try:
        school_id = getattr(getattr(instance, "subscription", None), "school_id", None)
        if school_id:
            _invalidate_access_cache(school_id)
    except Exception:
        logger.exception("Failed to invalidate access cache for screen addon id=%s", getattr(instance, "id", None))


# الفاتورة تصدر عن بوابتي الدفع المعتمدتين أو اعتماد يدوي لتحويل بنكي.
# ``METHOD_CHOICES`` يحصر الخيارات في الواجهات، لكن
# ``objects.create()`` يتجاوز تحقق الخيارات، فهذا الحارس هو ما يمنع أي مسار
# مستقبلي من إصدار فاتورة بطريقة دفع لم تُعتمد.
INVOICEABLE_METHODS = frozenset({"bank_transfer", "tamara", "moyasar"})


def _safe_create_invoice(sender, instance: SubscriptionPaymentOperation, created: bool, **kwargs):
    if not created:
        return
    try:
        if not instance.amount or instance.amount <= 0:
            return
        if instance.method not in INVOICEABLE_METHODS:
            logger.warning(
                "No invoice issued for payment operation id=%s: method %r is not an "
                "approved invoicing route %s.",
                getattr(instance, "id", None),
                instance.method,
                sorted(INVOICEABLE_METHODS),
            )
            return
        if hasattr(instance, "invoice"):
            return

        from .invoicing import build_invoice_from_operation

        build_invoice_from_operation(instance)
    except Exception:
        logger.exception(
            "Failed to create invoice for payment operation id=%s",
            getattr(instance, "id", None),
        )


def connect_signals():
    from django.db.models.signals import post_save, post_delete, pre_save, pre_delete

    def school_access_changed(sender, instance, **kwargs):
        _invalidate_access_cache(instance.pk)
        from django.db import transaction
        transaction.on_commit(lambda: _invalidate_access_cache(instance.pk))

    post_save.connect(school_access_changed, sender=School, weak=False,
                      dispatch_uid="subscriptions_school_display_access")

    pre_save.connect(_remember_term_features, sender=SchoolSubscription, dispatch_uid="subscription_pre_save_features")
    pre_delete.connect(_remember_term_features, sender=SchoolSubscription, dispatch_uid="subscription_pre_delete_features")
    pre_save.connect(_remember_plan_features, sender=SubscriptionPlan, dispatch_uid="subscription_plan_previous_features")
    post_save.connect(_plan_features_changed, sender=SubscriptionPlan, dispatch_uid="subscription_plan_refresh_features")
    post_save.connect(_safe_sync, sender=SchoolSubscription, dispatch_uid="subscriptions_sync_school_active_save")
    post_delete.connect(_safe_sync_delete, sender=SchoolSubscription, dispatch_uid="subscriptions_sync_school_active_delete")

    post_save.connect(
        _safe_sync_addon,
        sender=SubscriptionScreenAddon,
        dispatch_uid="subscriptions_invalidate_access_on_addon_save",
    )
    post_delete.connect(
        _safe_sync_addon,
        sender=SubscriptionScreenAddon,
        dispatch_uid="subscriptions_invalidate_access_on_addon_delete",
    )

    post_save.connect(
        _safe_create_invoice,
        sender=SubscriptionPaymentOperation,
        dispatch_uid="subscriptions_create_invoice_on_payment_operation_save",
    )
