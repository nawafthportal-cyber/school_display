from django.contrib import admin

from core.models import SubscriptionPlan
from dashboard.forms import SubscriptionPlanForm
from .models import (
    SchoolSubscription,
    SubscriptionAuditLog,
    SubscriptionInvoice,
    SubscriptionEmailNotification,
    SubscriptionPaymentOperation,
    SubscriptionRefund,
    SubscriptionScreenAddon,
    SubscriptionRequest,
    TamaraCheckout,
    MoyasarCheckout,
)
from .refunds import record_refund, refundable_amount


# إدارة خطط الاشتراك
@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    """
    Admin بسيط لعرض خطط الاشتراك الموجودة في core.SubscriptionPlan
    بدون افتراض أي حقول غير مضمونة.
    """
    list_display = ("id", "name", "price", "duration_days", "max_screens", "is_featured", "is_active")
    search_fields = ("name", "code", "description", "card_features")
    list_filter = ("is_active", "is_featured")
    ordering = ("sort_order", "price", "id")
    form = SubscriptionPlanForm
    exclude = ("disabled_features",)


# إدارة اشتراكات المدارس
@admin.register(SchoolSubscription)
class SchoolSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("school", "plan", "starts_at", "ends_at", "status", "is_active")
    list_filter = ("status", "plan")
    search_fields = ("school__name", "school__slug")
    autocomplete_fields = ("school", "plan")


@admin.register(SubscriptionScreenAddon)
class SubscriptionScreenAddonAdmin(admin.ModelAdmin):
    list_display = (
        "subscription",
        "screens_added",
        "pricing_cycle",
        "validity_days",
        "pricing_strategy",
        "bundle_price",
        "unit_price",
        "proration_factor",
        "total_price",
        "starts_at",
        "ends_at",
        "status",
    )
    list_filter = ("status",)
    search_fields = ("subscription__school__name", "subscription__school__slug")
    autocomplete_fields = ("subscription",)


@admin.register(SubscriptionRequest)
class SubscriptionRequestAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "request_type",
        "plan",
        "amount",
        "requested_starts_at",
        "status",
        "created_at",
        "processed_by",
    )
    list_filter = ("status", "request_type", "plan")
    search_fields = ("school__name", "school__slug", "transfer_note")
    autocomplete_fields = ("school", "plan", "created_by", "processed_by", "approved_subscription")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("معلومات الطلب", {
            "fields": ("school", "created_by", "request_type", "plan", "requested_starts_at")
        }),
        ("الدفع", {
            "fields": ("amount", "receipt_image", "transfer_note")
        }),
        ("الحالة", {
            "fields": ("status", "admin_note", "processed_by", "processed_at", "approved_subscription")
        }),
        ("التواريخ", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",)
        }),
    )


@admin.register(SubscriptionPaymentOperation)
class SubscriptionPaymentOperationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "school",
        "plan",
        "amount",
        "still_refundable",
        "method",
        "source",
        "created_by",
        "created_at",
    )
    list_filter = ("method", "source", "created_at")
    search_fields = ("school__name", "plan__name", "created_by__username", "note")
    autocomplete_fields = ("school", "subscription", "plan", "created_by")

    @admin.display(description="القابل للاسترداد")
    def still_refundable(self, obj):
        return refundable_amount(obj)


@admin.register(TamaraCheckout)
class TamaraCheckoutAdmin(admin.ModelAdmin):
    list_display = (
        "merchant_reference",
        "school",
        "plan",
        "amount",
        "status",
        "tamara_order_id",
        "created_at",
    )
    list_filter = ("status", "request_type", "created_at")
    search_fields = ("merchant_reference", "tamara_order_id", "school__name")
    autocomplete_fields = ("school", "plan", "created_by", "subscription", "payment_operation")
    readonly_fields = (
        "merchant_reference",
        "tamara_order_id",
        "checkout_id",
        "last_event",
        "processed_at",
        "created_at",
        "updated_at",
    )


@admin.register(MoyasarCheckout)
class MoyasarCheckoutAdmin(admin.ModelAdmin):
    list_display = (
        "merchant_reference",
        "school",
        "plan",
        "amount",
        "status",
        "live_mode",
        "payment_id",
        "created_at",
    )
    list_filter = ("status", "live_mode", "request_type", "created_at")
    search_fields = ("merchant_reference", "payment_id", "school__name")
    autocomplete_fields = ("school", "plan", "created_by", "subscription", "payment_operation")
    readonly_fields = (
        "merchant_reference",
        "payment_id",
        "live_mode",
        "last_event",
        "processed_at",
        "created_at",
        "updated_at",
    )


@admin.register(SubscriptionInvoice)
class SubscriptionInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "school",
        "plan",
        "amount",
        "payment_method",
        "issued_at",
        "created_at",
    )
    list_filter = ("payment_method", "currency", "issued_at")
    search_fields = ("invoice_number", "school__name", "plan__name")
    autocomplete_fields = ("school", "subscription", "plan", "operation")
    readonly_fields = ("invoice_number", "created_at")


@admin.register(SubscriptionEmailNotification)
class SubscriptionEmailNotificationAdmin(admin.ModelAdmin):
    list_display = (
        "event_type",
        "recipient",
        "subscription",
        "status",
        "attempts",
        "sent_at",
        "created_at",
    )
    list_filter = ("event_type", "status", "created_at")
    search_fields = ("recipient", "dedupe_key", "subscription__school__name")
    readonly_fields = (
        "event_type",
        "subscription",
        "invoice",
        "recipient",
        "reminder_days",
        "dedupe_key",
        "attempts",
        "available_at",
        "locked_at",
        "sent_at",
        "last_error",
        "created_at",
        "updated_at",
    )


@admin.register(SubscriptionRefund)
class SubscriptionRefundAdmin(admin.ModelAdmin):
    list_display = (
        "school",
        "amount",
        "reason",
        "status",
        "revokes_access",
        "created_by",
        "created_at",
    )
    list_filter = ("status", "reason", "revokes_access", "created_at")
    search_fields = (
        "school__name",
        "gateway_reference",
        "notes",
        "operation__note",
    )
    autocomplete_fields = ("operation", "school", "subscription")
    readonly_fields = ("created_at", "completed_at", "created_by")

    def save_model(self, request, obj, form, change):
        """Route new refunds through the service so effects and audit apply."""
        if change:
            super().save_model(request, obj, form, change)
            return

        refund = record_refund(
            obj.operation,
            amount=obj.amount,
            reason=obj.reason,
            status=obj.status,
            notes=obj.notes,
            gateway_reference=obj.gateway_reference,
            revoke_access=obj.revokes_access,
            actor=request.user,
            request=request,
        )
        obj.pk = refund.pk


@admin.register(SubscriptionAuditLog)
class SubscriptionAuditLogAdmin(admin.ModelAdmin):
    """Read-only by design: the trail is evidence, not editable data."""

    list_display = ("created_at", "action", "school", "actor_label", "amount", "summary")
    list_filter = ("action", "created_at")
    search_fields = ("school__name", "actor_label", "summary")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


from .models import DiscountCode, DiscountRedemption  # noqa: E402


@admin.register(DiscountCode)
class DiscountCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "discount_type",
        "percent",
        "amount",
        "valid_from",
        "valid_until",
        "max_uses",
        "used_count_display",
        "is_active",
    )
    list_filter = ("discount_type", "is_active")
    search_fields = ("code", "notes")
    filter_horizontal = ("plans",)
    readonly_fields = ("created_by", "created_at", "updated_at")

    @admin.display(description="المستخدم")
    def used_count_display(self, obj):
        return obj.used_count

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(DiscountRedemption)
class DiscountRedemptionAdmin(admin.ModelAdmin):
    """Read-only: redemptions are payment evidence, not editable data."""

    list_display = ("created_at", "discount_code", "school", "amount_discounted", "checkout")
    list_filter = ("discount_code",)
    search_fields = ("discount_code__code", "school__name")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
