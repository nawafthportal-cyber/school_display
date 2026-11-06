import re

from django.db.models import Prefetch

from core.models import UserProfile

from .recipients import school_matches_subscription_filter, subscription_recipient_schools


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def whatsapp_phone_number(value):
    """Convert local Saudi mobiles or explicit international numbers to wa.me digits."""
    raw = str(value or "").strip().translate(_ARABIC_DIGITS)
    if not raw or not re.fullmatch(r"\+?[0-9\s().-]+", raw):
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    international = raw.startswith("+") or digits.startswith("00")
    if digits.startswith("00"):
        digits = digits[2:]
    if re.fullmatch(r"05[0-9]{8}", digits):
        return "966" + digits[1:] if not international else None
    if re.fullmatch(r"5[0-9]{8}", digits) and not international:
        return "966" + digits
    if digits.startswith("966"):
        return digits if re.fullmatch(r"9665[0-9]{8}", digits) else None
    if international and re.fullmatch(r"[1-9][0-9]{7,14}", digits):
        return digits
    return None


def whatsapp_school_contacts():
    managers = UserProfile.objects.filter(
        user__is_active=True, user__is_staff=False, user__is_superuser=False,
    ).select_related("user").order_by("user__username", "id")
    schools = subscription_recipient_schools().prefetch_related(
        Prefetch("users", queryset=managers, to_attr="whatsapp_managers")
    )
    cards = []
    for school in schools:
        contacts = []
        seen = set()
        for manager in school.whatsapp_managers:
            phone = whatsapp_phone_number(manager.mobile)
            if phone and phone not in seen:
                seen.add(phone)
                contacts.append({"name": manager.user.get_full_name() or manager.user.username, "phone": phone})
        cards.append({
            "school": school,
            "active": school.mail_has_active_subscription,
            "expired_trial": school_matches_subscription_filter(school, "expired_trial"),
            "contacts": contacts,
        })
    return cards
