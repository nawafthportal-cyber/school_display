from django.contrib import admin
from django.utils import timezone

from .models import TelegramAlert


@admin.register(TelegramAlert)
class TelegramAlertAdmin(admin.ModelAdmin):
    actions = ("retry_failed",)

    @admin.action(description="إعادة محاولة التنبيهات الفاشلة المحددة", permissions=["change"])
    def retry_failed(self, request, queryset):
        count = queryset.filter(status=TelegramAlert.Status.FAILED).update(
            status=TelegramAlert.Status.PENDING,
            attempts=0,
            available_at=timezone.now(),
            locked_at=None,
            last_error="",
        )
        self.message_user(request, f"أُعيدت جدولة {count} تنبيهات فاشلة.")

    list_display = (
        "event_type",
        "status",
        "attempts",
        "created_at",
        "sent_at",
    )
    list_filter = ("status", "event_type", "created_at")
    search_fields = ("dedupe_key", "message", "last_error")
    readonly_fields = (
        "event_type",
        "dedupe_key",
        "message",
        "action_url",
        "action_label",
        "status",
        "attempts",
        "available_at",
        "locked_at",
        "sent_at",
        "telegram_message_id",
        "last_error",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False
