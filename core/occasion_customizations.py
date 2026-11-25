"""School-owned presentation overrides for the shared occasion registry."""
from dataclasses import replace
import re

from django.db import models

from core import occasions
from core.display_themes import HEX_COLOR

PATTERNS = {
    "solid": "none",
    "dots": "radial-gradient(var(--occasion-highlight) 1.5px, transparent 1.5px)",
    "lines": "repeating-linear-gradient(45deg, transparent 0 22px, var(--occasion-highlight) 23px 25px)",
}
PATTERN_CHOICES = [("original", "زخرفة المناسبة الأصلية"), ("solid", "بدون زخرفة"), ("dots", "نقاط"), ("lines", "خطوط مائلة")]
COLOR_FIELDS = ("deep", "accent", "highlight", "soft")
SCHOOL_EVENT_KEY = re.compile(r"event_[0-9a-f]{16}")
SCHOOL_EVENT = occasions.Occasion(
    key="school_event", name="مناسبة مدرسية", theme_name="مناسبة مدرسية",
    title="اسم المناسبة", body="", level="info", duration_hours=8,
    mark="✦", badge_icon="✦", symbols=("✦", "◇"), tagline="",
    deep="#123A42", accent="#257B77", highlight="#F1D596", soft="#F4FAF8",
    pattern_css=PATTERNS["dots"],
)


class OccasionSelectionField(models.CharField):
    """Keep registry choices and also accept events owned by this school."""

    def validate(self, value, model_instance):
        if SCHOOL_EVENT_KEY.fullmatch(value or "") and model_instance.school_id:
            from schedule.models import SchoolOccasionTheme
            if SchoolOccasionTheme.objects.filter(school_id=model_instance.school_id, occasion_key=value).exists():
                return
        super().validate(value, model_instance)


def customization_payload(instance):
    school_event = SCHOOL_EVENT_KEY.fullmatch(instance.occasion_key)
    if not school_event and not occasions.get(instance.occasion_key):
        return None
    if any(not HEX_COLOR.fullmatch(getattr(instance, key, "")) for key in COLOR_FIELDS):
        return None
    return {
        "name": instance.name, "tagline": instance.tagline,
        **{key: getattr(instance, key).upper() for key in COLOR_FIELDS},
        "pattern": instance.pattern if instance.pattern in dict(PATTERN_CHOICES) else "original",
        **({"identity": SCHOOL_EVENT.as_theme_dict()} if school_event else {}),
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


def resolve_occasion(key, customizations):
    original = occasions.get(key)
    custom = customizations.get(key)
    if not custom:
        return original
    if not original:
        original = replace(SCHOOL_EVENT, key=key, title=custom["name"], body=custom["tagline"])
    return replace(original, name=custom["name"], theme_name=custom["name"], tagline=custom["tagline"],
                   **{name: custom[name] for name in COLOR_FIELDS},
                   pattern_css=PATTERNS.get(custom["pattern"], original.pattern_css))


def choices_for_school(school_id, base_choices):
    custom = customizations_for_school(school_id)
    return [(key, custom.get(key, {}).get("name", label)) for key, label in base_choices] + [
        (key, value["name"]) for key, value in custom.items() if SCHOOL_EVENT_KEY.fullmatch(key)
    ]


def cards_for_school(school_id):
    custom = customizations_for_school(school_id)
    keys = [*occasions.OCCASIONS, *(key for key in custom if SCHOOL_EVENT_KEY.fullmatch(key))]
    cards = []
    for key in keys:
        item = resolve_occasion(key, custom)
        original = occasions.get(key)
        cards.append(dict(item.as_card_dict(), original_name=original.name if original else "",
                          is_custom=key in custom, school_event=original is None))
    return cards
