"""Why a display screen went dark.

The monitor used to send one sentence for every outage: "the screen has not
connected". That forced whoever read the alert to do the diagnosis. This module
collects the signals we already have — the WebSocket close code, a farewell
beacon from the television, and how many other screens in the same school fell
silent at the same moment — and turns them into a named cause with a confidence
level, so the alert says what happened instead of asking.

Signals are cached rather than written to the database: they are hints with a
one-day shelf life, and losing them must never cost a write on the display path.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone


SIGNAL_TTL_SECONDS = 36 * 60 * 60

# Cause taxonomy ------------------------------------------------------------
CAUSE_PLATFORM = "platform"
CAUSE_SCHOOL_CLOSED = "school_closed"
CAUSE_SCHOOL_NETWORK = "school_network"
CAUSE_DEVICE_OFF = "device_off"
CAUSE_NETWORK_DROP = "network_drop"
CAUSE_WS_TIMEOUT = "ws_timeout"
CAUSE_BINDING_LOST = "binding_lost"
CAUSE_NEVER_CONNECTED = "never_connected"
CAUSE_UNKNOWN = "unknown"

CAUSE_CHOICES = (
    (CAUSE_PLATFORM, "عطل في المنصة"),
    (CAUSE_SCHOOL_CLOSED, "المدرسة مغلقة (إجازة)"),
    (CAUSE_SCHOOL_NETWORK, "انقطاع إنترنت أو كهرباء المدرسة"),
    (CAUSE_DEVICE_OFF, "جهاز العرض مطفأ أو أُغلقت الصفحة"),
    (CAUSE_NETWORK_DROP, "انقطاع مفاجئ في الشبكة"),
    (CAUSE_WS_TIMEOUT, "توقف الاتصال اللحظي"),
    (CAUSE_BINDING_LOST, "الشاشة مرتبطة بجهاز آخر"),
    (CAUSE_NEVER_CONNECTED, "لم يتم تشغيل الشاشة بعد"),
    (CAUSE_UNKNOWN, "سبب غير محدد"),
)

CONFIDENCE_CONFIRMED = "confirmed"
CONFIDENCE_LIKELY = "likely"
CONFIDENCE_CHOICES = (
    (CONFIDENCE_CONFIRMED, "مؤكد"),
    (CONFIDENCE_LIKELY, "مرجّح"),
)

SCOPE_SCREEN = "screen"
SCOPE_SCHOOL = "school"
SCOPE_PLATFORM = "platform"
SCOPE_CHOICES = (
    (SCOPE_SCREEN, "شاشة واحدة"),
    (SCOPE_SCHOOL, "المدرسة كاملة"),
    (SCOPE_PLATFORM, "المنصة"),
)

# What each cause means, and what the school can actually do about it.
CAUSE_PRESENTATION = {
    CAUSE_PLATFORM: {
        "icon": "🛠",
        "title": "خلل مؤقت في خدمة العرض",
        "school_note": "المشكلة من طرفنا وليست من الشاشة أو الإنترنت لديكم، وفريقنا يعمل على معالجتها الآن.",
        "hints": ["لا حاجة لأي إجراء من المدرسة.", "ستعود الشاشات تلقائيًا فور انتهاء المعالجة."],
    },
    CAUSE_SCHOOL_CLOSED: {
        "icon": "🏖",
        "title": "المدرسة في إجازة",
        "school_note": "الشاشات مطفأة لأن اليوم مسجَّل إجازة في تقويم المدرسة، وهذا سلوك متوقّع لا عطل.",
        "hints": [
            "لا حاجة لأي إجراء.",
            "ستعود الشاشات تلقائيًا صباح أول يوم دوام.",
        ],
    },
    CAUSE_SCHOOL_NETWORK: {
        "icon": "🌐",
        "title": "انقطاع الإنترنت أو الكهرباء عن المدرسة",
        "school_note": "توقفت جميع شاشات المدرسة في اللحظة نفسها تقريبًا، وهذا يشير إلى مصدر مشترك لا إلى عطل في شاشة بعينها.",
        "hints": [
            "تحقق من جهاز الراوتر ومن اتصال الإنترنت في المدرسة.",
            "تأكد من عدم انقطاع الكهرباء عن غرفة الشبكة أو لوحة الشاشات.",
        ],
    },
    CAUSE_DEVICE_OFF: {
        "icon": "🔌",
        "title": "جهاز العرض مطفأ أو أُغلقت صفحة الشاشة",
        "school_note": "أُغلق الاتصال بشكل سليم، وهذا ما يحدث عادةً عند إطفاء التلفزيون أو إغلاق المتصفح.",
        "hints": [
            "شغّل التلفزيون وتأكد من فتح صفحة الشاشة.",
            "إن كان الإطفاء مقصودًا بعد الدوام فلا حاجة لأي إجراء.",
        ],
    },
    CAUSE_NETWORK_DROP: {
        "icon": "📶",
        "title": "انقطاع مفاجئ في الشبكة عن جهاز العرض",
        "school_note": "انقطع الاتصال دون إغلاق سليم، وهذا يحدث عند فصل الكهرباء عن الجهاز أو ضعف شبكة الواي فاي.",
        "hints": [
            "تحقق من كابل الشبكة أو قوة الواي فاي عند جهاز العرض.",
            "تأكد من ثبات مصدر الكهرباء عن التلفزيون.",
        ],
    },
    CAUSE_WS_TIMEOUT: {
        "icon": "⏱",
        "title": "توقف قناة التحديث اللحظي",
        "school_note": "بقي الجهاز يعمل لكن قناة التحديث توقفت عن الاستجابة، وغالبًا بسبب بطء الشبكة أو إعدادات جدار الحماية.",
        "hints": [
            "أعد تشغيل صفحة الشاشة من التلفزيون.",
            "إن تكرر الأمر فراجع إعدادات جدار الحماية في المدرسة معنا.",
        ],
    },
    CAUSE_BINDING_LOST: {
        "icon": "🔗",
        "title": "رابط الشاشة مفتوح على جهاز آخر",
        "school_note": "حاول جهاز مختلف فتح رابط هذه الشاشة، فتوقف عرضها على الجهاز المرتبط.",
        "hints": [
            "افتح لوحة التحكم ثم «شاشات العرض» وفك الارتباط لإعادة التفعيل على الجهاز الصحيح.",
            "تجنّب مشاركة رابط الشاشة مع أكثر من جهاز.",
        ],
    },
    CAUSE_NEVER_CONNECTED: {
        "icon": "🆕",
        "title": "الشاشة مضافة ولم تُشغَّل بعد",
        "school_note": "تم إنشاء الشاشة وربطها لكنها لم تتصل ولو مرة واحدة.",
        "hints": [
            "افتح رابط الشاشة على التلفزيون لإكمال التشغيل.",
            "إن لم تعد بحاجة لهذه الشاشة فيمكن حذفها من لوحة التحكم.",
        ],
    },
    CAUSE_UNKNOWN: {
        "icon": "❔",
        "title": "انقطاع اتصال الشاشة",
        "school_note": "لم تصلنا إشارة تحدد السبب بدقة.",
        "hints": [
            "تحقق من الكهرباء والإنترنت وجهاز العرض.",
            "أعد تشغيل صفحة الشاشة على التلفزيون.",
        ],
    },
}

# WebSocket close codes → cause. 1001/1000 are clean shutdowns (device off or
# page closed); 1006 and friends are abrupt drops; 4xxx are ours.
_CLOSE_CODE_CAUSES = {
    1000: CAUSE_DEVICE_OFF,
    1001: CAUSE_DEVICE_OFF,
    1005: CAUSE_NETWORK_DROP,
    1006: CAUSE_NETWORK_DROP,
    1011: CAUSE_WS_TIMEOUT,
    1012: CAUSE_WS_TIMEOUT,
    1013: CAUSE_WS_TIMEOUT,
    4000: CAUSE_WS_TIMEOUT,
    4403: CAUSE_BINDING_LOST,
    4408: CAUSE_BINDING_LOST,
}

_BEACON_CAUSES = {
    "pagehide": CAUSE_DEVICE_OFF,
    "unload": CAUSE_DEVICE_OFF,
    "beforeunload": CAUSE_DEVICE_OFF,
    "offline": CAUSE_NETWORK_DROP,
    "binding_lost": CAUSE_BINDING_LOST,
    "heartbeat_timeout": CAUSE_WS_TIMEOUT,
}


def cause_label(cause: str) -> str:
    return dict(CAUSE_CHOICES).get(cause or CAUSE_UNKNOWN, dict(CAUSE_CHOICES)[CAUSE_UNKNOWN])


def cause_presentation(cause: str) -> dict:
    return CAUSE_PRESENTATION.get(cause or CAUSE_UNKNOWN, CAUSE_PRESENTATION[CAUSE_UNKNOWN])


def _signal_key(screen_id: int) -> str:
    return f"display:disconnect_signal:{int(screen_id)}"


def record_disconnect_signal(
    screen_id: int,
    *,
    source: str,
    code: int | None = None,
    reason: str = "",
    online: bool | None = None,
    at: datetime | None = None,
) -> None:
    """Remember how a screen's last connection ended.

    Best-effort by design: this runs on the WebSocket disconnect path and on a
    farewell beacon, neither of which may fail because telemetry is unavailable.
    """
    try:
        screen_id = int(screen_id or 0)
    except (TypeError, ValueError):
        return
    if screen_id <= 0:
        return

    at = at or timezone.now()
    payload = {
        "source": str(source or "")[:32],
        "code": int(code) if code is not None else None,
        "reason": str(reason or "")[:64],
        "online": bool(online) if online is not None else None,
        "ts": at.timestamp(),
    }
    try:
        cache.set(_signal_key(screen_id), payload, timeout=SIGNAL_TTL_SECONDS)
    except Exception:
        pass


def read_disconnect_signals(screen_ids) -> dict[int, dict]:
    keys: dict[str, int] = {}
    for raw in screen_ids:
        try:
            screen_id = int(raw)
        except (TypeError, ValueError):
            continue
        if screen_id > 0:
            keys[_signal_key(screen_id)] = screen_id
    if not keys:
        return {}
    try:
        found = cache.get_many(list(keys.keys())) or {}
    except Exception:
        return {}
    out: dict[int, dict] = {}
    for key, payload in found.items():
        screen_id = keys.get(key)
        if screen_id is not None and isinstance(payload, dict):
            out[screen_id] = payload
    return out


def _signal_datetime(signal: dict | None) -> datetime | None:
    if not signal:
        return None
    try:
        return datetime.fromtimestamp(float(signal.get("ts") or 0), tz=timezone.get_current_timezone())
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _signal_is_relevant(signal: dict | None, last_seen: datetime | None) -> bool:
    """A signal only explains this outage if it arrived around the last heartbeat."""
    moment = _signal_datetime(signal)
    if moment is None:
        return False
    if last_seen is None:
        return True
    return moment >= last_seen - timedelta(minutes=5)


def classify_outage(
    *,
    last_seen: datetime | None,
    ever_connected: bool,
    signal: dict | None,
    school_offline_count: int,
    school_monitored_count: int,
    school_simultaneous: bool,
    platform_outage: bool = False,
    school_closed: bool = False,
) -> dict:
    """Name the cause of one screen's outage.

    Ordered from the widest blast radius inward: a platform fault explains every
    screen, a closed school explains every screen in it, a whole-school blackout
    explains every screen in that school, and only what is left is genuinely
    about this one television.

    ``school_closed`` comes before the blackout test on purpose. Two screens
    switched off for a holiday go quiet together exactly as they would in a
    power cut, so the blackout heuristic would name a cause it cannot tell apart
    from a closure — and an alert that asserts "check your router" on the first
    day of Eid teaches its reader to stop believing alerts.
    """
    if platform_outage:
        return {
            "cause": CAUSE_PLATFORM,
            "confidence": CONFIDENCE_CONFIRMED,
            "scope": SCOPE_PLATFORM,
            "detail": "انقطاع واسع يشمل شاشات عدة مدارس في الوقت نفسه.",
        }

    if school_closed:
        return {
            "cause": CAUSE_SCHOOL_CLOSED,
            "confidence": CONFIDENCE_CONFIRMED,
            "scope": SCOPE_SCHOOL,
            "detail": "اليوم مسجَّل إجازة في تقويم المدرسة، فالإطفاء متوقّع.",
        }

    if not ever_connected:
        return {
            "cause": CAUSE_NEVER_CONNECTED,
            "confidence": CONFIDENCE_CONFIRMED,
            "scope": SCOPE_SCREEN,
            "detail": "لم يُسجَّل أي اتصال لهذه الشاشة منذ إنشائها.",
        }

    school_wide = (
        school_monitored_count >= 2
        and school_offline_count >= 2
        and school_simultaneous
        and (school_offline_count == school_monitored_count or school_offline_count >= 3)
    )
    if school_wide:
        return {
            "cause": CAUSE_SCHOOL_NETWORK,
            "confidence": CONFIDENCE_LIKELY,
            "scope": SCOPE_SCHOOL,
            "detail": (
                f"توقفت {school_offline_count} من {school_monitored_count} شاشات في المدرسة "
                "خلال دقيقتين، ما يرجّح مصدرًا مشتركًا."
            ),
        }

    if _signal_is_relevant(signal, last_seen):
        source = str((signal or {}).get("source") or "")
        reason = str((signal or {}).get("reason") or "").strip().lower()
        code = (signal or {}).get("code")

        cause = _BEACON_CAUSES.get(reason)
        if cause is None and source in {"pagehide", "beacon"}:
            cause = CAUSE_DEVICE_OFF
        if cause is None and code is not None:
            try:
                cause = _CLOSE_CODE_CAUSES.get(int(code))
            except (TypeError, ValueError):
                cause = None

        if cause is not None:
            bits = []
            if code is not None:
                bits.append(f"رمز الإغلاق {code}")
            if reason:
                bits.append(reason)
            if (signal or {}).get("online") is False:
                bits.append("الجهاز أبلغ عن فقدان الشبكة قبل الانقطاع")
            detail = "إشارة الوداع من الجهاز: " + "، ".join(bits) if bits else "إشارة وداع مباشرة من الجهاز."
            confirmed = source in {"pagehide", "beacon"} or cause == CAUSE_BINDING_LOST
            return {
                "cause": cause,
                "confidence": CONFIDENCE_CONFIRMED if confirmed else CONFIDENCE_LIKELY,
                "scope": SCOPE_SCREEN,
                "detail": detail,
            }

    return {
        "cause": CAUSE_NETWORK_DROP if school_offline_count <= 1 else CAUSE_UNKNOWN,
        "confidence": CONFIDENCE_LIKELY,
        "scope": SCOPE_SCREEN,
        "detail": "لم تصل إشارة إغلاق من الجهاز قبل انقطاع اتصاله.",
    }
