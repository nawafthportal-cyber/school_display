from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from core.models import School
from subscriptions.models import SchoolSubscription


SUBSCRIPTION_RECIPIENT_CHOICES = [
    ("", "جميع المدارس"),
    ("active", "المدارس ذات الاشتراك النشط"),
    ("inactive", "المدارس التي ليس لديها اشتراك نشط"),
    ("expired_trial", "المدارس التي توقفت بعد التجربة المجانية"),
]


def subscription_recipient_schools():
    today = timezone.localdate()
    subscriptions = SchoolSubscription.objects.filter(school_id=OuterRef("pk"))
    return School.objects.annotate(
        mail_has_active_subscription=Exists(
            subscriptions.filter(status="active", starts_at__lte=today).filter(
                Q(ends_at__isnull=True) | Q(ends_at__gte=today)
            )
        ),
        mail_has_ended_trial=Exists(
            subscriptions.filter(
                plan__price=0, starts_at__lte=today,
                status__in=["active", "expired", "cancelled"],
            ).filter(Q(status__in=["expired", "cancelled"]) | Q(ends_at__lt=today))
        ),
        mail_has_paid_subscription=Exists(
            subscriptions.filter(
                plan__price__gt=0, starts_at__lte=today,
                status__in=["active", "expired", "cancelled"],
            )
        ),
    ).order_by("name", "id")


def school_matches_subscription_filter(school, status):
    if status == "active":
        return school.mail_has_active_subscription
    if status == "inactive":
        return not school.mail_has_active_subscription
    if status == "expired_trial":
        return (
            not school.mail_has_active_subscription
            and school.mail_has_ended_trial
            and not school.mail_has_paid_subscription
        )
    return True
