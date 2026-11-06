from __future__ import annotations

import logging

from django.db.models import Q
from django.utils import timezone

from core.models import School
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


def _safe_sync(sender, instance, **kwargs):
    try:
        if instance.school_id:
            sync_school_active(instance.school_id)
            _invalidate_access_cache(instance.school_id)
    except Exception:
        logger.exception("Failed to sync school.is_active for school_id=%s", getattr(instance, "school_id", None))


def _safe_sync_delete(sender, instance, **kwargs):
    try:
        if instance.school_id:
            sync_school_active(instance.school_id)
            _invalidate_access_cache(instance.school_id)
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
    from django.db.models.signals import post_save, post_delete

    def school_access_changed(sender, instance, **kwargs):
        _invalidate_access_cache(instance.pk)
        from django.db import transaction
        transaction.on_commit(lambda: _invalidate_access_cache(instance.pk))

    post_save.connect(school_access_changed, sender=School, weak=False,
                      dispatch_uid="subscriptions_school_display_access")

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
