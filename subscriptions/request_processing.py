"""Atomic, idempotent transitions for manually reviewed bank transfers."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import School
from .access import invalidate_school_subscription_cache
from .invoicing import build_invoice_from_operation
from .models import SchoolSubscription, SubscriptionInvoice, SubscriptionPaymentOperation, SubscriptionRequest
from .plan_terms import paid_plan_start_date


@transaction.atomic
def process_request(*, request_id, action, actor, admin_note=""):
    obj = SubscriptionRequest.objects.select_for_update().get(pk=request_id)
    if action not in {"approve", "reject", "under_review"}:
        raise ValidationError("إجراء غير صالح.")
    target = {"approve": "approved", "reject": "rejected", "under_review": "under_review"}[action]
    if obj.status in {"approved", "rejected"}:
        if obj.status == target:
            return obj, False
        raise ValidationError("تمت معالجة هذا الطلب بالفعل؛ حدّث الصفحة للاطلاع على حالته.")

    if action == "approve":
        # Serialize requests for the same school as well as repeated submissions.
        School.objects.select_for_update().get(pk=obj.school_id)
        starts_at = obj.requested_starts_at
        if obj.amount > 0:
            # A second request for the same quoted period needs review before moving its dates.
            if SubscriptionRequest.objects.filter(
                school_id=obj.school_id,
                plan_id=obj.plan_id,
                requested_starts_at=starts_at,
                status="approved",
                approved_subscription__isnull=False,
            ).exists():
                raise ValidationError("يوجد طلب معتمد لنفس الباقة وتاريخ البداية المطلوب. راجع الدفعة قبل اعتماد طلب آخر.")
            starts_at = paid_plan_start_date(obj.school_id, obj.plan_id, not_before=starts_at)
        sub, created = SchoolSubscription.objects.get_or_create(
            school_id=obj.school_id, plan_id=obj.plan_id, starts_at=starts_at,
            defaults={"status": "active", "notes": f"Approved from request #{obj.pk}"},
        )
        if not created and sub.status != "active":
            raise ValidationError("يوجد اشتراك ملغى أو غير نشط بنفس الباقة وتاريخ البداية؛ راجع تاريخ بدء الطلب قبل الاعتماد.")
        if sub.approved_from_requests.exclude(pk=obj.pk).filter(status="approved").exists():
            raise ValidationError("هذا الاشتراك مرتبط بطلب معتمد سابقاً؛ راجع فترة التفعيل قبل تسجيل دفعة أخرى.")
        if obj.amount > 0:
            op, _ = SubscriptionPaymentOperation.objects.get_or_create(
                subscription_request=obj,
                defaults={"school_id": obj.school_id, "subscription": sub, "plan_id": obj.plan_id,
                          "amount": obj.amount, "method": "bank_transfer", "source": "request",
                          "created_by": actor, "note": f"Approved from request #{obj.pk}"},
            )
            if not SubscriptionInvoice.objects.filter(operation=op, html_snapshot__gt="").exists():
                # A signal may fail quietly; approval must still require an invoice.
                if SubscriptionInvoice.objects.filter(operation=op).exists():
                    raise ValidationError("تعذر إكمال الفاتورة؛ لم يُعتمد الطلب. راجع سجل الأخطاء.")
                build_invoice_from_operation(op)
        obj.approved_subscription = sub
        transaction.on_commit(lambda: invalidate_school_subscription_cache(obj.school_id))

    obj.status = target
    obj.admin_note = admin_note
    obj.processed_by = actor
    obj.processed_at = timezone.now()
    obj.save(update_fields=["status", "admin_note", "processed_by", "processed_at", "approved_subscription", "updated_at"])
    return obj, True
