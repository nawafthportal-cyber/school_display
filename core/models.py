from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
import os
import secrets
import uuid

from .occasions import screen_choices as occasion_screen_choices
from .screen_diagnostics import (
    CAUSE_CHOICES as SCREEN_OUTAGE_CAUSE_CHOICES,
    CAUSE_UNKNOWN as SCREEN_OUTAGE_CAUSE_UNKNOWN,
    CONFIDENCE_CHOICES as SCREEN_OUTAGE_CONFIDENCE_CHOICES,
    CONFIDENCE_LIKELY as SCREEN_OUTAGE_CONFIDENCE_LIKELY,
    SCOPE_CHOICES as SCREEN_OUTAGE_SCOPE_CHOICES,
    SCOPE_SCREEN as SCREEN_OUTAGE_SCOPE_SCREEN,
)
from .system_access import ROLE_CHOICES


_IMAGE_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _validate_image_extension(value):
    if not value:
        return
    name = (getattr(value, "name", "") or "").strip()
    _, ext = os.path.splitext(name)
    ext = (ext or "").lower()
    if ext == "":
        return
    if ext not in _IMAGE_ALLOWED_EXTS:
        raise ValidationError(
            f"امتداد الملف \"{ext.lstrip('.')}\" غير مسموح به. "
            f"الامتدادات المسموح بها: {', '.join(sorted(e.lstrip('.') for e in _IMAGE_ALLOWED_EXTS))}.",
            code="invalid_extension",
        )


class SchoolType(models.TextChoices):
    BOYS = "boys", "بنين"
    GIRLS = "girls", "بنات"


class School(models.Model):
    name = models.CharField("اسم المدرسة", max_length=150)
    slug = models.SlugField(
        "رابط المدرسة",
        unique=True,
        help_text="يستخدم في الرابط، مثلا: school-1",
    )
    logo = models.ImageField(
        "الشعار",
        upload_to="schools/logos/",
        blank=True,
        null=True,
        validators=[_validate_image_extension],
    )
    is_active = models.BooleanField(
        "اشتراك نشط",
        default=True,
    )

    school_type = models.CharField(
        "نوع المدرسة",
        max_length=10,
        choices=SchoolType.choices,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "مدرسة"
        verbose_name_plural = "المدارس"

    def __str__(self):
        return self.name


class UserProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile",
        verbose_name="المستخدم",
    )
    schools = models.ManyToManyField(
        School,
        related_name="users",
        verbose_name="المدارس المتاحة",
        blank=True,
    )
    active_school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        related_name="active_users",
        verbose_name="المدرسة النشطة",
        null=True,
        blank=True,
    )
    mobile = models.CharField(
        "رقم الجوال",
        max_length=20,
        blank=True,
        null=True,
    )
    needs_onboarding = models.BooleanField(
        "يحتاج إلى دليل البدء",
        default=False,
        help_text="يوجّه المستخدم الجديد إلى دليل البدء مرة واحدة بعد أول دخول.",
    )
    email_verified_at = models.DateTimeField(
        "تاريخ توثيق البريد",
        null=True,
        blank=True,
        help_text="البريد الموثّق يضمن وصول الفواتير وروابط استعادة كلمة المرور.",
    )

    @property
    def email_is_verified(self) -> bool:
        return self.email_verified_at is not None

    class Meta:
        verbose_name = "ملف المستخدم"
        verbose_name_plural = "ملفات المستخدمين"

    def __str__(self):
        return f"{self.user.username} - {self.active_school.name if self.active_school else 'No Active School'}"


class UserSessionState(models.Model):
    """The sole authenticated browser session currently allowed for a user."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="session_state",
        verbose_name="المستخدم",
    )
    active_session_key = models.CharField(
        "مفتاح الجلسة النشطة",
        max_length=40,
        blank=True,
        default="",
    )
    activated_at = models.DateTimeField("وقت تفعيل الجلسة", auto_now=True)

    class Meta:
        verbose_name = "جلسة مستخدم نشطة"
        verbose_name_plural = "جلسات المستخدمين النشطة"

    def __str__(self) -> str:
        return f"{self.user_id}: {self.active_session_key[:8]}"


class SystemEmployeeProfile(models.Model):
    """Platform-level employee identity, separate from school managers."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="system_employee_profile",
        verbose_name="الموظف",
    )
    role = models.CharField(
        "الدور الوظيفي",
        max_length=32,
        choices=ROLE_CHOICES,
        default="support",
    )
    permission_keys = models.JSONField(
        "صلاحيات المنصة",
        default=list,
        blank=True,
        help_text="قائمة مفاتيح الصلاحيات الممنوحة لهذا الموظف.",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="created_system_employees",
        verbose_name="أنشأه",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "موظف منصة"
        verbose_name_plural = "موظفو المنصة"
        ordering = ("user__first_name", "user__username")

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} - {self.get_role_display()}"


class UserTwoFactorAuth(models.Model):
    """Encrypted TOTP configuration for privileged system accounts."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="two_factor_auth",
        verbose_name="المستخدم",
    )
    encrypted_secret = models.TextField("سر المصادقة المشفر", blank=True)
    is_enabled = models.BooleanField("مفعلة", default=False)
    recovery_code_hashes = models.JSONField("رموز الاسترداد المشفرة", default=list, blank=True)
    last_used_counter = models.BigIntegerField("آخر عداد مستخدم", null=True, blank=True)
    confirmed_at = models.DateTimeField("تاريخ التفعيل", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "مصادقة ثنائية"
        verbose_name_plural = "المصادقة الثنائية"

    def __str__(self) -> str:
        return f"2FA: {self.user} ({'enabled' if self.is_enabled else 'pending'})"


class DisplayScreen(models.Model):
    THEME_CHOICES = [
        ("", "استخدام ثيم المدرسة"),
        ("indigo", "أزرق/نيلي"),
        ("emerald", "أخضر"),
        ("rose", "وردي"),
        ("cyan", "سماوي"),
        ("amber", "أصفر"),
        ("orange", "برتقالي"),
        ("violet", "بنفسجي"),
    ]
    # مشتقة من سجل المناسبات الموحّد، فلا تنحرف عن الخيارات المعروضة في
    # لوحة المدير أو الثيمات المدعومة على الشاشة. انظر :mod:`core.occasions`.
    OCCASION_THEME_CHOICES = occasion_screen_choices()
    FEATURED_PANEL_CHOICES = [
        ("", "استخدام اختيار المدرسة"),
        ("excellence", "لوحة الشرف"),
        ("duty", "الإشراف والمناوبة"),
    ]

    short_code = models.CharField(
        max_length=8,
        unique=True,
        editable=False,
        verbose_name="رابط مختصر",
        null=True,
        blank=True,
    )
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="screens",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )
    name = models.CharField(
        max_length=100,
        verbose_name="اسم الشاشة",
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        verbose_name="رمز الوصول",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="نشط",
    )
    theme_override = models.CharField(
        "الثيم الخاص بالشاشة",
        max_length=20,
        choices=THEME_CHOICES,
        blank=True,
        default="",
        help_text="اتركه على ثيم المدرسة لتتبع الإعداد العام تلقائيًا.",
    )
    custom_theme_override = models.ForeignKey("schedule.DisplayTheme", on_delete=models.SET_NULL, null=True, blank=True, related_name="screens", verbose_name="الثيم الخاص بالشاشة")

    def clean(self):
        super().clean()
        if self.custom_theme_override_id and self.custom_theme_override.school_id != self.school_id:
            raise ValidationError({"custom_theme_override": "اختر ثيمًا تابعًا لهذه المدرسة."})
    occasion_theme = models.CharField(
        "قالب المناسبة",
        max_length=30,
        choices=OCCASION_THEME_CHOICES,
        default="auto",
        help_text="يمكن تفعيله تلقائيًا من التنبيهات أو تثبيت قالب لهذه الشاشة فقط.",
    )
    featured_panel_override = models.CharField(
        "الكرت المميز",
        max_length=20,
        choices=FEATURED_PANEL_CHOICES,
        blank=True,
        default="",
        help_text="يُستخدم عندما تكون لوحة الشرف والإشراف والمناوبة مفعّلتين معًا.",
    )
    logo_override = models.ImageField(
        "شعار هذه الشاشة",
        upload_to="screens/logos/",
        blank=True,
        null=True,
        validators=[_validate_image_extension],
        help_text="اتركه فارغًا لاستخدام شعار المدرسة العام.",
    )
    display_accent_color_override = models.CharField(
        "لون العرض الخاص بالشاشة",
        max_length=7,
        blank=True,
        default="",
        help_text="اتركه فارغًا لاستخدام لون جميع الشاشات.",
    )
    standby_scroll_speed_override = models.FloatField(
        "سرعة تمرير الانتظار لهذه الشاشة",
        null=True,
        blank=True,
    )
    periods_scroll_speed_override = models.FloatField(
        "سرعة تمرير جدول الحصص لهذه الشاشة",
        null=True,
        blank=True,
    )
    display_before_title_override = models.CharField(max_length=150, blank=True, default="")
    display_before_badge_override = models.CharField(max_length=40, blank=True, default="")
    display_after_title_override = models.CharField(max_length=150, blank=True, default="")
    display_after_badge_override = models.CharField(max_length=40, blank=True, default="")
    display_after_holiday_title_override = models.CharField(max_length=150, blank=True, default="")
    display_after_holiday_badge_override = models.CharField(max_length=40, blank=True, default="")
    display_holiday_title_override = models.CharField(max_length=150, blank=True, default="")
    display_holiday_badge_override = models.CharField(max_length=40, blank=True, default="")
    show_announcements = models.BooleanField("إظهار التنبيهات المدرسية", default=True)
    show_period_classes = models.BooleanField("إظهار جدول الحصص الجارية", default=True)
    show_standby = models.BooleanField("إظهار حصص الانتظار", default=True)
    show_duty = models.BooleanField("إظهار الإشراف والمناوبة", default=True)
    show_excellence = models.BooleanField("إظهار لوحة الشرف", default=True)
    auto_disabled_by_limit = models.BooleanField(
        default=False,
        verbose_name="موقوف تلقائياً بسبب الحد",
    )
    monitor_always_on = models.BooleanField(
        "مراقبة على مدار الساعة",
        default=False,
        help_text="فعّله للشاشات التي يُفترض بقاؤها تعمل خارج أوقات الدوام أيضًا.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="آخر ظهور",
    )

    # ربط الشاشة بجهاز واحد (TV) لمنع مشاركة الرابط
    bound_device_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        verbose_name="معرّف الجهاز المرتبط",
    )
    bound_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="تاريخ ربط الجهاز",
    )

    class Meta:
        verbose_name = "شاشة عرض"
        verbose_name_plural = "شاشات العرض"

    @staticmethod
    def _bad_value(v) -> bool:
        v = (v or "").strip()
        return (not v) or (v == "-") or (v.lower() == "none")

    def save(self, *args, **kwargs):
        # ✅ توليد token حتى لو كان None أو "" أو " - "
        if self._bad_value(self.token):
            # 64 hex chars
            for _ in range(30):
                t = secrets.token_hex(32)
                if not DisplayScreen.objects.filter(token=t).exclude(pk=self.pk).exists():
                    self.token = t
                    break

        # ✅ توليد short_code سهل للكتابة على TV (بدون 0/O و 1/I و l)
        if self._bad_value(self.short_code):
            alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
            for _ in range(80):
                code = "".join(secrets.choice(alphabet) for _ in range(6))
                if not DisplayScreen.objects.filter(short_code__iexact=code).exclude(pk=self.pk).exists():
                    self.short_code = code
                    break

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.school.name} - {self.name}" if self.school else self.name


class DisplayPairingSession(models.Model):
    """Short-lived, one-time authorization initiated by a television."""

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_EXPIRED = "expired"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "بانتظار الموافقة"),
        (STATUS_APPROVED, "تم الربط"),
        (STATUS_EXPIRED, "منتهي"),
        (STATUS_CANCELLED, "ملغي"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_code = models.CharField("رمز الربط", max_length=6, db_index=True)
    device_id = models.CharField("معرّف جهاز التلفاز", max_length=64)
    device_secret_hash = models.CharField("بصمة سر الجهاز", max_length=64)
    status = models.CharField(
        "الحالة",
        max_length=12,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    screen = models.ForeignKey(
        DisplayScreen,
        on_delete=models.SET_NULL,
        related_name="pairing_sessions",
        null=True,
        blank=True,
    )
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="approved_display_pairings",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(db_index=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "جلسة ربط تلفاز"
        verbose_name_plural = "جلسات ربط التلفاز"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user_code"],
                condition=models.Q(status="pending"),
                name="unique_pending_display_pairing_code",
            ),
        ]

    @property
    def is_expired(self) -> bool:
        return self.status == self.STATUS_EXPIRED or self.expires_at <= timezone.now()

    def __str__(self):
        return f"{self.user_code} ({self.get_status_display()})"


class ScreenOutage(models.Model):
    screen = models.ForeignKey(
        DisplayScreen,
        on_delete=models.CASCADE,
        related_name="outages",
        verbose_name="الشاشة",
    )
    detected_at = models.DateTimeField("وقت اكتشاف التعطل", default=timezone.now)
    last_seen_at = models.DateTimeField("آخر ظهور قبل التعطل", null=True, blank=True)
    alert_sent_at = models.DateTimeField("وقت إرسال التنبيه", null=True, blank=True)
    resolved_at = models.DateTimeField("وقت عودة الاتصال", null=True, blank=True)
    cause = models.CharField(
        "السبب",
        max_length=32,
        choices=SCREEN_OUTAGE_CAUSE_CHOICES,
        default=SCREEN_OUTAGE_CAUSE_UNKNOWN,
    )
    cause_detail = models.CharField("تفاصيل السبب", max_length=255, blank=True, default="")
    cause_confidence = models.CharField(
        "درجة الثقة",
        max_length=12,
        choices=SCREEN_OUTAGE_CONFIDENCE_CHOICES,
        default=SCREEN_OUTAGE_CONFIDENCE_LIKELY,
    )
    scope = models.CharField(
        "نطاق العطل",
        max_length=16,
        choices=SCREEN_OUTAGE_SCOPE_CHOICES,
        default=SCREEN_OUTAGE_SCOPE_SCREEN,
    )
    close_code = models.IntegerField("رمز إغلاق الاتصال", null=True, blank=True)
    suppressed_reason = models.CharField(
        "سبب عدم الإرسال",
        max_length=32,
        blank=True,
        default="",
        help_text="يُسجَّل العطل دائمًا للتقارير حتى لو لم يُرسل عنه تنبيه.",
    )
    recovery_notified_at = models.DateTimeField("وقت إشعار عودة الاتصال", null=True, blank=True)
    alert_count = models.PositiveSmallIntegerField("عدد التنبيهات المرسلة", default=0)

    class Meta:
        verbose_name = "تعطل شاشة"
        verbose_name_plural = "أعطال الشاشات"
        ordering = ("-detected_at",)
        indexes = [
            models.Index(fields=("screen", "resolved_at"), name="screen_outage_open_idx"),
            models.Index(fields=("detected_at",), name="screen_outage_detect_idx"),
            # Powers the per-screen cooldown and daily-cap lookups on every scan.
            models.Index(fields=("screen", "alert_sent_at"), name="screen_outage_alert_idx"),
        ]

    def __str__(self):
        return f"{self.screen} — {self.detected_at:%Y-%m-%d %H:%M}"


class ScreenWeeklyUptimeReport(models.Model):
    screen = models.ForeignKey(
        DisplayScreen,
        on_delete=models.CASCADE,
        related_name="weekly_uptime_reports",
        verbose_name="الشاشة",
    )
    week_start = models.DateField("بداية الأسبوع")
    week_end = models.DateField("نهاية الأسبوع")
    uptime_percent = models.DecimalField(
        "نسبة التشغيل",
        max_digits=5,
        decimal_places=2,
        default=100,
    )
    offline_seconds = models.PositiveBigIntegerField("ثواني الانقطاع", default=0)
    sent_at = models.DateTimeField("وقت إرسال التقرير", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "تقرير تشغيل أسبوعي"
        verbose_name_plural = "تقارير التشغيل الأسبوعية"
        ordering = ("-week_start", "screen__name")
        constraints = [
            models.UniqueConstraint(
                fields=("screen", "week_start"),
                name="uq_screen_uptime_week",
            )
        ]

    def __str__(self):
        return f"{self.screen} — {self.week_start} — {self.uptime_percent}%"


class SubscriptionPlan(models.Model):
    code = models.SlugField(
        "رمز الخطة",
        unique=True,
        max_length=50,
        help_text="يُستخدم داخليًا، مثل: basic, pro, enterprise",
    )
    name = models.CharField("اسم الخطة", max_length=150)
    description = models.CharField(
        "وصف مختصر",
        max_length=240,
        blank=True,
        help_text="يظهر أسفل اسم الباقة في صفحة الاشتراك وصفحة الهبوط.",
    )
    card_badge_text = models.CharField(
        "نص شارة الباقة",
        max_length=80,
        blank=True,
        help_text="اتركه فارغًا لاستخدام النص التلقائي بحسب نوع الباقة.",
    )
    card_duration_text = models.CharField(
        "نص مدة الباقة",
        max_length=120,
        blank=True,
        help_text="مثال: مدة الباقة: 6 أشهر. اتركه فارغًا ليُنشأ من عدد الأيام.",
    )
    card_price_caption = models.CharField(
        "وصف السعر",
        max_length=120,
        blank=True,
        help_text="مثال: ريال سعودي / نصف سنوي. اتركه فارغًا للنص التلقائي.",
    )
    card_monthly_text = models.CharField(
        "نص المعادل الشهري",
        max_length=160,
        blank=True,
        help_text="اتركه فارغًا ليُحسب المعادل الشهري تلقائيًا من السعر والمدة.",
    )
    card_features = models.TextField(
        "مزايا الباقة",
        default="جميع مزايا النظام كاملة",
        blank=True,
        help_text="اكتب كل ميزة في سطر مستقل. يمكن ترك الحقل فارغًا لإخفاء المزايا.",
    )
    card_screen_text = models.CharField(
        "نص عدد الشاشات",
        max_length=120,
        blank=True,
        help_text="مثال: الشاشات: 3. اتركه فارغًا ليُنشأ من حد الشاشات.",
    )
    card_cta_text = models.CharField(
        "نص زر الطلب",
        max_length=80,
        default="اطلب هذه الباقة",
        blank=True,
    )
    price = models.DecimalField(
        "سعر الباقة (ر.س)",
        max_digits=8,
        decimal_places=2,
        default=0,
    )

    duration_days = models.PositiveIntegerField(
        "مدة الاشتراك (بالأيام)",
        null=True,
        blank=True,
        help_text="عند إنشاء اشتراك وترك تاريخ النهاية فارغًا: النهاية = تاريخ البداية + عدد الأيام.",
    )
    max_users = models.PositiveIntegerField(
        "الحد الأقصى للمستخدمين",
        null=True,
        blank=True,
        help_text="اتركه فارغًا لعدد غير محدود.",
    )
    max_screens = models.PositiveIntegerField(
        "الحد الأقصى لشاشات العرض",
        null=True,
        blank=True,
        help_text="اتركه فارغًا لعدد غير محدود.",
    )
    # ``max_schools`` كان يوحي بحد للمدارس في الخطة الواحدة، لكن كل مدرسة تحمل
    # اشتراكها المستقل، فلم يُقرأ الحقل في أي مسار. أُزيل حتى لا يوهم بحدٍّ
    # مطبَّق. الحد الفعلي الوحيد هو ``max_screens`` لكل اشتراك.
    is_active = models.BooleanField(
        "متاحة للاشتراك",
        default=True,
    )
    is_featured = models.BooleanField(
        "الباقة الموصى بها",
        default=False,
        help_text="تميّز الباقة بصرياً وتعرض عليها شارة الأكثر طلباً.",
    )
    show_card_badge = models.BooleanField("إظهار شارة الباقة", default=True)
    show_card_duration = models.BooleanField("إظهار مدة الباقة", default=True)
    show_monthly_equivalent = models.BooleanField("إظهار المعادل الشهري", default=True)
    show_screen_limit = models.BooleanField("إظهار عدد الشاشات", default=True)
    sort_order = models.PositiveIntegerField(
        "ترتيب العرض",
        default=1,
    )

    class Meta:
        verbose_name = "خطة اشتراك"
        verbose_name_plural = "خطط الاشتراك"
        ordering = ("sort_order", "price")

    def __str__(self) -> str:
        return self.name


# The legacy ``core.SchoolSubscription`` model was removed once
# ``subscriptions.SchoolSubscription`` became the single source of truth.
# Nothing read the old table, and keeping two subscription models around was a
# standing source of confusion about which one governed access.


class SupportTicket(models.Model):
    STATUS_CHOICES = [
        ("open", "مفتوحة"),
        ("in_progress", "قيد المعالجة"),
        ("closed", "مغلقة"),
    ]
    PRIORITY_CHOICES = [
        ("low", "منخفضة"),
        ("medium", "متوسطة"),
        ("high", "عالية"),
        ("urgent", "عاجلة"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="support_tickets",
        verbose_name="المستخدم",
    )
    school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        related_name="support_tickets",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )
    subject = models.CharField("الموضوع", max_length=200)
    message = models.TextField("الرسالة")
    status = models.CharField(
        "الحالة",
        max_length=20,
        choices=STATUS_CHOICES,
        default="open",
    )
    priority = models.CharField(
        "الأولوية",
        max_length=20,
        choices=PRIORITY_CHOICES,
        default="medium",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "تذكرة دعم"
        verbose_name_plural = "تذاكر الدعم"
        ordering = ["-created_at"]

    def __str__(self):
        return self.subject


class TicketComment(models.Model):
    ticket = models.ForeignKey(
        SupportTicket,
        on_delete=models.CASCADE,
        related_name="comments",
        verbose_name="التذكرة",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        verbose_name="كاتب التعليق",
    )
    message = models.TextField("الرسالة")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "تعليق تذكرة"
        verbose_name_plural = "تعليقات التذاكر"
        ordering = ["created_at"]

    def __str__(self):
        return f"Comment by {self.user} on {self.ticket}"
