"""Call inside a transaction before removing an active platform owner."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError


def protect_owner_change(user_id, *, active=False, superuser=False):
    User = get_user_model()
    # Every owner-removal path acquires these locks in the same order.
    owners = list(User.objects.select_for_update().filter(is_active=True, is_superuser=True).order_by("pk"))
    if any(owner.pk == user_id for owner in owners) and not (active and superuser) and len(owners) < 2:
        raise ValidationError("لا يمكن تعطيل آخر مالك نشط للمنصة أو إزالة صلاحياته.")
