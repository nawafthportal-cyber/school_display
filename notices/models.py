# notices/models.py
from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any, Optional

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import DisplayScreen, School
from core.occasions import announcement_choices as occasion_announcement_choices
from core.occasion_customizations import OccasionSelectionField


ANNOUNCEMENT_LEVELS = (
    ("urgent", "عاجل"),
    ("warning", "تنبيه"),
    ("info", "معلومة"),
    ("success", "تهنئة"),
)


# ===========================
# Helpers
# ===========================

def _excellence_upload_to(instance: "Excellence", filename: str) -> str:
    """
    مسار رفع صورة التميّز:
        excellence/YYYY/MM/<uuid4><ext>
    - يحافظ على الامتداد الأصلي قدر الإمكان.
    - يضمن اسمًا فريدًا.
    """
    _, ext = os.path.splitext(filename)
    ext = (ext or "").lower()[:10]  # تنظيف بسيط للاسم
    now = datetime.now()
    return f"excellence/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext or '.jpg'}"


_EXCELLENCE_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _validate_excellence_photo_extension(value: Any) -> None:
    """
    يتحقق من امتداد الملف للصور المرفوعة لبطاقة التميز.

    ملاحظة توافقية:
    بعض السجلات القديمة قد تحتوي اسم ملف بدون امتداد (كما في التخزين سابقًا)،
    وعند تعديل السجل بدون رفع صورة جديدة يعيد Django تمرير الملف الحالي عبر
    Validators مما يسبب خطأ "امتداد الملف \"\"". لذلك نسمح بالامتداد الفارغ.
    """
    if not value:
        return

    name = (getattr(value, "name", "") or "").strip()
    _, ext = os.path.splitext(name)
    ext = (ext or "").lower()

    # ✅ توافق: اسم ملف بدون امتداد
    if ext == "":
        return

    if ext not in _EXCELLENCE_ALLOWED_EXTS:
        raise ValidationError(
            f"امتداد الملف \"{ext.lstrip('.')}\" غير مسموح به. "
            f"الامتدادات المسموح بها هي: {', '.join([e.lstrip('.') for e in sorted(_EXCELLENCE_ALLOWED_EXTS)])}.",
            code="invalid_extension",
        )


# ===========================
# Announcement
# ===========================

class AnnouncementQuerySet(models.QuerySet):
    """
    QuerySet مخصص لإعلانات المدرسة مع فلاتر جاهزة:
    - active()           -> الإعلانات المفعلة زمنيًا.
    - for_school(school) -> إعلانات مدرسة معينة + العامة (school is null).
    - active_for_school  -> مزيج الاثنين معًا.
    """

    def active(self, now: Optional[datetime] = None) -> "AnnouncementQuerySet":
        now = now or timezone.now()
        qs = self.filter(is_active=True)

        # يبدأ قبل الآن (أو بدون starts_at)
        qs = qs.filter(
            models.Q(starts_at__lte=now) | models.Q(starts_at__isnull=True)
        )

        # لم ينتهِ أو لا يوجد expires_at
        qs = qs.filter(
            models.Q(expires_at__gt=now) | models.Q(expires_at__isnull=True)
        )

        return qs

    def for_school(self, school: Optional[School]) -> "AnnouncementQuerySet":
        """
        ترجع الإعلانات الخاصة بالمدرسة + الإعلانات العامة (school is null).
        """
        if school is None:
            return self.none()
        return self.filter(
            models.Q(school=school) | models.Q(school__isnull=True)
        )

    def active_for_school(
        self,
        school: Optional[School],
        now: Optional[datetime] = None,
    ) -> "AnnouncementQuerySet":
        return self.for_school(school).active(now=now)


class Announcement(models.Model):
    # مشتقة من سجل المناسبات الموحّد. انظر :mod:`core.occasions`.
    OCCASION_THEME_CHOICES = occasion_announcement_choices()

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="announcements",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )
    title = models.CharField("العنوان", max_length=120)
    body = models.CharField("النص", max_length=280, blank=True)
    level = models.CharField(
        "المستوى",
        max_length=10,
        choices=ANNOUNCEMENT_LEVELS,
        default="info",
    )
    occasion_theme = OccasionSelectionField(
        "ثيم المناسبة",
        max_length=30,
        choices=OCCASION_THEME_CHOICES,
        blank=True,
        default="",
        help_text="يغيّر مظهر شاشة العرض طوال فترة نشاط هذا الإعلان.",
    )
    screens = models.ManyToManyField(
        DisplayScreen,
        related_name="announcements",
        verbose_name="شاشات محددة",
        blank=True,
        help_text="اتركها فارغة لعرض التنبيه على جميع شاشات المدرسة.",
    )
    starts_at = models.DateTimeField("يظهر من", default=timezone.now)
    expires_at = models.DateTimeField("ينتهي في", blank=True, null=True)
    is_active = models.BooleanField("مفعّل", default=True)

    objects: AnnouncementQuerySet = AnnouncementQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        verbose_name = "تنبيه"
        verbose_name_plural = "تنبيهات"
        ordering = ("-starts_at",)
        indexes = [
            models.Index(fields=["school", "is_active"], name="ann_school_active_idx"),
            models.Index(fields=["school", "starts_at"], name="ann_school_starts_idx"),
            models.Index(fields=["school", "expires_at"], name="ann_school_expires_idx"),
        ]

    def __str__(self) -> str:
        return f"[{self.get_level_display()}] {self.title}"

    # --------- منطق التفعيل ---------

    @property
    def active_now(self) -> bool:
        """
        هل التنبيه فعّال حاليًا بناءً على الوقت والفلاغ is_active؟
        """
        if not self.is_active:
            return False

        now = timezone.now()
        if self.starts_at and now < self.starts_at:
            return False
        if self.expires_at and now >= self.expires_at:
            return False
        return True

    # --------- دوال مساعدة للـ API / الواجهة ---------

    @classmethod
    def active_for_school(cls, school: Optional[School]) -> "AnnouncementQuerySet":
        """
        واجهة مختصرة تستخدم في الـ API:
            Announcement.active_for_school(request.school)
        """
        return cls.objects.active_for_school(school)

    def as_dict(self) -> dict[str, Any]:
        """
        تمثيل بسيط للاستخدام في JSON (شريط التنبيهات في شاشة العرض).
        نحافظ على أسماء مفهومة ويمكن للواجهة استخدامها مباشرة.
        """
        return {
            "id": self.id,
            "school_id": self.school_id,
            "title": self.title,
            "body": self.body,
            "level": self.level,
            "level_label": self.get_level_display(),
            "occasion_theme": self.occasion_theme,
            "occasion_theme_label": self.get_occasion_theme_display() if self.occasion_theme else "",
            "screen_ids": list(self.screens.values_list("id", flat=True)),
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "active": self.active_now,
        }


class EmergencyAlert(models.Model):
    KIND_EVACUATION = "evacuation"
    KIND_FIRE = "fire"
    KIND_WEATHER = "weather"
    KIND_SUSPENSION = "suspension"
    KIND_URGENT = "urgent"
    KIND_CHOICES = [
        (KIND_EVACUATION, "إخلاء"),
        (KIND_FIRE, "حريق"),
        (KIND_WEATHER, "حالة جوية"),
        (KIND_SUSPENSION, "تعليق دراسة"),
        (KIND_URGENT, "رسالة عاجلة"),
    ]

    kind = models.CharField("نوع التنبيه", max_length=20, choices=KIND_CHOICES)
    title = models.CharField("العنوان", max_length=120)
    message = models.TextField("الرسالة", max_length=600)
    schools = models.ManyToManyField(
        School,
        related_name="emergency_alerts",
        verbose_name="المدارس",
    )
    screens = models.ManyToManyField(
        DisplayScreen,
        related_name="emergency_alerts",
        verbose_name="شاشات محددة",
        blank=True,
        help_text="اتركها فارغة لإرسال التنبيه إلى جميع شاشات المدارس المحددة.",
    )
    limited_to_screens = models.BooleanField(default=False)
    is_active = models.BooleanField("نشط", default=True)
    starts_at = models.DateTimeField("وقت البدء", default=timezone.now)
    expires_at = models.DateTimeField("وقت الانتهاء", null=True, blank=True)
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        related_name="emergency_alerts_created",
        verbose_name="أرسله",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        related_name="emergency_alerts_cancelled",
        verbose_name="ألغاه",
        null=True,
        blank=True,
    )
    cancelled_at = models.DateTimeField("وقت الإلغاء", null=True, blank=True)

    class Meta:
        verbose_name = "تنبيه طارئ"
        verbose_name_plural = "التنبيهات الطارئة"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("is_active", "starts_at"), name="emergency_active_idx"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.title}"

    @property
    def active_now(self):
        now = timezone.now()
        return bool(
            self.is_active
            and self.starts_at <= now
            and (self.expires_at is None or self.expires_at > now)
            and self.cancelled_at is None
        )

    def as_display_dict(self):
        return {
            "id": self.pk,
            "kind": self.kind,
            "kind_label": self.get_kind_display(),
            "title": self.title,
            "message": self.message,
            "starts_at": self.starts_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "screen_ids": list(self.screens.values_list("id", flat=True)),
        }


class EmergencyAlertReceipt(models.Model):
    alert = models.ForeignKey(EmergencyAlert, on_delete=models.CASCADE, related_name="receipts")
    screen = models.ForeignKey(DisplayScreen, on_delete=models.CASCADE)
    displayed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("alert", "screen"), name="one_emergency_receipt_per_screen"),
        ]


# ===========================
# Excellence (لوحة الشرف)
# ===========================

class ExcellenceQuerySet(models.QuerySet):
    """
    QuerySet مخصص لسجلات التميّز:
    - for_school(school)
    - active_for_today(school, date)
    """

    def for_school(self, school: Optional[School]) -> "ExcellenceQuerySet":
        if school is None:
            return self.none()
        return self.filter(school=school)

    def active_for_today(
        self,
        school: Optional[School],
        today: Optional[datetime.date] = None,
    ) -> "ExcellenceQuerySet":
        """
        يعيد سجلات التميّز النشطة لليوم الحالي:
        start_at <= اليوم  و (end_at is null أو end_at >= اليوم)
        """
        if school is None:
            return self.none()

        today = today or timezone.localdate()

        return (
            self.for_school(school)
            .filter(start_at__date__lte=today)
            .filter(
                models.Q(end_at__isnull=True) | models.Q(end_at__date__gte=today)
            )
        )


class Excellence(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="excellence_records",
        verbose_name="المدرسة",
        null=True,
        blank=True,
    )
    teacher_name = models.CharField("اسم المعلم/ـة", max_length=100)
    reason = models.CharField("سبب التميّز", max_length=200)

    # رفع صورة كملف
    photo = models.ImageField(
        "صورة (رفع ملف)",
        upload_to=_excellence_upload_to,
        blank=True,
        null=True,
        validators=[_validate_excellence_photo_extension],
        help_text="الصيغ المدعومة: JPG / JPEG / PNG / WEBP.",
    )

    # أو رابط صورة جاهز (اختياري)
    photo_url = models.URLField("رابط صورة (اختياري)", blank=True, null=True)

    start_at = models.DateTimeField("يظهر من", default=timezone.now)
    end_at = models.DateTimeField("ينتهي في", blank=True, null=True)
    priority = models.PositiveSmallIntegerField("أولوية العرض", default=10)

    objects: ExcellenceQuerySet = ExcellenceQuerySet.as_manager()  # type: ignore[assignment]

    class Meta:
        verbose_name = "تميّز"
        verbose_name_plural = "تميّز"
        ordering = ("priority", "-start_at")
        indexes = [
            # active_for_today filters on (school, start_at__date__lte)
            models.Index(fields=["school", "start_at"], name="exc_school_day_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.teacher_name} — {self.reason}"

    # --------- منطق التفعيل ---------

    @property
    def active_now(self) -> bool:
        now = timezone.now()
        if self.start_at and now < self.start_at:
            return False
        if self.end_at and now >= self.end_at:
            return False
        return True

    @property
    def image_src(self) -> Optional[str]:
        """
        يرجع رابط الصورة المعتمدة للعرض (ملف مرفوع أو رابط خارجي).
        """
        try:
            if self.photo and hasattr(self.photo, "url"):
                return self.photo.url
        except ValueError:
            # في حال الملف غير متوفر على التخزين
            pass
        return self.photo_url or None

    # --------- دوال مساعدة للـ API / الواجهة ---------

    @classmethod
    def active_for_today(cls, school: Optional[School]) -> "ExcellenceQuerySet":
        """
        تُستخدم في طبقة snapshot الحالية:
            Excellence.active_for_today(request.school)
        """
        return cls.objects.active_for_today(school)

    def as_dict(self) -> dict[str, Any]:
        """
        تمثيل JSON بسيط لواجهة شاشة العرض.
        نحاول توفير أكثر من اسم مفتاح (name/teacher_name, image/image_url)
        حتى لو تغيّر الـ JS مستقبلاً يكون التوافق أسهل.
        """
        img = self.image_src
        return {
            "id": self.id,
            "school_id": self.school_id,
            # أسماء عامة
            "name": self.teacher_name,
            "title": self.teacher_name,
            "subtitle": self.reason,
            # الأسماء الأصلية
            "teacher_name": self.teacher_name,
            "reason": self.reason,
            # الصورة
            "image": img,
            "image_url": img,
            # التواريخ
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "priority": self.priority,
            "active": self.active_now,
        }

    def save(self, *args, **kwargs) -> None:
        """
        تنظيف بسيط:
        - عند استبدال صورة مرفوعة بصورة جديدة، نحذف القديمة من التخزين بعد نجاح الحفظ.
        - لا نوقف الحفظ لو فشل حذف الملف من النظام.
        """
        old_path: Optional[str] = None

        if self.pk:
            try:
                old = Excellence.objects.get(pk=self.pk)
                if old.photo and self.photo and old.photo.name != self.photo.name:
                    # نخزّن المسار القديم لنحذفه بعد نجاح الحفظ
                    try:
                        old_path = old.photo.path
                    except (ValueError, Exception):
                        old_path = None
            except Excellence.DoesNotExist:
                old_path = None

        super().save(*args, **kwargs)

        if old_path and os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                # لا نرفع استثناء لو فشل الحذف
                pass
