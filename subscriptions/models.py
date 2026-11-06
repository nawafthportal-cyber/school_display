from decimal import Decimal
from datetime import timedelta
import os
import uuid

from django.conf import settings
import secrets

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import School, SubscriptionPlan
from .receipt_storage import PrivateReceiptStorage


_RECEIPT_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}


def _validate_receipt_extension(value):
    if not value:
        return
    name = (getattr(value, "name", "") or "").strip()
    _, ext = os.path.splitext(name)
    ext = (ext or "").lower()
    if ext == "":
        return
    if ext not in _RECEIPT_ALLOWED_EXTS:
        raise ValidationError(
            f"امتداد الملف \"{ext.lstrip('.')}\" غير مسموح به. "
            f"الامتدادات المسموح بها: {', '.join(sorted(e.lstrip('.') for e in _RECEIPT_ALLOWED_EXTS))}.",
            code="invalid_extension",
        )


def _moyasar_reference() -> str:
    return f"MS-{uuid.uuid4().hex}"


def _tamara_reference() -> str:
    return f"TM-{uuid.uuid4().hex}"


class SchoolSubscription(models.Model):
    STATUS_CHOICES = [
        ("pending", "قيد الإعداد"),
        ("active", "سارية"),
        ("expired", "منتهية"),
        ("cancelled", "ملغاة"),
    ]
    CLOSURE_REASON_CHOICES = [
        ("budget", "الميزانية"),
        ("not_used", "ضعف الاستخدام"),
        ("technical", "مشكلة تقنية"),
        ("competitor", "الانتقال إلى منافس"),
        ("school_closed", "إغلاق أو دمج المدرسة"),
        ("no_response", "تعذر التواصل"),
        ("upgraded", "الترقية إلى باقة مدفوعة"),
        ("other", "سبب آخر"),
    ]

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        verbose_name="المدرسة",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="subscriptions",
        verbose_name="الخطة",
    )
    starts_at = models.DateField(
        default=timezone.localdate,
        verbose_name="بداية الاشتراك",
    )
    ends_at = models.DateField(
        null=True,
        blank=True,
        verbose_name="نهاية الاشتراك",
        help_text="اتركه فارغًا ليكون مفتوح المدة.",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="active",
        verbose_name="الحالة",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="ملاحظات",
    )
    closure_reason = models.CharField(
        "سبب الإلغاء أو عدم التجديد",
        max_length=30,
        choices=CLOSURE_REASON_CHOICES,
        blank=True,
    )
    closure_notes = models.TextField(
        "تفاصيل سبب الإلغاء أو عدم التجديد",
        blank=True,
    )
    closed_at = models.DateTimeField(
        "وقت تسجيل الإغلاق",
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="تاريخ الإنشاء",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="آخر تحديث",
    )

    class Meta:
        verbose_name = "اشتراك مدرسة"
        verbose_name_plural = "اشتراكات المدارس"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["school", "plan", "starts_at"],
                name="uniq_school_plan_start",
            )
        ]

    def __str__(self) -> str:
        return f"{self.school} - {self.plan} ({self.starts_at})"

    def save(self, *args, **kwargs):
        # إذا لم يُحدد تاريخ نهاية يدويًا، احسبه تلقائيًا من مدة الباقة.
        if not self.ends_at and self.starts_at and getattr(self, "plan", None):
            try:
                days = getattr(self.plan, "duration_days", None)
                if days is not None:
                    days_int = int(days)
                    if days_int > 0:
                        # حسب المطلوب: النهاية = تاريخ البداية + عدد الأيام
                        self.ends_at = self.starts_at + timedelta(days=days_int)
            except Exception:
                pass

        if self.status in {"cancelled", "expired"} and not self.closed_at:
            self.closed_at = timezone.now()
        elif self.status not in {"cancelled", "expired"}:
            self.closed_at = None
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"closed_at"}

        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        """
        هل الاشتراك ساري المفعول حاليًا؟
        يعتمد على الحالة والتاريخ.
        """
        today = timezone.localdate()

        if self.status != "active":
            return False

        if self.starts_at and self.starts_at > today:
            return False

        if self.ends_at and self.ends_at < today:
            return False

        return True

    @property
    def days_left(self):
        """
        عدد الأيام المتبقية حتى انتهاء الاشتراك.
        يرجع None إذا كان الاشتراك مفتوح المدة (بدون تاريخ انتهاء).
        """
        if not self.ends_at:
            return None  # اشتراك مفتوح المدة

        today = timezone.localdate()
        delta = (self.ends_at - today).days
        return delta if delta >= 0 else 0


class SubscriptionScreenAddon(models.Model):
    """شراء/إضافة عدد شاشات فوق حد الباقة لمدّة اشتراك معيّنة."""

    STATUS_CHOICES = [
        ("pending", "بانتظار الدفع"),
        ("paid", "مدفوع"),
        ("cancelled", "ملغي"),
        ("refunded", "مُسترد"),
    ]

    PRICING_STRATEGY_CHOICES = [
        ("auto_bundle", "تلقائي (شرائح)"),
        ("manual_bundle", "يدوي (مبلغ إجمالي)"),
        ("manual_per_screen", "يدوي (سعر لكل شاشة)"),
    ]

    PRICING_CYCLE_CHOICES = [
        ("inherit", "حسب الاشتراك"),
        ("monthly", "شهري"),
        ("semiannual", "نصف سنوي"),
        ("annual", "سنوي"),
    ]

    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.CASCADE,
        related_name="screen_addons",
        verbose_name="الاشتراك",
    )
    screens_added = models.PositiveIntegerField(
        verbose_name="عدد الشاشات المضافة",
        help_text="عدد الشاشات الإضافية المطلوبة لهذه الفترة.",
    )

    pricing_strategy = models.CharField(
        max_length=30,
        choices=PRICING_STRATEGY_CHOICES,
        default="auto_bundle",
        verbose_name="طريقة التسعير",
        help_text=(
            "التلقائي: يعتمد السعر الشهري المعتمد لكل شاشة إضافية، "
            "ويُضرب في معامل الدورة (نصف سنوي/سنوي) حسب إعدادات المنصة."
        ),
    )

    bundle_price = models.DecimalField(
        "سعر الإضافة للفترة (ر.س)",
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="يُستخدم مع (يدوي مبلغ إجمالي) أو يتم تعبئته تلقائيًا في (تلقائي شرائح).",
    )

    unit_price = models.DecimalField(
        "سعر للشاشة (ر.س)",
        max_digits=8,
        decimal_places=2,
        default=0,
    )
    proration_factor = models.DecimalField(
        "معامل الاحتساب النسبي",
        max_digits=6,
        decimal_places=5,
        default=Decimal("1.0"),
        help_text="1.0 = كامل المدة. يتم حسابه تلقائيًا عند الحفظ إذا كان للمدة نهاية.",
    )
    total_price = models.DecimalField(
        "الإجمالي (ر.س)",
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="يتم حسابه تلقائيًا: screens_added × unit_price × proration_factor.",
    )
    starts_at = models.DateField(
        default=timezone.localdate,
        verbose_name="بداية الإضافة",
    )

    validity_days = models.PositiveIntegerField(
        verbose_name="مدة الصلاحية بالأيام",
        null=True,
        blank=True,
        help_text="اختياري: لو حددتها ستصبح نهاية الإضافة = البداية + (المدة-1). مثال شهر: 30 يوم.",
    )

    pricing_cycle = models.CharField(
        max_length=20,
        choices=PRICING_CYCLE_CHOICES,
        default="inherit",
        verbose_name="دورة تسعير الإضافة",
        help_text="مثال: اشتراك سنوي لكن إضافة لمدة شهر → اختر (شهري) وحدد مدة 30 يوم.",
    )
    ends_at = models.DateField(
        null=True,
        blank=True,
        verbose_name="نهاية الإضافة",
        help_text="افتراضيًا تُضبط على نهاية الاشتراك إن وُجدت.",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="paid",
        verbose_name="الحالة",
    )
    notes = models.TextField(
        blank=True,
        verbose_name="ملاحظات",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="تاريخ الإنشاء",
    )

    class Meta:
        verbose_name = "زيادة شاشات"
        verbose_name_plural = "زيادات الشاشات"
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.subscription.school} +{self.screens_added} شاشة"

    @property
    def is_effective_today(self) -> bool:
        today = timezone.localdate()
        if self.status != "paid":
            return False
        if self.starts_at and self.starts_at > today:
            return False
        if self.ends_at and self.ends_at < today:
            return False
        return True

    def _calc_proration_factor(self, as_of=None) -> Decimal:
        """احتساب نسبي بناءً على نافذة صلاحية الإضافة (starts_at→ends_at).

        - إذا لم يكن هناك ends_at → 1.0
        - يتم تثبيت الحساب على تاريخ الشراء/البداية (as_of)، وليس "اليوم"، لتجنب تغيّر الفاتورة لاحقًا.
        """
        as_of_date = as_of or self.starts_at or timezone.localdate()

        if not self.ends_at or not self.starts_at:
            return Decimal("1.0")

        period_days = (self.ends_at - self.starts_at).days + 1
        if period_days <= 0:
            return Decimal("1.0")

        remaining_days = (self.ends_at - as_of_date).days + 1
        if remaining_days <= 0:
            return Decimal("0")

        factor = (Decimal(remaining_days) / Decimal(period_days))
        if factor < 0:
            return Decimal("0")
        if factor > 1:
            return Decimal("1")
        return factor

    @staticmethod
    def _annual_multiplier() -> Decimal:
        return Decimal(str(getattr(settings, "SCREEN_ADDON_ANNUAL_MULTIPLIER", 10)))

    @staticmethod
    def _semiannual_multiplier() -> Decimal:
        return Decimal(str(getattr(settings, "SCREEN_ADDON_SEMIANNUAL_MULTIPLIER", 6)))

    def _infer_subscription_cycle_multiplier(self) -> Decimal:
        """استنتاج معامل مدة الاشتراك من طول الاشتراك (fallback عند اختيار inherit)."""
        sub_start = getattr(self.subscription, "starts_at", None)
        sub_end = getattr(self.subscription, "ends_at", None)
        if not sub_start or not sub_end:
            return Decimal("1")

        days = (sub_end - sub_start).days + 1
        # حدود مرنة لتغطية اختلاف الأشهر/السنة
        if days >= 330:
            return self._annual_multiplier()
        if days >= 150:
            return self._semiannual_multiplier()
        return Decimal("1")

    def _cycle_multiplier(self) -> Decimal:
        """معامل دورة تسعير الإضافة."""
        c = (self.pricing_cycle or "inherit").strip().lower()
        if c == "annual":
            return self._annual_multiplier()
        if c == "semiannual":
            return self._semiannual_multiplier()
        if c == "monthly":
            return Decimal("1")
        return self._infer_subscription_cycle_multiplier()

    def _calc_auto_monthly_bundle_price(self) -> Decimal:
        """سعر شهري واضح وثابت لكل شاشة إضافية."""
        n = int(self.screens_added or 0)
        if n <= 0:
            return Decimal("0")
        unit = Decimal(str(getattr(settings, "SCREEN_ADDON_MONTHLY_PRICE", 60)))
        return Decimal(n) * unit

    def _calc_auto_bundle_price_for_cycle(self) -> Decimal:
        monthly = self._calc_auto_monthly_bundle_price()
        return (monthly * self._cycle_multiplier()).quantize(Decimal("0.01"))

    def _default_ends_at_for_cycle(self):
        """يضبط ends_at تلقائيًا عند عدم تحديدها.

        - لو validity_days موجودة → تعتمد عليها
        - لو دورة الإضافة محددة (شهري/نصف سنوي/سنوي) → مدة ثابتة تقريبية
        - وإلا → توريث نهاية الاشتراك إن وُجدت
        """
        if self.starts_at and self.validity_days:
            try:
                days = int(self.validity_days)
                if days > 0:
                    return self.starts_at + timedelta(days=days - 1)
            except Exception:
                pass

        c = (self.pricing_cycle or "inherit").strip().lower()
        if self.starts_at:
            if c == "monthly":
                return self.starts_at + timedelta(days=30 - 1)
            if c == "semiannual":
                return self.starts_at + timedelta(days=182 - 1)
            if c == "annual":
                return self.starts_at + timedelta(days=365 - 1)

        try:
            return getattr(self.subscription, "ends_at", None)
        except Exception:
            return None

    def _calc_total_price(self) -> Decimal:
        factor = Decimal(str(self.proration_factor or 0))

        if self.pricing_strategy == "manual_per_screen":
            unit = Decimal(str(self.unit_price or 0))
            qty = Decimal(int(self.screens_added or 0))
            total = unit * qty * factor
            return total.quantize(Decimal("0.01"))

        # bundle strategies
        bundle = Decimal(str(self.bundle_price or 0))
        total = bundle * factor
        return total.quantize(Decimal("0.01"))

    def save(self, *args, **kwargs):
        # default ends_at
        if self.ends_at is None:
            self.ends_at = self._default_ends_at_for_cycle()

        # compute proration (freeze to starts_at)
        self.proration_factor = self._calc_proration_factor(as_of=self.starts_at)

        # auto pricing (bundle) if selected
        if self.pricing_strategy == "auto_bundle":
            self.bundle_price = self._calc_auto_bundle_price_for_cycle()

        # compute total
        self.total_price = self._calc_total_price()

        super().save(*args, **kwargs)


class SubscriptionRequest(models.Model):
    REQUEST_TYPE_CHOICES = [
        ("renewal", "طلب تجديد"),
        ("new", "طلب اشتراك جديد"),
    ]

    STATUS_CHOICES = [
        ("submitted", "مرسلة"),
        ("under_review", "قيد المراجعة"),
        ("approved", "معتمدة"),
        ("rejected", "مرفوضة"),
    ]

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="subscription_requests",
        verbose_name="المدرسة",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_subscription_requests",
        verbose_name="أُنشئ بواسطة",
    )

    request_type = models.CharField(
        max_length=20,
        choices=REQUEST_TYPE_CHOICES,
        verbose_name="نوع الطلب",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="subscription_requests",
        verbose_name="الخطة المطلوبة",
    )

    # حسب طلبك: يبدأ التجديد من تاريخ رفع الطلب (يُثبت عند الإرسال)
    requested_starts_at = models.DateField(
        default=timezone.localdate,
        verbose_name="تاريخ بدء التفعيل المطلوب",
    )

    amount = models.DecimalField(
        "مبلغ التحويل (ر.س)",
        max_digits=10,
        decimal_places=2,
        default=0,
    )
    receipt_image = models.ImageField(
        storage=PrivateReceiptStorage(),
        upload_to="receipts/subscription_requests/%Y/%m",
        max_length=500,
        blank=True,
        verbose_name="إيصال التحويل (صورة)",
        validators=[_validate_receipt_extension],
    )
    transfer_note = models.CharField(
        "ملاحظة العميل",
        max_length=255,
        blank=True,
        default="",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="submitted",
        verbose_name="الحالة",
    )
    admin_note = models.TextField(
        "ملاحظة الإدارة",
        blank=True,
        default="",
    )

    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_subscription_requests",
        verbose_name="تمت المراجعة بواسطة",
    )
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="تاريخ المراجعة",
    )
    approved_subscription = models.ForeignKey(
        "subscriptions.SchoolSubscription",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_from_requests",
        verbose_name="الاشتراك المُنشأ",
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ الإنشاء")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="آخر تحديث")

    class Meta:
        verbose_name = "طلب اشتراك/تجديد"
        verbose_name_plural = "طلبات الاشتراك/التجديد"
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.school} - {self.get_request_type_display()} ({self.get_status_display()})"


class SubscriptionPaymentOperation(models.Model):
    """سجل عمليات الدفع/التفعيل للاشتراكات.

    الهدف: عند إنشاء اشتراك يدويًا من لوحة النظام، نسجل طريقة الدفع
    حتى يظهر لدينا تاريخ واضح لآلية الدفع.
    """

    # الفاتورة تصدر عن بوابتي الدفع المعتمدتين أو اعتماد يدوي لتحويل بنكي.
    METHOD_CHOICES = [
        ("bank_transfer", "تحويل بنكي"),
        ("tamara", "تمارا"),
        ("moyasar", "ميسر"),
    ]

    SOURCE_CHOICES = [
        ("admin_manual", "إضافة يدوية"),
        ("request", "طلب اشتراك"),
    ]

    subscription_request = models.OneToOneField(
        SubscriptionRequest, null=True, blank=True, on_delete=models.PROTECT,
        related_name="payment_operation", verbose_name="طلب التحويل المعتمد",
    )

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="subscription_payment_operations",
        verbose_name="المدرسة",
    )
    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.CASCADE,
        related_name="payment_operations",
        verbose_name="الاشتراك",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="payment_operations",
        verbose_name="الخطة",
    )

    amount = models.DecimalField(
        "المبلغ (ر.س)",
        max_digits=10,
        decimal_places=2,
        default=0,
    )
    method = models.CharField(
        "طريقة الدفع",
        max_length=20,
        choices=METHOD_CHOICES,
    )
    source = models.CharField(
        "مصدر العملية",
        max_length=20,
        choices=SOURCE_CHOICES,
        default="admin_manual",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_subscription_payment_operations",
        verbose_name="تمت بواسطة",
    )
    note = models.CharField(
        "ملاحظة",
        max_length=255,
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ العملية")

    class Meta:
        verbose_name = "عملية دفع اشتراك"
        verbose_name_plural = "عمليات دفع الاشتراكات"
        ordering = ("-created_at", "-id")

    def __str__(self) -> str:
        return f"{self.school} - {self.plan} - {self.get_method_display()}"


class TamaraCheckout(models.Model):
    REQUEST_TYPE_CHOICES = SubscriptionRequest.REQUEST_TYPE_CHOICES
    STATUS_CHOICES = [
        ("initiated", "بدأت"),
        ("new", "بانتظار الدفع"),
        ("approved", "تمت الموافقة"),
        ("authorised", "مصرح بها"),
        ("captured", "مكتملة"),
        ("declined", "مرفوضة"),
        ("canceled", "ملغاة"),
        ("expired", "منتهية"),
        ("refunded", "مستردة"),
        ("error", "خطأ"),
    ]

    merchant_reference = models.CharField(
        "مرجع التاجر",
        max_length=40,
        unique=True,
        default=_tamara_reference,
        editable=False,
    )
    tamara_order_id = models.CharField(
        "رقم طلب تمارا",
        max_length=80,
        unique=True,
        null=True,
        blank=True,
    )
    checkout_id = models.CharField("رقم جلسة الدفع", max_length=80, blank=True, default="")
    checkout_url = models.URLField("رابط الدفع", max_length=2048, blank=True, default="")
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="tamara_checkouts",
        verbose_name="المدرسة",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tamara_checkouts",
        verbose_name="أنشئت بواسطة",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="tamara_checkouts",
        verbose_name="الخطة",
    )
    request_type = models.CharField(max_length=20, choices=REQUEST_TYPE_CHOICES)
    starts_at = models.DateField("بداية الاشتراك المطلوبة", default=timezone.localdate)
    amount = models.DecimalField("المبلغ", max_digits=10, decimal_places=2)
    currency = models.CharField("العملة", max_length=3, default="SAR")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="initiated")
    last_event = models.CharField("آخر حدث", max_length=40, blank=True, default="")
    error_message = models.CharField("رسالة الخطأ", max_length=300, blank=True, default="")
    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tamara_checkouts",
    )
    payment_operation = models.OneToOneField(
        SubscriptionPaymentOperation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tamara_checkout",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "جلسة دفع تمارا"
        verbose_name_plural = "جلسات دفع تمارا"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["school", "status", "created_at"], name="tamara_school_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.merchant_reference} - {self.get_status_display()}"



class MoyasarCheckout(models.Model):
    """Local order used to verify Moyasar payments before activating access."""

    # "screens" has no SubscriptionRequest equivalent: it tops up an existing
    # term rather than starting or renewing one.
    REQUEST_TYPE_CHOICES = [
        *SubscriptionRequest.REQUEST_TYPE_CHOICES,
        ("screens", "شراء شاشات إضافية"),
    ]
    STATUS_CHOICES = [
        ("initiated", "بانتظار الدفع"),
        ("paid", "مدفوعة"),
        ("authorized", "مصرح بها"),
        ("captured", "مكتملة"),
        ("failed", "فشلت"),
        ("refunded", "مستردة"),
        ("voided", "ملغاة"),
        ("error", "خطأ"),
    ]

    merchant_reference = models.CharField(
        "مرجع التاجر",
        max_length=40,
        unique=True,
        default=_moyasar_reference,
        editable=False,
    )
    payment_id = models.CharField(
        "رقم دفعة ميسر",
        max_length=80,
        unique=True,
        null=True,
        blank=True,
    )
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="moyasar_checkouts",
        verbose_name="المدرسة",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="moyasar_checkouts",
        verbose_name="أنشئت بواسطة",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="moyasar_checkouts",
        verbose_name="الخطة",
    )
    request_type = models.CharField(max_length=20, choices=REQUEST_TYPE_CHOICES)
    starts_at = models.DateField("بداية الاشتراك المطلوبة", default=timezone.localdate)
    extra_screens = models.PositiveIntegerField(
        "شاشات إضافية",
        default=0,
        help_text="عدد الشاشات المشتراة فوق حد الباقة ضمن هذه العملية.",
    )
    screen_addon_validity_days = models.PositiveIntegerField(
        "مدة الشاشات الإضافية بالأيام",
        null=True,
        blank=True,
        help_text="مدة شراء الشاشات الإضافية كما تم احتسابها عند الدفع.",
    )
    screen_addon_ends_at = models.DateField(
        "نهاية صلاحية الشاشات الإضافية",
        null=True,
        blank=True,
        help_text="تُستخدم لطلبات شراء الشاشات فقط حتى تطابق التفعيل قيمة الدفع.",
    )
    amount = models.DecimalField("المبلغ", max_digits=10, decimal_places=2)
    discount_code = models.ForeignKey(
        "subscriptions.DiscountCode",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="checkouts",
        verbose_name="كود الخصم",
    )
    discount_amount = models.DecimalField(
        "قيمة الخصم",
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="المبلغ المخصوم من الإجمالي قبل إنشاء الدفعة.",
    )
    currency = models.CharField("العملة", max_length=3, default="SAR")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="initiated")
    live_mode = models.BooleanField("عملية فعلية", default=False)
    last_event = models.CharField("آخر حدث", max_length=40, blank=True, default="")
    error_message = models.CharField("رسالة الخطأ", max_length=300, blank=True, default="")
    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="moyasar_checkouts",
    )
    payment_operation = models.OneToOneField(
        SubscriptionPaymentOperation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="moyasar_checkout",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "جلسة دفع ميسر"
        verbose_name_plural = "جلسات دفع ميسر"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["school", "status", "created_at"], name="moyasar_school_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.merchant_reference} - {self.get_status_display()}"

class SubscriptionInvoice(models.Model):
    """فاتورة إلكترونية محفوظة (HTML snapshot) مرتبطة بعملية دفع اشتراك."""

    CURRENCY_CHOICES = [
        ("SAR", "ر.س"),
    ]

    operation = models.OneToOneField(
        SubscriptionPaymentOperation,
        on_delete=models.PROTECT,
        related_name="invoice",
        verbose_name="عملية الدفع",
    )

    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="subscription_invoices",
        verbose_name="المدرسة",
    )
    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.PROTECT,
        related_name="invoices",
        verbose_name="الاشتراك",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="subscription_invoices",
        verbose_name="الخطة",
    )

    invoice_number = models.CharField(
        "رقم الفاتورة",
        max_length=40,
        unique=True,
        editable=False,
    )
    issued_at = models.DateTimeField(
        "تاريخ الإصدار",
        default=timezone.now,
    )

    currency = models.CharField(
        "العملة",
        max_length=3,
        choices=CURRENCY_CHOICES,
        default="SAR",
    )
    amount = models.DecimalField(
        "المبلغ (ر.س)",
        max_digits=10,
        decimal_places=2,
        default=0,
    )
    payment_method = models.CharField(
        "طريقة الدفع",
        max_length=20,
        choices=SubscriptionPaymentOperation.METHOD_CHOICES,
    )

    seller_name = models.CharField(
        "الطرف الأول (المنصة)",
        max_length=200,
        blank=True,
        default="",
    )
    buyer_name = models.CharField(
        "الطرف الثاني (المدرسة)",
        max_length=200,
        blank=True,
        default="",
    )

    html_snapshot = models.TextField(
        "نسخة HTML",
        blank=True,
        default="",
        help_text="يُحفظ HTML النهائي للفاتورة لضمان ثباتها حتى لو تغيّر تصميم القالب لاحقًا.",
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ الإنشاء")

    class Meta:
        verbose_name = "فاتورة اشتراك"
        verbose_name_plural = "فواتير الاشتراكات"
        ordering = ("-issued_at", "-id")

    def __str__(self) -> str:
        return self.invoice_number

    @staticmethod
    def _make_invoice_number() -> str:
        # رقم فريد وبسيط للقراءة (بدون الاعتماد على تسلسل DB)
        token = secrets.token_hex(4).upper()
        stamp = timezone.now().strftime("%Y%m%d")
        return f"INV-{stamp}-{token}"

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            # حاول عدة مرات لضمان uniqueness
            for _ in range(10):
                cand = self._make_invoice_number()
                if not SubscriptionInvoice.objects.filter(invoice_number=cand).exclude(pk=self.pk).exists():
                    self.invoice_number = cand
                    break
        super().save(*args, **kwargs)


class SubscriptionRefund(models.Model):
    """A recorded refund against a payment operation.

    Refunds are tracked locally even when the money movement happens in the
    gateway dashboard, so the platform's own books stay authoritative.
    """

    STATUS_CHOICES = [
        ("pending", "قيد التنفيذ"),
        ("completed", "منفذ"),
        ("failed", "فشل"),
    ]

    REASON_CHOICES = [
        ("duplicate", "دفعة مكررة"),
        ("service_issue", "مشكلة في الخدمة"),
        ("cancelled_early", "إلغاء مبكر"),
        ("billing_error", "خطأ في الفوترة"),
        ("goodwill", "بادرة حسن نية"),
        ("other", "سبب آخر"),
    ]

    operation = models.ForeignKey(
        SubscriptionPaymentOperation,
        on_delete=models.PROTECT,
        related_name="refunds",
        verbose_name="عملية الدفع",
    )
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="subscription_refunds",
        verbose_name="المدرسة",
    )
    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.PROTECT,
        related_name="refunds",
        verbose_name="الاشتراك",
        null=True,
        blank=True,
    )

    amount = models.DecimalField(
        "المبلغ المسترد (ر.س)",
        max_digits=10,
        decimal_places=2,
    )
    reason = models.CharField(
        "سبب الاسترداد",
        max_length=30,
        choices=REASON_CHOICES,
        default="other",
    )
    status = models.CharField(
        "الحالة",
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
    )
    notes = models.TextField("ملاحظات", blank=True, default="")
    gateway_reference = models.CharField(
        "مرجع الاسترداد لدى المزود",
        max_length=120,
        blank=True,
        default="",
    )
    revokes_access = models.BooleanField(
        "إيقاف الاشتراك المرتبط",
        default=False,
        help_text="عند التفعيل يتم إلغاء الاشتراك المرتبط بهذه العملية.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_subscription_refunds",
        verbose_name="سُجل بواسطة",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ التسجيل")
    completed_at = models.DateTimeField("تاريخ التنفيذ", null=True, blank=True)

    class Meta:
        verbose_name = "استرداد اشتراك"
        verbose_name_plural = "استردادات الاشتراكات"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["school", "status"], name="refund_school_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.school} - {self.amount} ر.س ({self.get_status_display()})"

    def clean(self):
        super().clean()
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "مبلغ الاسترداد يجب أن يكون أكبر من صفر."})

        operation = getattr(self, "operation", None)
        if operation is not None and self.amount is not None:
            already = (
                SubscriptionRefund.objects.filter(operation=operation)
                .exclude(pk=self.pk)
                .exclude(status="failed")
                .aggregate(total=models.Sum("amount"))
                .get("total")
                or Decimal("0")
            )
            if already + self.amount > operation.amount:
                raise ValidationError(
                    {"amount": "إجمالي المبالغ المستردة يتجاوز قيمة عملية الدفع."}
                )

    def save(self, *args, **kwargs):
        if self.status == "completed" and not self.completed_at:
            self.completed_at = timezone.now()
        elif self.status != "completed":
            self.completed_at = None
        super().save(*args, **kwargs)


class SubscriptionAuditLog(models.Model):
    """Append-only record of who changed billing state and when."""

    ACTION_CHOICES = [
        ("subscription_created", "إنشاء اشتراك"),
        ("subscription_updated", "تعديل اشتراك"),
        ("subscription_cancelled", "إلغاء اشتراك"),
        ("subscription_expired", "انتهاء اشتراك"),
        ("payment_recorded", "تسجيل دفعة"),
        ("payment_reconciled", "تسوية دفعة"),
        ("refund_recorded", "تسجيل استرداد"),
        ("refund_completed", "تنفيذ استرداد"),
        ("screens_added", "إضافة شاشات"),
    ]

    action = models.CharField("الإجراء", max_length=40, choices=ACTION_CHOICES, db_index=True)
    school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        related_name="subscription_audit_logs",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )
    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        verbose_name="الاشتراك",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="subscription_audit_logs",
        verbose_name="المنفّذ",
        null=True,
        blank=True,
    )
    # Kept denormalised so the trail survives account deletion.
    actor_label = models.CharField("اسم المنفّذ", max_length=150, blank=True, default="")
    amount = models.DecimalField(
        "المبلغ",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    summary = models.CharField("الوصف", max_length=500, blank=True, default="")
    context = models.JSONField("تفاصيل", default=dict, blank=True)
    ip_address = models.CharField("عنوان IP", max_length=45, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "سجل تدقيق اشتراك"
        verbose_name_plural = "سجلات تدقيق الاشتراكات"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["school", "-created_at"], name="audit_school_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} - {self.school} - {self.created_at:%Y-%m-%d %H:%M}"


class SubscriptionEmailNotification(models.Model):
    """Durable outbox for customer invoice and subscription email messages."""

    class EventType(models.TextChoices):
        INVOICE = "invoice", "فاتورة اشتراك"
        EXPIRY = "expiry", "تنبيه قرب انتهاء الاشتراك"
        VERIFY_EMAIL = "verify_email", "توثيق البريد الإلكتروني"

    class Status(models.TextChoices):
        PENDING = "pending", "بانتظار الإرسال"
        PROCESSING = "processing", "جارٍ الإرسال"
        SENT = "sent", "تم الإرسال"
        SKIPPED = "skipped", "تم التجاوز"
        FAILED = "failed", "فشل نهائي"

    event_type = models.CharField(max_length=20, choices=EventType.choices)
    subscription = models.ForeignKey(
        SchoolSubscription,
        on_delete=models.CASCADE,
        related_name="email_notifications",
    )
    invoice = models.ForeignKey(
        SubscriptionInvoice,
        on_delete=models.CASCADE,
        related_name="email_notifications",
        null=True,
        blank=True,
    )
    recipient = models.EmailField(max_length=254)
    reminder_days = models.PositiveSmallIntegerField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    locked_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            models.Index(
                fields=("status", "available_at"),
                name="sub_email_status_available_idx",
            )
        ]
        verbose_name = "إشعار بريد اشتراك"
        verbose_name_plural = "إشعارات بريد الاشتراكات"

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} - {self.recipient}"


class DiscountCode(models.Model):
    """كود خصم يُنشئه مدير المنصة ويُطبق على إجمالي عملية الدفع.

    الكود يتوقف من تلقاء نفسه في حالتين: انتهاء مدة السماح، أو استنفاد عدد
    الاستخدامات المصدرة. الإيقاف اليدوي متاح عبر ``is_active``.
    """

    TYPE_PERCENT = "percent"
    TYPE_AMOUNT = "amount"
    TYPE_CHOICES = [
        (TYPE_PERCENT, "نسبة مئوية"),
        (TYPE_AMOUNT, "مبلغ ثابت"),
    ]

    code = models.CharField(
        "الكود",
        max_length=40,
        unique=True,
        help_text="يُحفظ ويُقارن بأحرف كبيرة دون مسافات.",
    )
    discount_type = models.CharField("نوع الخصم", max_length=10, choices=TYPE_CHOICES)
    percent = models.DecimalField(
        "نسبة الخصم %",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="بين 0.01 و 100 عند اختيار نوع النسبة.",
    )
    amount = models.DecimalField(
        "مبلغ الخصم (ر.س)",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="مبلغ ثابت يُخصم من الإجمالي عند اختيار نوع المبلغ.",
    )
    valid_from = models.DateTimeField("بداية السماح", default=timezone.now)
    valid_until = models.DateTimeField(
        "نهاية السماح",
        null=True,
        blank=True,
        help_text="اتركه فارغاً لعدم تحديد نهاية زمنية.",
    )
    max_uses = models.PositiveIntegerField(
        "عدد الأكواد المصدرة",
        help_text="إجمالي مرات الاستخدام المسموحة؛ عند بلوغه يتوقف الكود تلقائياً.",
    )
    is_active = models.BooleanField("مفعّل", default=True)
    plans = models.ManyToManyField(
        SubscriptionPlan,
        blank=True,
        related_name="discount_codes",
        verbose_name="الباقات المشمولة",
        help_text="اتركها فارغة ليصلح الكود لكل الباقات.",
    )
    notes = models.CharField("ملاحظات", max_length=200, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discount_codes_created",
        verbose_name="أنشئ بواسطة",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "كود خصم"
        verbose_name_plural = "أكواد الخصم"
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                name="discount_percent_range",
                check=(
                    models.Q(discount_type="amount")
                    | (models.Q(percent__gt=0) & models.Q(percent__lte=100))
                ),
            ),
            models.CheckConstraint(
                name="discount_amount_positive",
                check=models.Q(discount_type="percent") | models.Q(amount__gt=0),
            ),
        ]

    def __str__(self) -> str:
        return self.code

    @staticmethod
    def normalize(raw: str | None) -> str:
        return (raw or "").strip().upper()

    def clean(self):
        super().clean()
        self.code = self.normalize(self.code)
        if not self.code:
            raise ValidationError({"code": "أدخل كود الخصم."})
        if self.discount_type == self.TYPE_PERCENT:
            if not self.percent or self.percent <= 0 or self.percent > 100:
                raise ValidationError({"percent": "أدخل نسبة بين 0.01 و 100."})
        elif self.discount_type == self.TYPE_AMOUNT:
            if not self.amount or self.amount <= 0:
                raise ValidationError({"amount": "أدخل مبلغ خصم أكبر من صفر."})
        if self.valid_until and self.valid_from and self.valid_until <= self.valid_from:
            raise ValidationError({"valid_until": "نهاية السماح يجب أن تكون بعد بدايته."})

    # -----------------------------
    # Availability helpers
    # -----------------------------
    @property
    def used_count(self) -> int:
        return int(self.redemptions.count())

    @property
    def remaining_uses(self) -> int:
        return max(0, int(self.max_uses) - self.used_count)

    def is_within_window(self, now=None) -> bool:
        now = now or timezone.now()
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now >= self.valid_until:
            return False
        return True

    def is_usable(self, now=None) -> bool:
        return bool(self.is_active) and self.is_within_window(now) and self.remaining_uses > 0

    def allows_plan(self, plan) -> bool:
        """هل يصلح الكود لهذه الباقة؟ قائمة فارغة تعني كل الباقات."""
        if not self.pk or not self.plans.exists():
            return True
        plan_id = getattr(plan, "pk", plan)
        if plan_id is None:
            return False
        return self.plans.filter(pk=plan_id).exists()

    def discount_for(self, total: Decimal) -> Decimal:
        """قيمة الخصم لهذا الكود على إجمالي معيّن (لا تتجاوز الإجمالي)."""
        total = Decimal(total or 0)
        if total <= 0:
            return Decimal("0.00")
        if self.discount_type == self.TYPE_PERCENT:
            cut = (total * Decimal(self.percent or 0) / Decimal("100"))
        else:
            cut = Decimal(self.amount or 0)
        if cut > total:
            cut = total
        return cut.quantize(Decimal("0.01"))


class DiscountRedemption(models.Model):
    """تسجيل استخدام كود خصم من مدرسة في عملية دفع مكتملة."""

    discount_code = models.ForeignKey(
        DiscountCode,
        on_delete=models.PROTECT,
        related_name="redemptions",
        verbose_name="كود الخصم",
    )
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="discount_redemptions",
        verbose_name="المدرسة",
    )
    checkout = models.OneToOneField(
        "subscriptions.MoyasarCheckout",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="discount_redemption",
        verbose_name="جلسة الدفع",
    )
    amount_discounted = models.DecimalField("قيمة الخصم", max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "استخدام كود خصم"
        verbose_name_plural = "استخدامات أكواد الخصم"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("discount_code", "school"),
                name="uq_discount_once_per_school",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.discount_code_id} → {self.school_id}"
