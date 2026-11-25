from datetime import date, timedelta

from django.utils import timezone

from core.models import School, SubscriptionPlan

from .models import SchoolSubscription
from .trials import close_superseded_trials_quietly


def paid_plan_start_date(school_id: int, plan_id: int, *, not_before: date | None = None) -> date:
    """Append to confirmed, finite terms of the same paid plan, including scheduled terms."""
    today = timezone.localdate()
    earliest_start = max(today, not_before or today)
    latest_end = (
        SchoolSubscription.objects.filter(
            school_id=school_id,
            plan_id=plan_id,
            plan__price__gt=0,
            status="active",
            ends_at__gte=earliest_start,
        )
        .order_by("-ends_at")
        .values_list("ends_at", flat=True)
        .first()
    )
    return latest_end + timedelta(days=1) if latest_end is not None else earliest_start


def activate_gateway_plan(
    *, school_id: int, plan: SubscriptionPlan, requested_start: date, notes: str,
) -> SchoolSubscription:
    """Schedule a verified purchase inside the transaction recording its payment."""
    # The bank approval and both gateways use this school lock before assigning dates.
    School.objects.select_for_update().get(pk=school_id)
    starts_at = paid_plan_start_date(school_id, plan.pk, not_before=requested_start)
    subscription, _ = SchoolSubscription.objects.get_or_create(
        school_id=school_id,
        plan=plan,
        starts_at=starts_at,
        defaults={"status": "active", "notes": notes},
    )
    if subscription.status != "active":
        subscription.status = "active"
        changed = ["status", "updated_at"]
        term_days = int(plan.duration_days or 0)
        if term_days > 0 and (subscription.ends_at is None or subscription.ends_at < starts_at):
            subscription.ends_at = starts_at + timedelta(days=term_days)
            changed.append("ends_at")
        subscription.save(update_fields=changed)

    close_superseded_trials_quietly(school_id)
    return subscription
