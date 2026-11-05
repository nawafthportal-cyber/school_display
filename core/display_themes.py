"""Validated presentation values shared by saved themes and the TV renderer."""
from __future__ import annotations

import re

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
BASE_THEMES = ("indigo", "emerald", "rose", "cyan", "amber", "orange", "violet")
FONT_CHOICES = [("ibm", "IBM Plex Sans Arabic"), ("system", "خط الجهاز"), ("arial", "Arial")]
BACKGROUND_CHOICES = [("gradient", "تدرج هادئ"), ("solid", "لون موحد"), ("dots", "نقاط خفيفة")]
COLOR_FIELDS = ("background_color", "card_color", "text_color", "heading_color", "accent_color")


def contrast_ratio(first: str, second: str) -> float:
    def luminance(color):
        components = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in components]
        return sum(x * weight for x, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def theme_payload(theme):
    if theme is None:
        return None
    # Never emit unvalidated CSS, URLs, font declarations or arbitrary JSON.
    if any(not HEX_COLOR.fullmatch(str(getattr(theme, field, ""))) for field in COLOR_FIELDS):
        return None
    return {
        "id": theme.pk, "name": theme.name,
        **{field: getattr(theme, field).upper() for field in COLOR_FIELDS},
        "font": theme.font if theme.font in dict(FONT_CHOICES) else "ibm",
        "font_scale": max(90, min(115, int(theme.font_scale))),
        "card_radius": max(0, min(32, int(theme.card_radius))),
        "background_style": theme.background_style if theme.background_style in dict(BACKGROUND_CHOICES) else "gradient",
    }


def effective_custom_theme(settings_obj, screen=None):
    school_id = getattr(settings_obj, "school_id", None)
    if screen is not None:
        selected = getattr(screen, "custom_theme_override", None)
        if selected is not None:
            return selected if selected.school_id == school_id else None
        if getattr(screen, "theme_override", ""):
            return None
    selected = getattr(settings_obj, "custom_theme", None)
    return selected if selected is not None and selected.school_id == school_id else None
