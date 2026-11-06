"""School-owned presentation overrides for the shared occasion registry."""
from dataclasses import replace

from core import occasions
from core.display_themes import HEX_COLOR

PATTERNS = {
    "solid": "none",
    "dots": "radial-gradient(var(--occasion-highlight) 1.5px, transparent 1.5px)",
    "lines": "repeating-linear-gradient(45deg, transparent 0 22px, var(--occasion-highlight) 23px 25px)",
}
PATTERN_CHOICES = [("original", "زخرفة المناسبة الأصلية"), ("solid", "بدون زخرفة"), ("dots", "نقاط"), ("lines", "خطوط مائلة")]
COLOR_FIELDS = ("deep", "accent", "highlight", "soft")


def customization_payload(instance):
    if not occasions.get(instance.occasion_key):
        return None
    if any(not HEX_COLOR.fullmatch(getattr(instance, key, "")) for key in COLOR_FIELDS):
        return None
    return {
        "name": instance.name, "tagline": instance.tagline,
        **{key: getattr(instance, key).upper() for key in COLOR_FIELDS},
        "pattern": instance.pattern if instance.pattern in dict(PATTERN_CHOICES) else "original",
    }


def customizations_for_school(school_id):
    if not school_id:
        return {}
    from schedule.models import SchoolOccasionTheme
    return {
        item.occasion_key: payload
        for item in SchoolOccasionTheme.objects.filter(school_id=school_id)
        if (payload := customization_payload(item)) is not None
    }


def cards_for_school(school_id):
    customizations = customizations_for_school(school_id)
    cards = []
    for original in occasions.all_occasions():
        custom = customizations.get(original.key)
        item = original
        if custom:
            item = replace(original, name=custom["name"], theme_name=custom["name"],
                           tagline=custom["tagline"], **{key: custom[key] for key in COLOR_FIELDS},
                           pattern_css=PATTERNS.get(custom["pattern"], original.pattern_css))
        card = item.as_card_dict()
        card.update(original_name=original.name, is_custom=bool(custom))
        cards.append(card)
    return cards
