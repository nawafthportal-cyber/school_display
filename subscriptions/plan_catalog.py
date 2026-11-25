from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .features import PLAN_FEATURES, feature_rows, plan_disabled_features

def _duration_details(duration_days: int | None) -> tuple[str, str, int | None]:
    days = int(duration_days or 0)
    if days <= 0:
        return "مدة مفتوحة", "للمدة كاملة", None
    if 27 <= days <= 31:
        return "شهر واحد", "شهرياً", 1
    if 80 <= days <= 100:
        return "3 أشهر", "كل 3 أشهر", 3
    if 150 <= days <= 200:
        return "6 أشهر", "نصف سنوي", 6
    if 330 <= days <= 370:
        return "سنة كاملة", "سنوياً", 12
    return f"{days} يوماً", f"لمدة {days} يوماً", None


def plan_card(plan) -> dict:
    """Return the single public/customer representation of a subscription plan."""
    duration_days = int(getattr(plan, "duration_days", 0) or 0)
    duration_label, period_label, cycle_months = _duration_details(duration_days)
    price = Decimal(str(getattr(plan, "price", 0) or 0))
    monthly_equivalent = None
    if price > 0 and cycle_months and cycle_months > 1:
        monthly_equivalent = (price / Decimal(cycle_months)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

    code = str(getattr(plan, "code", "") or "")
    name = str(getattr(plan, "name", "") or "")
    is_trial = code == "free-trial" or (price == 0 and "تجرب" in name)
    max_screens = getattr(plan, "max_screens", None)
    show_badge = bool(getattr(plan, "show_card_badge", True))
    show_duration = bool(getattr(plan, "show_card_duration", True))
    show_monthly = bool(getattr(plan, "show_monthly_equivalent", True))
    show_screens = bool(getattr(plan, "show_screen_limit", True))

    automatic_badge = (
        "تجربة مجانية"
        if is_trial
        else "الأكثر طلباً"
        if bool(getattr(plan, "is_featured", False))
        else "مجانية"
        if price == 0
        else "متاحة للاشتراك"
    )
    badge_text = str(getattr(plan, "card_badge_text", "") or "").strip() or automatic_badge
    duration_text = str(getattr(plan, "card_duration_text", "") or "").strip()
    if not duration_text:
        duration_text = f"مدة الباقة: {duration_label}"
    price_caption = str(getattr(plan, "card_price_caption", "") or "").strip()
    if not price_caption:
        price_caption = f"ريال سعودي / {period_label}"

    custom_monthly_text = str(getattr(plan, "card_monthly_text", "") or "").strip()
    if custom_monthly_text:
        monthly_text = custom_monthly_text
    elif monthly_equivalent is not None:
        monthly_text = f"يعادل {monthly_equivalent} ر.س شهرياً"
    else:
        monthly_text = ""

    raw_features = str(getattr(plan, "card_features", "") or "")
    features = [line.strip() for line in raw_features.splitlines() if line.strip()]
    disabled = plan_disabled_features(plan)
    if disabled:
        features = [label for key, label in PLAN_FEATURES.items() if key not in disabled]
    custom_screen_text = str(getattr(plan, "card_screen_text", "") or "").strip()
    if custom_screen_text:
        screen_text = custom_screen_text
    elif max_screens not in (None, ""):
        screen_text = f"الشاشات: {int(max_screens)}"
    else:
        screen_text = "الشاشات: غير محدودة"

    return {
        "id": getattr(plan, "pk", None),
        "code": code,
        "name": name,
        "description": str(getattr(plan, "description", "") or ""),
        "price": str(price),
        "duration_days": duration_days,
        "duration_label": duration_label,
        "period_label": period_label,
        "monthly_equivalent": str(monthly_equivalent) if monthly_equivalent is not None else None,
        "max_screens": int(max_screens) if max_screens not in (None, "") else None,
        "is_featured": bool(getattr(plan, "is_featured", False)),
        "is_trial": is_trial,
        "is_free": price == 0,
        "badge_text": badge_text if show_badge else "",
        "duration_text": duration_text if show_duration else "",
        "price_caption": price_caption,
        "monthly_text": monthly_text if show_monthly else "",
        "features": features,
        "feature_states": feature_rows(disabled),
        "has_feature_restrictions": bool(disabled),
        "screen_text": screen_text if show_screens else "",
        "cta_text": str(getattr(plan, "card_cta_text", "") or "").strip() or "اطلب هذه الباقة",
    }


def plan_cards(plans) -> list[dict]:
    return [plan_card(plan) for plan in plans]
