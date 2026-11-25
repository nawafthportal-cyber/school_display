# notices/forms.py
from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from core.image_uploads import optimize_uploaded_image
from .models import Announcement, Excellence


class AnnouncementForm(forms.ModelForm):
    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        if school:
            self.instance.school = school
        from core.occasion_customizations import choices_for_school
        self.fields["occasion_theme"].choices = choices_for_school(self.instance.school_id, Announcement.OCCASION_THEME_CHOICES)

    class Meta:
        model = Announcement
        fields = [
            "title",
            "body",
            "level",
            "occasion_theme",
            "starts_at",
            "expires_at",
            "is_active",
        ]
        widgets = {
            "title": forms.TextInput(attrs={"maxlength": 120, "class": "w-full"}),
            "body": forms.Textarea(
                attrs={"rows": 3, "maxlength": 280, "class": "w-full"}
            ),
            # HTML5 datetime-local يتوقع قيمة بدون timezone; اترك التحويل لمنطق العرض/المسلسل
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "expires_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def clean(self):
        cleaned = super().clean()
        starts_at = cleaned.get("starts_at")
        expires_at = cleaned.get("expires_at")
        if starts_at and expires_at and expires_at <= starts_at:
            raise ValidationError(_("وقت الانتهاء يجب أن يكون بعد وقت البداية."))
        return cleaned


class ExcellenceForm(forms.ModelForm):
    """
    يدعم:
      - رفع صورة (ImageField: photo)
      - أو رابط صورة (photo_url)
    الأولوية للملف المرفوع عند العرض عبر خاصية model.image_src.
    """

    MAX_PHOTO_MB = 5  # حد حجم افتراضي (يمكن تعديله)
    MAX_SOURCE_PHOTO_MB = 20

    class Meta:
        model = Excellence
        fields = [
            "teacher_name",
            "reason",
            "photo",       # ← جديد: رفع ملف
            "photo_url",   # ← رابط صورة (اختياري)
            "start_at",
            "end_at",
            "priority",
        ]
        widgets = {
            "teacher_name": forms.TextInput(attrs={"maxlength": 100, "class": "w-full"}),
            "reason": forms.TextInput(attrs={"maxlength": 200, "class": "w-full"}),
            "start_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "end_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    _ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

    def clean_photo(self):
        file = self.cleaned_data.get("photo")
        if not file:
            return file
        source_max_bytes = self.MAX_SOURCE_PHOTO_MB * 1024 * 1024
        if getattr(file, "size", 0) > source_max_bytes:
            raise ValidationError(
                _("حجم الصورة الخام يتجاوز %(mb)s م.ب."),
                params={"mb": self.MAX_SOURCE_PHOTO_MB},
            )
        # تحقق من نوع المحتوى
        content_type = getattr(file, "content_type", "") or ""
        if content_type not in self._ALLOWED_CONTENT_TYPES:
            raise ValidationError(
                _("نوع الملف غير مسموح به. الأنواع المسموحة: JPEG, PNG, WebP."),
            )
        file = optimize_uploaded_image(
            file,
            max_width=1600,
            max_height=1600,
            quality=82,
        )
        max_bytes = self.MAX_PHOTO_MB * 1024 * 1024
        if getattr(file, "size", 0) > max_bytes:
            raise ValidationError(
                _("حجم الصورة بعد المعالجة يتجاوز %(mb)s م.ب."),
                params={"mb": self.MAX_PHOTO_MB},
            )
        return file

    def clean(self):
        cleaned = super().clean()
        start_at = cleaned.get("start_at")
        end_at = cleaned.get("end_at")
        if start_at and end_at and end_at <= start_at:
            raise ValidationError(_("وقت الانتهاء يجب أن يكون بعد وقت البداية."))
        return cleaned
