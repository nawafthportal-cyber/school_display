# schedule/models.py
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from core.models import School

# ============================================================
# Helpers
# ============================================================

WEEKDAYS = (
    (1, "الاثنين"),
    (2, "الثلاثاء"),
    (3, "الأربعاء"),
    (4, "الخميس"),
    (5, "الجمعة"),
    (6, "السبت"),
    (7, "الأحد"),
)


def _fmt(t: time | None) -> str:
    return t.strftime("%H:%M:%S") if isinstance(t, time) else "—"


def _overlap(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


def _to_dt(t: time) -> datetime:
    return datetime.combine(date(2000, 1, 1), t)


def _text_or_default(value: str | None, default: str) -> str:
    value = (value or "").strip()
    return value or default


DEFAULT_DISPLAY_BEFORE_BADGE = "أهلا بكم"
DEFAULT_DISPLAY_BEFORE_TITLE = "استعدوا لبداية يوم دراسي جميل"
DEFAULT_DISPLAY_AFTER_BADGE = "أحسنتم"
DEFAULT_DISPLAY_AFTER_TITLE = "أحسنتم اليوم، ونلقاكم غدا بإذن الله"
DEFAULT_DISPLAY_AFTER_HOLIDAY_BADGE = "إجازة"
DEFAULT_DISPLAY_AFTER_HOLIDAY_TITLE = "أحسنتم اليوم، نتمنى لكم إجازة سعيدة"
DEFAULT_DISPLAY_HOLIDAY_BADGE = "إجازة"
DEFAULT_DISPLAY_HOLIDAY_TITLE = "اليوم إجازة، نتمنى لكم وقتًا سعيدًا"

BELL_SOUND_DEFAULT = "bell"
BELL_SOUND_ELEVATOR_ANNOUNCEMENT = "elevator_announcement_bells"
BELL_SOUND_CHOICES = [
    (BELL_SOUND_DEFAULT, "جرس المدرسة الافتراضي"),
    (BELL_SOUND_ELEVATOR_ANNOUNCEMENT, "أجراس إعلان المصعد"),
]
BELL_SOUND_STATIC_PATHS = {
    BELL_SOUND_DEFAULT: "sounds/bell.mp3",
    BELL_SOUND_ELEVATOR_ANNOUNCEMENT: "sounds/mixkit-elevator-announcement-bells-112.wav",
}


# ============================================================
# School Settings
# ============================================================

class DisplayTheme(models.Model):
    from core.display_themes import BACKGROUND_CHOICES, BASE_THEMES, FONT_CHOICES

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="display_themes")
    name = models.CharField("اسم الثيم", max_length=70)
    base_theme = models.CharField("نقطة البداية", max_length=20, choices=[(key, key) for key in BASE_THEMES], default="indigo")
    background_color = models.CharField("لون الخلفية", max_length=7, default="#07111F", validators=[RegexValidator(r"^#[0-9A-Fa-f]{6}$", "اختر لونًا صالحًا.")])
    card_color = models.CharField("لون البطاقات", max_length=7, default="#142033", validators=[RegexValidator(r"^#[0-9A-Fa-f]{6}$", "اختر لونًا صالحًا.")])
    text_color = models.CharField("لون النصوص", max_length=7, default="#E2E8F0", validators=[RegexValidator(r"^#[0-9A-Fa-f]{6}$", "اختر لونًا صالحًا.")])
    heading_color = models.CharField("لون العناوين", max_length=7, default="#FFFFFF", validators=[RegexValidator(r"^#[0-9A-Fa-f]{6}$", "اختر لونًا صالحًا.")])
    accent_color = models.CharField("اللون الرئيسي", max_length=7, default="#38BDF8", validators=[RegexValidator(r"^#[0-9A-Fa-f]{6}$", "اختر لونًا صالحًا.")])
    font = models.CharField("الخط", max_length=10, choices=FONT_CHOICES, default="ibm")
    font_scale = models.PositiveSmallIntegerField("حجم النصوص (%)", default=100, validators=[MinValueValidator(90), MaxValueValidator(115)])
    card_radius = models.PositiveSmallIntegerField("استدارة البطاقات", default=24, validators=[MinValueValidator(0), MaxValueValidator(32)])
    background_style = models.CharField("نمط الخلفية", max_length=12, choices=BACKGROUND_CHOICES, default="gradient")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "pk")
        constraints = [models.UniqueConstraint(fields=("school", "name"), name="display_theme_school_name_unique")]
        verbose_name = "ثيم خاص"
        verbose_name_plural = "الثيمات الخاصة"

    def __str__(self):
        return self.name

    def clean(self):
        from core.display_themes import HEX_COLOR, contrast_ratio
        super().clean()
        errors = {}
        for foreground, minimum in (("text_color", 4.5), ("heading_color", 3), ("accent_color", 3)):
            for background in ("background_color", "card_color"):
                front, back = getattr(self, foreground), getattr(self, background)
                if HEX_COLOR.fullmatch(front or "") and HEX_COLOR.fullmatch(back or "") and contrast_ratio(front, back) < minimum:
                    errors[foreground] = "اختر لونًا أوضح على الخلفية والبطاقات لضمان سهولة القراءة."
        if errors:
            raise ValidationError(errors)


class SchoolSettings(models.Model):
    # Legacy stored values (kept for backward compatibility)
    THEME_DEFAULT = "default"
    THEME_BOYS = "boys"
    THEME_GIRLS = "girls"

    # New display theme keys (match CSS: body.display-board[data-theme="..."])
    THEME_INDIGO = "indigo"
    THEME_EMERALD = "emerald"
    THEME_ROSE = "rose"
    THEME_CYAN = "cyan"
    THEME_AMBER = "amber"
    THEME_ORANGE = "orange"
    THEME_VIOLET = "violet"

    THEME_CHOICES = [
        # Recommended themes
        (THEME_INDIGO, "أزرق/نيلي"),
        (THEME_EMERALD, "أخضر"),
        (THEME_ROSE, "وردي"),
        (THEME_CYAN, "سماوي"),
        (THEME_AMBER, "أصفر"),
        (THEME_ORANGE, "برتقالي"),
        (THEME_VIOLET, "بنفسجي"),

        # Legacy values (older dashboards stored these)
        (THEME_DEFAULT, "افتراضي (قديم)"),
        (THEME_BOYS, "مدارس البنين (قديم)"),
        (THEME_GIRLS, "مدارس البنات (قديم)"),
    ]

    show_home = models.BooleanField("إظهار الصفحة الرئيسية", default=True)
    show_settings = models.BooleanField("إظهار إعدادات المدرسة", default=True)
    show_change_password = models.BooleanField("إظهار تغيير كلمة المرور", default=True)
    show_screens = models.BooleanField("إظهار شاشات العرض", default=True)
    show_days = models.BooleanField("إظهار جداول الأيام", default=True)
    show_announcements = models.BooleanField("إظهار التنبيهات", default=True)
    show_excellence = models.BooleanField("إظهار قسم التميز", default=True)
    show_standby = models.BooleanField("إظهار حصص الانتظار", default=True)
    show_lessons = models.BooleanField("إظهار الحصص المجدولة", default=True)
    show_school_data = models.BooleanField("إظهار الفصول والمواد والمعلم/ـةون", default=True)

    FEATURE_PANEL_EXCELLENCE = "excellence"
    FEATURE_PANEL_DUTY = "duty"
    FEATURE_PANEL_CHOICES = [
        (FEATURE_PANEL_EXCELLENCE, "لوحة الشرف"),
        (FEATURE_PANEL_DUTY, "لوحة الإشراف والمناوبة"),
    ]

    featured_panel = models.CharField(
        "الكرت المميز في شاشة العرض",
        max_length=20,
        choices=FEATURE_PANEL_CHOICES,
        default=FEATURE_PANEL_EXCELLENCE,
        help_text="يحدد محتوى الكرت المميز في شاشة العرض لجميع شاشات المدرسة.",
    )

    school = models.OneToOneField(
        School,
        on_delete=models.CASCADE,
        related_name="schedule_settings",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )

    name = models.CharField("اسم المدرسة", max_length=150)
    logo_url = models.URLField("رابط الشعار", blank=True, null=True)

    theme = models.CharField(
        "الثيم/اللون",
        max_length=20,
        choices=THEME_CHOICES,
        default=THEME_DEFAULT,
    )
    custom_theme = models.ForeignKey(DisplayTheme, on_delete=models.SET_NULL, null=True, blank=True, related_name="school_defaults", verbose_name="الثيم الخاص")

    def clean(self):
        super().clean()
        if self.custom_theme_id and self.custom_theme.school_id != self.school_id:
            raise ValidationError({"custom_theme": "اختر ثيمًا تابعًا لهذه المدرسة."})

    timezone_name = models.CharField("المنطقة الزمنية", max_length=64, default="Asia/Riyadh")

    refresh_interval_sec = models.PositiveIntegerField(
        "فاصل تحديث الشاشة (ثوانٍ)",
        default=60,
        validators=[MinValueValidator(5)],
        help_text="عدد الثواني بين كل تحديث تلقائي للشاشة.",
    )
    screen_offline_alerts_enabled = models.BooleanField(
        "تنبيه عند تعطل شاشة",
        default=True,
    )
    screen_offline_threshold_minutes = models.PositiveSmallIntegerField(
        "مدة الانتظار قبل التنبيه (دقائق)",
        default=10,
        validators=[MinValueValidator(5), MaxValueValidator(60)],
        help_text="يرسل النظام تنبيهًا إذا لم تتصل الشاشة خلال هذه المدة.",
    )
    screen_offline_school_hours_only = models.BooleanField(
        "التنبيه أثناء الدوام فقط",
        default=True,
        help_text="لا يُرسل تنبيه بعد انتهاء الدوام أو في الإجازات، ويبقى الانقطاع مسجّلًا في التقرير الأسبوعي.",
    )
    screen_offline_grace_minutes = models.PositiveSmallIntegerField(
        "مهلة بداية الدوام (دقائق)",
        default=15,
        validators=[MaxValueValidator(120)],
        help_text="فترة سماح بعد بداية اليوم الدراسي قبل إرسال أول تنبيه.",
    )
    screen_offline_cooldown_minutes = models.PositiveSmallIntegerField(
        "أقل فاصل بين تنبيهين (دقائق)",
        default=120,
        validators=[MinValueValidator(10), MaxValueValidator(720)],
        help_text="يمنع تكرار التنبيه عن الشاشة نفسها خلال هذه المدة.",
    )
    screen_offline_max_alerts_per_day = models.PositiveSmallIntegerField(
        "أقصى عدد تنبيهات لكل شاشة يوميًا",
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(20)],
    )
    screen_recovery_notice_enabled = models.BooleanField(
        "إشعار عند عودة الاتصال",
        default=True,
        help_text="يرسل رسالة قصيرة عند عودة الشاشة مع مدة الانقطاع.",
    )
    weekly_uptime_report_enabled = models.BooleanField(
        "إرسال تقرير التشغيل الأسبوعي",
        default=True,
    )
    standby_scroll_speed = models.FloatField(
        "سرعة تمرير الانتظار",
        default=0.8,
        validators=[MinValueValidator(0.05), MaxValueValidator(5.0)],
    )

    periods_scroll_speed = models.FloatField(
        "سرعة تمرير جدول الحصص",
        default=0.5,
        validators=[MinValueValidator(0.05), MaxValueValidator(5.0)],
    )

    bell_sound = models.CharField(
        "صوت الجرس",
        max_length=40,
        choices=BELL_SOUND_CHOICES,
        default=BELL_SOUND_DEFAULT,
        help_text="الصوت الذي يعمل عند بداية الحصة ونهاية الحصة السابقة.",
    )

    display_before_title = models.CharField(
        "عنوان الشاشة قبل بداية الدوام",
        max_length=150,
        blank=True,
        default=DEFAULT_DISPLAY_BEFORE_TITLE,
        help_text="يظهر كنص رئيسي في شاشة العرض قبل بداية أول حصة.",
    )

    display_before_badge = models.CharField(
        "شارة الحالة قبل بداية الدوام",
        max_length=40,
        blank=True,
        default=DEFAULT_DISPLAY_BEFORE_BADGE,
        help_text="تظهر كشارة صغيرة أعلى بطاقة الحالة قبل بداية اليوم الدراسي.",
    )

    display_after_title = models.CharField(
        "عنوان الشاشة بعد انتهاء الدوام",
        max_length=150,
        blank=True,
        default=DEFAULT_DISPLAY_AFTER_TITLE,
        help_text="يظهر كنص رئيسي في شاشة العرض بعد انتهاء آخر حصة.",
    )

    display_after_badge = models.CharField(
        "شارة الحالة بعد انتهاء الدوام",
        max_length=40,
        blank=True,
        default=DEFAULT_DISPLAY_AFTER_BADGE,
        help_text="تظهر كشارة صغيرة أعلى بطاقة الحالة بعد انتهاء اليوم الدراسي.",
    )

    display_after_holiday_title = models.CharField(
        "عنوان الشاشة بعد الدوام إذا كان الغد إجازة",
        max_length=150,
        blank=True,
        default=DEFAULT_DISPLAY_AFTER_HOLIDAY_TITLE,
        help_text="يظهر بعد انتهاء الدوام عندما لا يكون اليوم التالي يومًا دراسيًا.",
    )

    display_after_holiday_badge = models.CharField(
        "شارة الحالة بعد الدوام إذا كان الغد إجازة",
        max_length=40,
        blank=True,
        default=DEFAULT_DISPLAY_AFTER_HOLIDAY_BADGE,
        help_text="تظهر كشارة صغيرة بعد انتهاء الدوام عندما لا يكون اليوم التالي يومًا دراسيًا.",
    )

    display_holiday_title = models.CharField(
        "عنوان الشاشة في يوم الإجازة",
        max_length=150,
        blank=True,
        default=DEFAULT_DISPLAY_HOLIDAY_TITLE,
        help_text="يظهر كنص رئيسي على الشاشة عندما يكون اليوم إجازة.",
    )

    display_holiday_badge = models.CharField(
        "شارة الحالة في يوم الإجازة",
        max_length=40,
        blank=True,
        default=DEFAULT_DISPLAY_HOLIDAY_BADGE,
        help_text="تظهر كشارة صغيرة أعلى بطاقة الحالة عندما يكون اليوم إجازة.",
    )

    display_accent_color = models.CharField(
        "لون شاشة العرض (اختياري)",
        max_length=7,
        blank=True,
        null=True,
        validators=[
            RegexValidator(
                regex=r"^#[0-9A-Fa-f]{6}$",
                message="أدخل لونًا بصيغة HEX مثل: #22C55E",
            )
        ],
        help_text="اختياري: اختر لوناً رئيسياً لشاشة العرض. اتركه فارغاً لاستخدام ألوان الثيم.",
    )

    schedule_revision = models.PositiveIntegerField(
        "إصدار الجدول",
        default=1,
        help_text="يزداد تلقائياً عند أي تعديل على الجدول لإجبار شاشات العرض على جلب بيانات جديدة فوراً.",
    )

    test_mode_weekday_override = models.PositiveSmallIntegerField(
        "وضع الاختبار: محاكاة يوم",
        choices=WEEKDAYS,
        blank=True,
        null=True,
        help_text=(
            "للسوبر أدمن فقط: لتشغيل الشاشة في يوم إجازة للاختبار، "
            "حدد رقم اليوم المراد محاكاته (مثلاً: الأحد). "
            "سيعرض النظام جدول ذلك اليوم حتى في أيام الإجازة. "
            "اتركه فارغاً لإلغاء المحاكاة."
        ),
    )

    class Meta:
        verbose_name = "إعداد المدرسة"
        verbose_name_plural = "إعدادات المدرسة"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    def get_display_before_title(self) -> str:
        return _text_or_default(getattr(self, "display_before_title", ""), DEFAULT_DISPLAY_BEFORE_TITLE)

    def get_display_before_badge(self) -> str:
        return _text_or_default(getattr(self, "display_before_badge", ""), DEFAULT_DISPLAY_BEFORE_BADGE)

    def get_display_after_title(self) -> str:
        return _text_or_default(getattr(self, "display_after_title", ""), DEFAULT_DISPLAY_AFTER_TITLE)

    def get_display_after_badge(self) -> str:
        return _text_or_default(getattr(self, "display_after_badge", ""), DEFAULT_DISPLAY_AFTER_BADGE)

    def get_display_after_holiday_title(self) -> str:
        return _text_or_default(getattr(self, "display_after_holiday_title", ""), DEFAULT_DISPLAY_AFTER_HOLIDAY_TITLE)

    def get_display_after_holiday_badge(self) -> str:
        return _text_or_default(getattr(self, "display_after_holiday_badge", ""), DEFAULT_DISPLAY_AFTER_HOLIDAY_BADGE)

    def get_display_holiday_title(self) -> str:
        return _text_or_default(getattr(self, "display_holiday_title", ""), DEFAULT_DISPLAY_HOLIDAY_TITLE)

    def get_display_holiday_badge(self) -> str:
        return _text_or_default(getattr(self, "display_holiday_badge", ""), DEFAULT_DISPLAY_HOLIDAY_BADGE)

    def get_display_messages(self) -> dict[str, str]:
        return {
            "before_title": self.get_display_before_title(),
            "before_badge": self.get_display_before_badge(),
            "after_title": self.get_display_after_title(),
            "after_badge": self.get_display_after_badge(),
            "after_holiday_title": self.get_display_after_holiday_title(),
            "after_holiday_badge": self.get_display_after_holiday_badge(),
            "holiday_title": self.get_display_holiday_title(),
            "holiday_badge": self.get_display_holiday_badge(),
        }

    def get_bell_sound_path(self) -> str:
        return BELL_SOUND_STATIC_PATHS.get(
            getattr(self, "bell_sound", "") or BELL_SOUND_DEFAULT,
            BELL_SOUND_STATIC_PATHS[BELL_SOUND_DEFAULT],
        )


class DutyAssignment(models.Model):
    DUTY_SUPERVISION = "supervision"
    DUTY_DUTY = "duty"
    DUTY_CHOICES = [
        (DUTY_SUPERVISION, "إشراف"),
        (DUTY_DUTY, "مناوبة"),
    ]

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="duty_assignments",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )
    date = models.DateField("التاريخ", default=timezone.localdate)
    teacher_name = models.CharField("اسم المعلم/ـة", max_length=100)
    duty_type = models.CharField("النوع", max_length=20, choices=DUTY_CHOICES)
    location = models.CharField("المكان (اختياري)", max_length=120, blank=True)
    priority = models.PositiveSmallIntegerField("ترتيب العرض", default=10)
    is_active = models.BooleanField("نشط", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "إشراف/مناوبة"
        verbose_name_plural = "الإشراف والمناوبة"
        ordering = ("date", "priority", "-id")
        indexes = [
            models.Index(fields=["school", "date"], name="duty_school_date_idx"),
            models.Index(fields=["school", "date", "duty_type"], name="duty_school_date_type"),
        ]

    def __str__(self) -> str:
        loc = f" — {self.location}" if self.location else ""
        return f"{self.date} — {self.teacher_name} — {self.get_duty_type_display()}{loc}"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "date": self.date.isoformat() if self.date else None,
            "teacher_name": self.teacher_name,
            "duty_type": self.duty_type,
            "duty_label": self.get_duty_type_display(),
            "location": self.location or "",
        }


class SchoolClosure(models.Model):
    """يومٌ لا دوام فيه رغم أن جدوله الأسبوعي قائم.

    الجداول كلها مبنية على **أيام الأسبوع**: ``DaySchedule`` يعرف «الأحد» ولا
    يعرف «الأحد الواقع في العاشر من رمضان». وإجازة العيد أو منتصف الفصل تقع على
    أيام لها جداول كاملة، فكان النظام يقرؤها أيام دوام:

    * الشاشة تعرض جدول يومٍ لا أحد فيه بدل رسالة الإجازة،
    * ومراقب الانقطاع يرسل تنبيهاً عن شاشاتٍ أُطفئت عمداً — وهو تنبيهٌ صحيح
      تقنياً وخاطئ عملياً، وتكراره يعلّم قارئه تجاهل التنبيهات كلها.

    هذا النموذج يضيف البُعد الغائب: **التاريخ**. مدىً واحد يغطي يوماً أو
    أسبوعين، وما دام يغطي اليوم فلا نافذة دوام له.
    """

    settings = models.ForeignKey(
        SchoolSettings,
        on_delete=models.CASCADE,
        related_name="closures",
        verbose_name="إعدادات المدرسة",
    )
    title = models.CharField(
        "المناسبة",
        max_length=120,
        help_text="مثال: إجازة عيد الفطر، إجازة منتصف الفصل، يوم التأسيس.",
    )
    start_date = models.DateField("من تاريخ")
    end_date = models.DateField(
        "إلى تاريخ",
        help_text="يوم واحد؟ اجعل التاريخين متطابقين.",
    )
    is_active = models.BooleanField("مفعّلة", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "إجازة مدرسية"
        verbose_name_plural = "الإجازات المدرسية"
        ordering = ("-start_date", "-id")
        indexes = [
            models.Index(fields=("settings", "start_date", "end_date"), name="ix_closure_range"),
        ]

    def __str__(self) -> str:
        if self.start_date == self.end_date:
            return f"{self.title} — {self.start_date}"
        return f"{self.title} — {self.start_date} إلى {self.end_date}"

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "تاريخ النهاية قبل تاريخ البداية."})

    @property
    def days_count(self) -> int:
        if not self.start_date or not self.end_date:
            return 0
        return (self.end_date - self.start_date).days + 1

    def covers(self, day: date) -> bool:
        return bool(
            self.is_active
            and self.start_date
            and self.end_date
            and self.start_date <= day <= self.end_date
        )


# ============================================================
# Core Entities
# ============================================================

class SchoolClass(models.Model):
    settings = models.ForeignKey(
        SchoolSettings,
        on_delete=models.CASCADE,
        related_name="school_classes",
        verbose_name="إعدادات المدرسة",
    )
    name = models.CharField("اسم الصف", max_length=50)

    class Meta:
        verbose_name = "صف"
        verbose_name_plural = "الصفوف"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=("settings", "name"), name="uq_sc_set_nm"),
        ]
        indexes = [
            models.Index(fields=("settings", "name"), name="ix_sc_set_nm"),
        ]

    def __str__(self) -> str:
        return self.name


class Subject(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="subjects",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )
    name = models.CharField("اسم المادة", max_length=100)

    class Meta:
        verbose_name = "مادة"
        verbose_name_plural = "المواد"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=("school", "name"), name="uq_sub_sch_nm"),
        ]
        indexes = [
            models.Index(fields=("school", "name"), name="ix_sub_sch_nm"),
        ]

    def __str__(self) -> str:
        return self.name


class Teacher(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="teachers",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )
    name = models.CharField("اسم المعلم/ـة", max_length=100)

    class Meta:
        verbose_name = "معلم"
        verbose_name_plural = "المعلم/ـةون"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=("school", "name"), name="uq_tch_sch_nm"),
        ]
        indexes = [
            models.Index(fields=("school", "name"), name="ix_tch_sch_nm"),
        ]

    def __str__(self) -> str:
        return self.name


# ============================================================
# Schedule
# ============================================================

class DaySchedule(models.Model):
    settings = models.ForeignKey(
        SchoolSettings,
        on_delete=models.CASCADE,
        related_name="day_schedules",
        verbose_name="إعدادات المدرسة",
    )
    weekday = models.PositiveSmallIntegerField("اليوم", choices=WEEKDAYS)
    is_active = models.BooleanField("يوم دراسي", default=True)
    periods_count = models.PositiveSmallIntegerField(
        "عدد الحصص",
        default=6,
        validators=[MinValueValidator(1)],
    )

    class Meta:
        verbose_name = "جدول يوم"
        verbose_name_plural = "جداول الأيام"
        ordering = ("weekday",)
        constraints = [
            models.UniqueConstraint(fields=("settings", "weekday"), name="uq_ds_set_wd"),
        ]
        indexes = [
            models.Index(fields=("settings", "weekday"), name="ix_ds_set_wd"),
        ]

    def __str__(self) -> str:
        return f"{self.settings.name} — {self.get_weekday_display()}"


class Period(models.Model):
    day = models.ForeignKey(
        DaySchedule,
        on_delete=models.CASCADE,
        related_name="periods",
        verbose_name="جدول اليوم",
    )
    school_class = models.ForeignKey(
        SchoolClass,
        on_delete=models.CASCADE,
        related_name="periods",
        verbose_name="الصف",
        null=True,
        blank=True,
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="periods",
        verbose_name="المادة",
        null=True,
        blank=True,
    )
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name="periods",
        verbose_name="المعلم/ـة",
        null=True,
        blank=True,
    )

    index = models.PositiveSmallIntegerField("ترتيب الحصة (1..ن)", validators=[MinValueValidator(1)])
    starts_at = models.TimeField("يبدأ")
    ends_at = models.TimeField("ينتهي")

    class Meta:
        verbose_name = "حصة"
        verbose_name_plural = "الحصص"
        ordering = ("day__weekday", "index", "starts_at")
        constraints = [
            models.UniqueConstraint(fields=("day", "index"), name="uq_p_day_idx"),
        ]
        indexes = [
            models.Index(fields=("day", "starts_at"), name="ix_p_day_st"),
            models.Index(fields=("day", "ends_at"), name="ix_p_day_en"),
        ]

    def __str__(self) -> str:
        return f"حصة #{self.index} — {_fmt(self.starts_at)}→{_fmt(self.ends_at)}"

    def clean(self) -> None:
        if self.starts_at is None or self.ends_at is None:
            raise ValidationError("وقت البداية والنهاية مطلوبان.")
        if not (self.starts_at < self.ends_at):
            raise ValidationError({"ends_at": [f"وقت البداية يجب أن يكون قبل النهاية ({_fmt(self.starts_at)} < {_fmt(self.ends_at)})"]})

        if not self.day_id:
            return

        # When saving multiple periods/breaks in one request (inline formsets),
        # model-level DB overlap checks can falsely fail because other rows are
        # still persisted with their old times until the formset finishes saving.
        # The dashboard formset already performs full cross-validation using the
        # submitted values, so allow opting out of DB overlap checks.
        if getattr(self, "_skip_cross_validation", False):
            return

        # تداخل مع حصص أخرى
        qs = Period.objects.filter(day_id=self.day_id).exclude(pk=self.pk)
        for p in qs:
            if _overlap(self.starts_at, self.ends_at, p.starts_at, p.ends_at):
                raise ValidationError(f"تداخل مع الحصة #{p.index} ({_fmt(p.starts_at)}-{_fmt(p.ends_at)})")

        # تداخل مع الفسح (بدون import دائري)
        BreakModel = apps.get_model("schedule", "Break")
        for b in BreakModel.objects.filter(day_id=self.day_id):
            if _overlap(self.starts_at, self.ends_at, b.starts_at, b.ends_at):
                raise ValidationError(f"تداخل مع الفسحة '{b.label}' ({_fmt(b.starts_at)}-{_fmt(b.ends_at)})")


class Break(models.Model):
    day = models.ForeignKey(
        DaySchedule,
        on_delete=models.CASCADE,
        related_name="breaks",
        verbose_name="جدول اليوم",
    )
    label = models.CharField("تسمية الفسحة", max_length=50, default="فسحة")
    starts_at = models.TimeField("يبدأ")
    duration_min = models.PositiveSmallIntegerField(
        "المدة (دقائق)",
        default=20,
        validators=[MinValueValidator(1), MaxValueValidator(240)],
    )

    class Meta:
        verbose_name = "فسحة"
        verbose_name_plural = "فسح"
        ordering = ("day__weekday", "starts_at")
        indexes = [
            models.Index(fields=("day", "starts_at"), name="ix_b_day_st"),
        ]

    def __str__(self) -> str:
        return f"{self.label} — {_fmt(self.starts_at)} ({self.duration_min} د)"

    @property
    def ends_at(self) -> time:
        return (_to_dt(self.starts_at) + timedelta(minutes=int(self.duration_min))).time()

    def clean(self) -> None:
        if self.duration_min is None or int(self.duration_min) <= 0:
            raise ValidationError({"duration_min": ["المدة يجب أن تكون أكبر من صفر."]})
        if self.starts_at is None:
            raise ValidationError({"starts_at": ["هذا الحقل مطلوب."]})

        end_t = self.ends_at
        if not (self.starts_at < end_t):
            raise ValidationError({"starts_at": [f"البداية يجب أن تكون قبل النهاية ({_fmt(self.starts_at)} < {_fmt(end_t)})"]})

        if not self.day_id:
            return

        # See Period.clean(): allow the dashboard batch editor to rely on its
        # own POST-based overlap validation without being blocked by stale DB.
        if getattr(self, "_skip_cross_validation", False):
            return

        # تداخل مع فسح أخرى
        qs = Break.objects.filter(day_id=self.day_id).exclude(pk=self.pk)
        for b in qs:
            if _overlap(self.starts_at, end_t, b.starts_at, b.ends_at):
                raise ValidationError(f"تداخل مع '{b.label}' ({_fmt(b.starts_at)}-{_fmt(b.ends_at)})")

        # تداخل مع حصص
        PeriodModel = apps.get_model("schedule", "Period")
        for p in PeriodModel.objects.filter(day_id=self.day_id):
            if _overlap(self.starts_at, end_t, p.starts_at, p.ends_at):
                raise ValidationError(f"تداخل مع الحصة #{p.index} ({_fmt(p.starts_at)}-{_fmt(p.ends_at)})")


class ClassLesson(models.Model):
    settings = models.ForeignKey(
        SchoolSettings,
        on_delete=models.CASCADE,
        related_name="class_lessons",
        verbose_name="إعدادات المدرسة",
    )
    school_class = models.ForeignKey(
        SchoolClass,
        on_delete=models.CASCADE,
        related_name="class_lessons",
        verbose_name="الصف",
    )
    weekday = models.PositiveSmallIntegerField("اليوم", choices=WEEKDAYS)
    period_index = models.PositiveSmallIntegerField("رقم الحصة", validators=[MinValueValidator(1)])
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="class_lessons",
        verbose_name="المادة",
    )
    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        related_name="class_lessons",
        verbose_name="المعلم/ـة",
    )
    is_active = models.BooleanField("مفعّلة", default=True)

    class Meta:
        verbose_name = "حصة مجدولة"
        verbose_name_plural = "الحصص المجدولة"
        ordering = ("settings", "weekday", "period_index", "school_class__name")
        constraints = [
            models.UniqueConstraint(fields=("settings", "weekday", "period_index", "school_class"), name="uq_cl_slot"),
        ]
        indexes = [
            models.Index(fields=("settings", "weekday", "period_index"), name="ix_cl_lookup"),
        ]

    def __str__(self) -> str:
        return f"{self.school_class} | {self.subject} | {self.teacher}"

    def clean(self) -> None:
        if not self.settings_id:
            return
        settings_school_id = self.settings.school_id
        if settings_school_id:
            if self.subject_id and self.subject.school_id and self.subject.school_id != settings_school_id:
                raise ValidationError({"subject": ["المادة لا تتبع لنفس المدرسة."]})
            if self.teacher_id and self.teacher.school_id and self.teacher.school_id != settings_school_id:
                raise ValidationError({"teacher": ["المعلم/ـة لا يتبع لنفس المدرسة."]})
