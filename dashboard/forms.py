# dashboard/forms.py
from __future__ import annotations

from datetime import datetime, timedelta
import os
import logging
import re

from django import forms
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from schedule.models import (
    DisplayTheme,
    SchoolSettings,
    DutyAssignment,
    DaySchedule,
    Period,
    Break,
    ClassLesson,
    WEEKDAYS,
    SchoolClass,
    Teacher,
    Subject,
)
from notices.models import Announcement, EmergencyAlert, Excellence
from standby.models import StandbyAssignment
from core.image_uploads import optimize_uploaded_image
from core.models import (
    DisplayScreen,
    School,
    SchoolType,
    SubscriptionPlan,
    SystemEmployeeProfile,
    UserProfile,
)
from core.system_access import (
    PERMISSION_DEFINITIONS,
    ROLE_CHOICES,
    ROLE_SUPPORT as ROLE_SUPPORT_KEY,
    normalize_permission_keys,
    role_permissions,
)
from subscriptions.models import SchoolSubscription, SubscriptionScreenAddon, SubscriptionRequest
logger = logging.getLogger(__name__)

UserModel = get_user_model()


def _validate_updated_password(password, user, cleaned):
    from copy import copy
    candidate = copy(user)
    for field in ("username", "email", "first_name", "last_name"):
        if field in cleaned:
            setattr(candidate, field, cleaned[field] or "")
    validate_password(password, candidate)


# ========================
# دوال مساعدة داخلية
# ========================

def _parse_hhmm(value: str | None):
    if not value:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def _is_checked(raw) -> bool:
    return str(raw).lower() in {"1", "true", "on", "yes"}


def _is_blank_period_fields(idx, st, en) -> bool:
    return (idx in (None, "")) and (st is None) and (en is None)


def _is_blank_break_fields(label, st, dur) -> bool:
    return (st is None) and (dur in (None, ""))


def _get_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


# ========================
# إعدادات المدرسة
# ========================

class SchoolSettingsForm(forms.ModelForm):
    DISPLAY_FIELDS = {
        "logo",
        "featured_panel",
        "theme",
        "custom_theme",
        "display_accent_color",
        "standby_scroll_speed",
        "periods_scroll_speed",
        "display_before_title",
        "display_before_badge",
        "display_after_title",
        "display_after_badge",
        "display_after_holiday_title",
        "display_after_holiday_badge",
        "display_holiday_title",
        "display_holiday_badge",
    }
    ACCOUNT_FIELDS = {
        "email",
        "mobile",
        "test_mode_weekday_override",
        "screen_offline_alerts_enabled",
        "screen_offline_threshold_minutes",
        "screen_offline_school_hours_only",
        "screen_offline_grace_minutes",
        "screen_offline_cooldown_minutes",
        "screen_offline_max_alerts_per_day",
        "screen_recovery_notice_enabled",
    }
    email = forms.EmailField(
        label="البريد الإلكتروني",
        required=False,
        max_length=254,
        help_text="يُستخدم لإشعارات الاشتراك وإتمام عمليات الدفع.",
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "dir": "ltr",
                "placeholder": "name@example.com",
            }
        ),
    )
    mobile = forms.CharField(
        label="رقم الجوال",
        required=False,
        max_length=20,
        help_text="أدخل رقم جوال سعودي مثل 05xxxxxxxx. يُستخدم للدخول وعمليات الدفع.",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "tel",
                "dir": "ltr",
                "inputmode": "tel",
                "placeholder": "05xxxxxxxx",
            }
        ),
    )
    logo = forms.ImageField(
        label="شعار المدرسة",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={
                "accept": "image/png,image/jpeg,image/webp",
                "aria-describedby": "logoHelp",
            }
        ),
    )
    THEME_ACCENTS = {
        "indigo": "#6366F1",
        "emerald": "#22C55E",
        "rose": "#EC4899",
        "cyan": "#06B6D4",
        "amber": "#EAB308",
        "orange": "#F97316",
        "violet": "#A855F7",
        "default": "#6366F1",
        "boys": "#22C55E",
        "girls": "#EC4899",
    }

    class Meta:
        model = SchoolSettings
        fields = [
            "featured_panel",
            "theme",
            "custom_theme",
            "standby_scroll_speed",
            "periods_scroll_speed",
            "bell_sound",
            "display_before_title",
            "display_before_badge",
            "display_after_title",
            "display_after_badge",
            "display_after_holiday_title",
            "display_after_holiday_badge",
            "display_holiday_title",
            "display_holiday_badge",
            "display_accent_color",
            "test_mode_weekday_override",
            "screen_offline_alerts_enabled",
            "screen_offline_threshold_minutes",
            "screen_offline_school_hours_only",
            "screen_offline_grace_minutes",
            "screen_offline_cooldown_minutes",
            "screen_offline_max_alerts_per_day",
            "screen_recovery_notice_enabled",
        ]
        widgets = {
            "featured_panel": forms.Select(),
            "theme": forms.Select(),

            # ✅ حد أدنى 0.5 + خطوة 0.1 (قيم عملية للعرض)
            "standby_scroll_speed": forms.NumberInput(attrs={"min": 0.5, "max": 5.0, "step": 0.1}),
            "periods_scroll_speed": forms.NumberInput(attrs={"min": 0.5, "max": 5.0, "step": 0.1}),
            "bell_sound": forms.Select(),

            # The settings screen uses the visual theme palette as the source
            # of truth. Avoid type=color here because browsers submit #000000
            # for empty hidden color inputs, which can override the selected
            # theme on the display screen.
            "display_accent_color": forms.HiddenInput(),
            "display_before_title": forms.TextInput(attrs={"dir": "rtl", "maxlength": "150"}),
            "display_before_badge": forms.TextInput(attrs={"dir": "rtl", "maxlength": "40"}),
            "display_after_title": forms.TextInput(attrs={"dir": "rtl", "maxlength": "150"}),
            "display_after_badge": forms.TextInput(attrs={"dir": "rtl", "maxlength": "40"}),
            "display_after_holiday_title": forms.TextInput(attrs={"dir": "rtl", "maxlength": "150"}),
            "display_after_holiday_badge": forms.TextInput(attrs={"dir": "rtl", "maxlength": "40"}),
            "display_holiday_title": forms.TextInput(attrs={"dir": "rtl", "maxlength": "150"}),
            "display_holiday_badge": forms.TextInput(attrs={"dir": "rtl", "maxlength": "40"}),
            
            "test_mode_weekday_override": forms.Select(),
            "screen_offline_threshold_minutes": forms.NumberInput(attrs={"min": 5, "max": 60}),
            "screen_offline_grace_minutes": forms.NumberInput(attrs={"min": 0, "max": 120}),
            "screen_offline_cooldown_minutes": forms.NumberInput(attrs={"min": 10, "max": 720}),
            "screen_offline_max_alerts_per_day": forms.NumberInput(attrs={"min": 1, "max": 20}),
        }

    def __init__(self, *args, **kwargs):
        # استخراج المستخدم إذا تم تمريره (من الـ view)
        self.request_user = kwargs.pop("user", None)
        self.form_mode = kwargs.pop("mode", "all")
        super().__init__(*args, **kwargs)

        self.fields["custom_theme"].queryset = DisplayTheme.objects.filter(school_id=self.instance.school_id)
        self.fields["custom_theme"].empty_label = "استخدام الألوان الجاهزة"

        if self.request_user and getattr(self.request_user, "is_authenticated", False):
            self.fields["email"].initial = (getattr(self.request_user, "email", "") or "").strip()
            profile = UserProfile.objects.filter(user=self.request_user).only("mobile").first()
            self.fields["mobile"].initial = (getattr(profile, "mobile", "") or "").strip()

        # تأكيد attrs حتى لو تغيّرت الـ widgets أو تم override من مكان آخر
        for fname in ["standby_scroll_speed", "periods_scroll_speed"]:
            if fname in self.fields:
                self.fields[fname].widget.attrs.update({"min": "0.5", "max": "5.0", "step": "0.1"})
                # تنبيه قيم مفيدة للمستخدم
                if fname == "standby_scroll_speed":
                    self.fields[fname].help_text = "الحد الأدنى 0.5. قيم مقترحة: 0.5 – 1.2. كلما زادت القيمة زادت السرعة."
                else:
                    self.fields[fname].help_text = "الحد الأدنى 0.5. قيم مقترحة: 0.5 – 1.0. كلما زادت القيمة زادت السرعة."

        if "display_accent_color" in self.fields:
            self.fields["display_accent_color"].help_text = (
                "اختياري: اختر لونًا رئيسياً لشاشة العرض. اتركه فارغًا لاستخدام ألوان الثيم."
            )
        
        # ✅ وضع الاختبار: للسوبر أدمن فقط
        if "test_mode_weekday_override" in self.fields:
            # إذا لم يكن سوبر أدمن، أخفِ الحقل
            if self.request_user and not self.request_user.is_superuser:
                del self.fields["test_mode_weekday_override"]
            else:
                self.fields["test_mode_weekday_override"].help_text = (
                    "<strong style='color: #d97706;'>⚠️ للسوبر أدمن فقط:</strong> "
                    "لتشغيل الشاشة في يوم إجازة للاختبار، حدد اليوم المراد محاكاته "
                    "(مثلاً: لو اليوم خميس وتريد اختبار جدول الأحد، اختر 'الأحد'). "
                    "<br><strong>لا تنسَ إلغاء التفعيل بعد الاختبار!</strong>"
                )
                self.fields["test_mode_weekday_override"].label = "🧪 وضع الاختبار: محاكاة يوم"
                self.fields["test_mode_weekday_override"].required = False

        if self.form_mode == "account":
            for field_name in self.DISPLAY_FIELDS:
                self.fields.pop(field_name, None)
        elif self.form_mode == "display":
            for field_name in self.ACCOUNT_FIELDS:
                self.fields.pop(field_name, None)

    # =========================
    # ✅ Server-side validation
    # =========================
    def clean_standby_scroll_speed(self):
        v = self.cleaned_data.get("standby_scroll_speed")
        if v is None:
            return v
        try:
            v_f = float(v)
        except (TypeError, ValueError):
            raise forms.ValidationError("الرجاء إدخال رقم صحيح لسرعة تمرير الانتظار.")
        if v_f < 0.5:
            raise forms.ValidationError("الحد الأدنى لسرعة تمرير الانتظار هو 0.5.")
        return v_f

    def clean_periods_scroll_speed(self):
        v = self.cleaned_data.get("periods_scroll_speed")
        if v is None:
            return v
        try:
            v_f = float(v)
        except (TypeError, ValueError):
            raise forms.ValidationError("الرجاء إدخال رقم صحيح لسرعة تمرير جدول الحصص.")
        if v_f < 0.5:
            raise forms.ValidationError("الحد الأدنى لسرعة تمرير جدول الحصص هو 0.5.")
        return v_f

    def clean_email(self):
        return (self.cleaned_data.get("email") or "").strip().casefold()

    def clean_mobile(self):
        value = (self.cleaned_data.get("mobile") or "").strip()
        if not value:
            return ""

        arabic_digits = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
        digits = re.sub(r"\D+", "", value.translate(arabic_digits))
        if digits.startswith("00966"):
            digits = "0" + digits[5:]
        elif digits.startswith("966"):
            digits = "0" + digits[3:]
        elif re.fullmatch(r"5\d{8}", digits):
            digits = "0" + digits

        if not re.fullmatch(r"05\d{8}", digits):
            raise forms.ValidationError("أدخل رقم جوال سعودي صحيحًا بصيغة 05xxxxxxxx.")
        return digits

    def clean(self):
        cleaned = super().clean()
        if "theme" in self.fields:
            theme = (cleaned.get("theme") or "indigo").strip().lower()
            # The settings screen exposes preset theme buttons, not a custom
            # color picker. Keep the stored accent aligned with that palette.
            cleaned["display_accent_color"] = self.THEME_ACCENTS.get(theme, "#6366F1")
            custom = cleaned.get("custom_theme")
            if custom:
                cleaned["theme"] = custom.base_theme
                cleaned["display_accent_color"] = custom.accent_color
        return cleaned

    def clean_logo(self):
        file_obj = self.cleaned_data.get("logo")
        if not file_obj:
            return file_obj
        return optimize_uploaded_image(
            file_obj,
            max_width=1200,
            max_height=1200,
            quality=84,
        )

    @transaction.atomic
    def save(self, commit=True):
        """
        نحفظ الإعدادات، ولو تم رفع شعار جديد نحدّث school.logo بأمان.
        """
        instance: SchoolSettings = super().save(commit=False)
        logo_file = self.cleaned_data.get("logo") if "logo" in self.fields else None

        # اسم المدرسة يُدار من موديل School نفسه ولا يُسمح بتعديله من لوحة المدرسة.
        if getattr(instance, "school_id", None) and getattr(instance, "school", None):
            instance.name = instance.school.name

        if logo_file and getattr(instance, "school_id", None):
            try:
                # تحديث شعار المدرسة المرتبطة
                instance.school.logo = logo_file
                instance.school.save(update_fields=["logo"])
            except Exception as exc:
                # لا نكسر الحفظ لو فشل تحديث الشعار لأي سبب
                logger.exception("Failed to update school logo for school_id=%s: %s", instance.school_id, exc)

        if commit:
            instance.save()
            # إذا كان عندك many-to-many في الفورم مستقبلًا
            self.save_m2m()

            if self.request_user and getattr(self.request_user, "is_authenticated", False):
                if "email" not in self.fields or "mobile" not in self.fields:
                    return instance
                email = self.cleaned_data.get("email", "")
                if self.request_user.email != email:
                    self.request_user.email = email
                    self.request_user.save(update_fields=["email"])

                profile = _get_profile(self.request_user)
                mobile = self.cleaned_data.get("mobile", "")
                if (profile.mobile or "") != mobile:
                    profile.mobile = mobile
                    profile.save(update_fields=["mobile"])

        return instance


class ScreenDisplayCustomizationForm(forms.ModelForm):
    """واجهة موحدة لتخصيص شاشة واحدة مع قيم إعداد العرض العام كمرجع.

    كل مجموعة إعدادات تملك مفتاح "اتباع إعداد جميع الشاشات". عند تفعيله تُفرَّغ
    حقول ``*_override`` الخاصة بالمجموعة، فتبقى الشاشة وارثة للإعداد العام وتصلها
    أي تعديلات لاحقة عليه. هذا هو السلوك الذي يفترضه ``website.views`` عند تحويل
    الشاشة إلى سياق عرض: القيمة الفارغة تعني "خذها من إعدادات المدرسة".
    """

    THEME_ACCENTS = SchoolSettingsForm.THEME_ACCENTS
    MESSAGE_FIELD_MAP = {
        "display_before_title": "display_before_title_override",
        "display_before_badge": "display_before_badge_override",
        "display_after_title": "display_after_title_override",
        "display_after_badge": "display_after_badge_override",
        "display_after_holiday_title": "display_after_holiday_title_override",
        "display_after_holiday_badge": "display_after_holiday_badge_override",
        "display_holiday_title": "display_holiday_title_override",
        "display_holiday_badge": "display_holiday_badge_override",
    }

    INHERIT_APPEARANCE = "appearance"
    INHERIT_MESSAGES = "messages"
    INHERIT_MOTION = "motion"
    INHERIT_GROUP_CHOICES = [
        (INHERIT_APPEARANCE, "الهوية البصرية (اللون والكرت المميز)"),
        (INHERIT_MESSAGES, "رسائل خارج وقت الحصص"),
        (INHERIT_MOTION, "سرعات التمرير"),
    ]
    # كل مجموعة ← حقول الـ override التي تُفرَّغ عند اختيار الوراثة.
    INHERIT_GROUP_FIELDS = {
        INHERIT_APPEARANCE: (
            "theme_override",
            "custom_theme_override_id",
            "display_accent_color_override",
            "featured_panel_override",
        ),
        INHERIT_MESSAGES: tuple(MESSAGE_FIELD_MAP.values()),
        INHERIT_MOTION: (
            "standby_scroll_speed_override",
            "periods_scroll_speed_override",
        ),
    }
    # الحقول المعروضة التي تصبح إلزامية فقط عندما لا ترث مجموعتها.
    INHERIT_GROUP_INPUTS = {
        INHERIT_APPEARANCE: ("theme", "featured_panel"),
        INHERIT_MESSAGES: tuple(MESSAGE_FIELD_MAP.keys()),
        INHERIT_MOTION: ("standby_scroll_speed", "periods_scroll_speed"),
    }

    inherit_groups = forms.MultipleChoiceField(
        label="اتباع إعداد جميع الشاشات",
        choices=INHERIT_GROUP_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple(),
    )
    use_school_logo = forms.BooleanField(
        label="استخدام شعار المدرسة",
        required=False,
        help_text="عند تفعيله يُحذف الشعار الخاص بهذه الشاشة وتعود إلى شعار المدرسة.",
    )
    logo = forms.ImageField(
        label="شعار هذه الشاشة",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/png,image/jpeg,image/webp"}),
    )
    featured_panel = forms.ChoiceField(
        label="الكرت المميز",
        choices=SchoolSettings.FEATURE_PANEL_CHOICES,
        required=False,
    )
    theme = forms.ChoiceField(
        label="لون الثيم",
        choices=SchoolSettings.THEME_CHOICES,
        required=False,
    )
    display_accent_color = forms.CharField(required=False, widget=forms.HiddenInput())
    custom_theme = forms.ModelChoiceField(label="الثيم الخاص", queryset=DisplayTheme.objects.none(), required=False, empty_label="استخدام الألوان الجاهزة")
    standby_scroll_speed = forms.FloatField(
        label="سرعة تمرير الانتظار",
        required=False,
        min_value=0.5,
        max_value=5.0,
        widget=forms.NumberInput(attrs={"min": "0.5", "max": "5", "step": "0.1"}),
    )
    periods_scroll_speed = forms.FloatField(
        label="سرعة تمرير جدول الحصص",
        required=False,
        min_value=0.5,
        max_value=5.0,
        widget=forms.NumberInput(attrs={"min": "0.5", "max": "5", "step": "0.1"}),
    )
    display_before_title = forms.CharField(max_length=150, required=False, label="عنوان ما قبل الدوام")
    display_before_badge = forms.CharField(max_length=40, required=False, label="شارة ما قبل الدوام")
    display_after_title = forms.CharField(max_length=150, required=False, label="عنوان ما بعد الدوام")
    display_after_badge = forms.CharField(max_length=40, required=False, label="شارة ما بعد الدوام")
    display_after_holiday_title = forms.CharField(max_length=150, required=False, label="عنوان ما قبل الإجازة")
    display_after_holiday_badge = forms.CharField(max_length=40, required=False, label="شارة ما قبل الإجازة")
    display_holiday_title = forms.CharField(max_length=150, required=False, label="عنوان يوم الإجازة")
    display_holiday_badge = forms.CharField(max_length=40, required=False, label="شارة يوم الإجازة")

    class Meta:
        model = DisplayScreen
        fields = [
            "name",
            "is_active",
            "inherit_groups",
            "use_school_logo",
            "logo",
            "featured_panel",
            "theme",
            "custom_theme",
            "display_accent_color",
            "standby_scroll_speed",
            "periods_scroll_speed",
            "display_before_title",
            "display_before_badge",
            "display_after_title",
            "display_after_badge",
            "display_after_holiday_title",
            "display_after_holiday_badge",
            "display_holiday_title",
            "display_holiday_badge",
            "occasion_theme",
            "show_announcements",
            "show_period_classes",
            "show_standby",
            "show_duty",
            "show_excellence",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"maxlength": "100"}),
            "occasion_theme": forms.Select(),
        }

    @classmethod
    def inherited_groups_for(cls, screen) -> list[str]:
        """المجموعات التي ما زالت هذه الشاشة ترث إعدادها العام بالكامل."""
        inherited = []
        for group, override_names in cls.INHERIT_GROUP_FIELDS.items():
            if all(
                getattr(screen, name, None) in (None, "")
                for name in override_names
            ):
                inherited.append(group)
        return inherited

    def __init__(self, *args, school_settings: SchoolSettings, **kwargs):
        self.school_settings = school_settings
        super().__init__(*args, **kwargs)
        screen = self.instance
        self.fields["custom_theme"].queryset = DisplayTheme.objects.filter(school_id=school_settings.school_id)
        from core.occasion_customizations import customizations_for_school
        occasion_customizations = customizations_for_school(school_settings.school_id)
        self.fields["occasion_theme"].choices = [
            (key, occasion_customizations.get(key, {}).get("name", label))
            for key, label in self.fields["occasion_theme"].choices
        ]
        self.initial["custom_theme"] = screen.custom_theme_override_id or (
            school_settings.custom_theme_id if not screen.theme_override else None
        )

        # القيم المعروضة دائمًا هي القيمة السارية فعليًا على الشاشة: تخصيصها
        # الخاص إن وُجد، وإلا الإعداد العام. هكذا يرى المدير النتيجة النهائية
        # سواء كانت موروثة أو مخصصة.
        self.initial.update(
            {
                "theme": (getattr(screen, "theme_override", "") or school_settings.theme or "indigo"),
                "featured_panel": (
                    getattr(screen, "featured_panel_override", "")
                    or school_settings.featured_panel
                    or SchoolSettings.FEATURE_PANEL_EXCELLENCE
                ),
                "display_accent_color": (
                    getattr(screen, "display_accent_color_override", "")
                    or school_settings.display_accent_color
                    or self.THEME_ACCENTS.get(school_settings.theme, "#6366F1")
                ),
                "standby_scroll_speed": (
                    getattr(screen, "standby_scroll_speed_override", None)
                    or school_settings.standby_scroll_speed
                ),
                "periods_scroll_speed": (
                    getattr(screen, "periods_scroll_speed_override", None)
                    or school_settings.periods_scroll_speed
                ),
            }
        )
        for public_name, model_name in self.MESSAGE_FIELD_MAP.items():
            self.initial[public_name] = (
                getattr(screen, model_name, "")
                or getattr(school_settings, public_name, "")
            )

        # شاشة جديدة (بلا أي override) ترث كل شيء افتراضيًا.
        self.initial["inherit_groups"] = self.inherited_groups_for(screen)
        self.initial["use_school_logo"] = not bool(getattr(screen, "logo_override", None))

    def clean(self):
        cleaned = super().clean()
        inherited = set(cleaned.get("inherit_groups") or ())

        # الحقول إلزامية فقط عندما تكون مجموعتها مخصصة. عند الوراثة نتجاهل ما
        # أُرسل حتى لا يمنع إدخال ناقص حفظَ اختيارٍ لا يستعمله أصلًا.
        for group, input_names in self.INHERIT_GROUP_INPUTS.items():
            if group in inherited:
                for name in input_names:
                    self.errors.pop(name, None)
                continue
            for name in input_names:
                if name in self.errors:
                    continue
                value = cleaned.get(name)
                if value is None or (isinstance(value, str) and not value.strip()):
                    self.add_error(name, "هذا الحقل مطلوب عند تخصيص هذه الشاشة.")

        theme = (cleaned.get("theme") or "indigo").strip().lower()
        cleaned["display_accent_color"] = self.THEME_ACCENTS.get(theme, "#6366F1")
        custom = cleaned.get("custom_theme")
        if custom and self.INHERIT_APPEARANCE not in inherited:
            cleaned["theme"] = custom.base_theme
            cleaned["display_accent_color"] = custom.accent_color
        return cleaned

    def clean_logo(self):
        file_obj = self.cleaned_data.get("logo")
        if not file_obj:
            return file_obj
        return optimize_uploaded_image(file_obj, max_width=1200, max_height=1200, quality=84)

    @transaction.atomic
    def save(self, commit=True):
        screen: DisplayScreen = super().save(commit=False)
        inherited = set(self.cleaned_data.get("inherit_groups") or ())

        if self.INHERIT_APPEARANCE in inherited:
            screen.theme_override = ""
            screen.custom_theme_override = None
            screen.display_accent_color_override = ""
            screen.featured_panel_override = ""
        else:
            screen.theme_override = self.cleaned_data["theme"]
            screen.custom_theme_override = self.cleaned_data.get("custom_theme")
            screen.display_accent_color_override = self.cleaned_data["display_accent_color"]
            screen.featured_panel_override = self.cleaned_data["featured_panel"]

        if self.INHERIT_MOTION in inherited:
            screen.standby_scroll_speed_override = None
            screen.periods_scroll_speed_override = None
        else:
            screen.standby_scroll_speed_override = self.cleaned_data["standby_scroll_speed"]
            screen.periods_scroll_speed_override = self.cleaned_data["periods_scroll_speed"]

        messages_inherited = self.INHERIT_MESSAGES in inherited
        for public_name, model_name in self.MESSAGE_FIELD_MAP.items():
            setattr(
                screen,
                model_name,
                "" if messages_inherited else self.cleaned_data[public_name],
            )

        logo_file = self.cleaned_data.get("logo")
        if self.cleaned_data.get("use_school_logo") and not logo_file:
            if getattr(screen, "logo_override", None):
                screen.logo_override.delete(save=False)
            screen.logo_override = None
        elif logo_file:
            screen.logo_override = logo_file

        if commit:
            screen.save()
            self.save_m2m()
        return screen


# ========================
# اليوم والجدول الزمني
# ========================

class DayScheduleForm(forms.ModelForm):
    class Meta:
        model = DaySchedule
        fields = ["periods_count"]
        widgets = {
            "periods_count": forms.NumberInput(attrs={
                "class": "dp-input dp-input--center",
                "min": "0"
            })
        }

    def clean_periods_count(self):
        v = self.cleaned_data.get("periods_count")
        if v is None or v < 0:
            raise ValidationError("عدد الحصص يجب أن يكون رقمًا غير سالب.")
        return v


class PeriodForm(forms.ModelForm):
    class Meta:
        model = Period
        fields = ["index", "starts_at", "ends_at"]
        widgets = {
            "index": forms.NumberInput(attrs={
                "class": "dp-input dp-input--center",
                "placeholder": "#"
            }),
            "starts_at": forms.TimeInput(attrs={
                "type": "time", 
                "step": 60,
                "class": "dp-input dp-input--ltr"
            }),
            "ends_at": forms.TimeInput(attrs={
                "type": "time", 
                "step": 60,
                "class": "dp-input dp-input--ltr"
            }),
        }

    def clean(self):
        cleaned = super().clean()

        # حذف الصف
        if _is_checked(self.data.get(f"{self.prefix}-DELETE")):
            self._is_marked_delete = True
            self.instance._skip_cross_validation = True
            return cleaned

        st = cleaned.get("starts_at")
        en = cleaned.get("ends_at")
        idx = cleaned.get("index")

        # صف فارغ
        if _is_blank_period_fields(idx, st, en):
            self._is_blank_row = True
            self.instance._skip_cross_validation = True
            return cleaned

        if st is None:
            self.add_error("starts_at", "هذا الحقل مطلوب.")
        if en is None:
            self.add_error("ends_at", "هذا الحقل مطلوب.")
        if idx in (None, ""):
            self.add_error("index", "هذا الحقل مطلوب.")
        elif isinstance(idx, int) and idx < 1:
            self.add_error("index", "رقم الحصة يجب أن يبدأ من 1.")

        if st is not None and en is not None and en <= st:
            self.add_error("ends_at", "وقت نهاية الحصة يجب أن يكون بعد وقت بدايتها.")

        if self.errors:
            self.instance._skip_cross_validation = True

        # When the row is valid, the inline formset performs cross-validation
        # against the submitted periods + breaks. Skipping model-level DB overlap
        # checks prevents false failures against still-old rows during save.
        if not getattr(self, "_is_blank_row", False) and not getattr(self, "_is_marked_delete", False):
            if not self.errors:
                self.instance._skip_cross_validation = True

        return cleaned


class BreakForm(forms.ModelForm):
    class Meta:
        model = Break
        fields = ["label", "starts_at", "duration_min"]
        widgets = {
            "label": forms.TextInput(attrs={
                "class": "dp-input",
                "placeholder": "مثار: فسحة الصلاة"
            }),
            "starts_at": forms.TimeInput(attrs={
                "type": "time", 
                "step": 60,
                "class": "dp-input dp-input--ltr"
            }),
            "duration_min": forms.NumberInput(attrs={
                "class": "dp-input dp-input--center",
                "min": "1"
            }),
        }

    def clean(self):
        cleaned = super().clean()

        if _is_checked(self.data.get(f"{self.prefix}-DELETE")):
            self._is_marked_delete = True
            self.instance._skip_cross_validation = True
            return cleaned

        label = cleaned.get("label")
        st = cleaned.get("starts_at")
        dur = cleaned.get("duration_min")

        if _is_blank_break_fields(label, st, dur):
            self._is_blank_row = True
            self.instance._skip_cross_validation = True
            return cleaned

        if st is None:
            self.add_error("starts_at", "هذا الحقل مطلوب.")
        if dur is None or dur <= 0:
            self.add_error("duration_min", "مدة الفسحة يجب أن تكون رقمًا موجبًا بالدقائق.")

        if self.errors:
            self.instance._skip_cross_validation = True

        # Same rationale as PeriodForm.clean(): the dashboard validates overlaps
        # using the POSTed rows, so skip DB overlap checks during batch save.
        if not getattr(self, "_is_blank_row", False) and not getattr(self, "_is_marked_delete", False):
            if not self.errors:
                self.instance._skip_cross_validation = True

        return cleaned


class PeriodInlineFormSet(BaseInlineFormSet):
    """
    يتحقق من:
    - عدد الحصص لا يتجاوز العدد المحدد في اليوم.
    - عدم تكرار أرقام الحصص.
    - عدم وجود تداخل زمني بين الحصص والفسح.
    """

    def clean(self):
        super().clean()

        parent: DaySchedule = self.instance
        target_count = int(getattr(parent, "periods_count", 0) or 0)

        errors_added = 0
        periods = []
        seen_indexes: dict[int, forms.ModelForm] = {}

        # جمع الحصص
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            cd = form.cleaned_data

            if cd.get("DELETE") or getattr(form, "_is_marked_delete", False):
                form.instance._skip_cross_validation = True
                continue

            st, en, idx = cd.get("starts_at"), cd.get("ends_at"), cd.get("index")

            if getattr(form, "_is_blank_row", False) or _is_blank_period_fields(idx, st, en):
                form.instance._skip_cross_validation = True
                continue

            if form.errors:
                form.instance._skip_cross_validation = True
                errors_added += sum(len(v) for v in form.errors.values())
                continue

            if idx in seen_indexes:
                form.add_error("index", "رقم الحصة مكرر لهذا اليوم.")
                seen_indexes[idx].add_error("index", "رقم الحصة مكرر لهذا اليوم.")
                form.instance._skip_cross_validation = True
                seen_indexes[idx].instance._skip_cross_validation = True
                errors_added += 2
                continue

            seen_indexes[idx] = form
            periods.append({"label": f"الحصة {idx}", "start": st, "end": en, "form": form})

        # جمع الفسح من بيانات POST (فورم آخر)
        breaks = []
        try:
            total_b = int(self.data.get("b-TOTAL_FORMS", 0) or 0)
        except (TypeError, ValueError):
            raise ValidationError("تعذر قراءة صفوف الفسح. أعد تحميل الصفحة ثم حاول الحفظ مجددًا.")
        if total_b < 0 or total_b > 1000:
            raise ValidationError("عدد صفوف الفسح غير صالح.")
        for i in range(total_b):
            if _is_checked(self.data.get(f"b-{i}-DELETE")):
                continue
            label = (self.data.get(f"b-{i}-label") or "").strip() or "فسحة"
            st = _parse_hhmm(self.data.get(f"b-{i}-starts_at"))
            dur_raw = self.data.get(f"b-{i}-duration_min")
            try:
                dur = int(dur_raw) if dur_raw not in (None, "") else None
            except ValueError:
                dur = None

            if _is_blank_break_fields(label, st, dur):
                continue

            if st and dur and dur > 0:
                end = (datetime.combine(datetime.today(), st) + timedelta(minutes=dur)).time()
                breaks.append({"label": f"الفسحة ({label})", "start": st, "end": end})

        # التحقق من العدد الأقصى
        count_periods = len(periods)
        if target_count > 0 and count_periods > target_count:
            raise ValidationError(
                f"عدد الحصص المدخلة ({count_periods}) أكبر من القيمة المحددة لليوم ({target_count}). "
                f"رجاءً احذف/عدّل الحصص الزائدة."
            )

        # ترتيب كل العناصر زمنيًا وفحص التداخل
        items = [{"kind": "p", **p} for p in periods] + [{"kind": "b", **b} for b in breaks]
        items.sort(key=lambda x: x["start"])

        for i in range(1, len(items)):
            prev, cur = items[i - 1], items[i]
            if cur["start"] < prev["end"]:
                if cur["kind"] == "b" and prev["kind"] == "b":
                    raise ValidationError(f"يوجد تداخل زمني بين {prev['label']} و{cur['label']}.")
                msg_cur = f"تداخل مع {prev['label']} ({prev['start']}-{prev['end']})."
                msg_prev = f"يتداخل مع {cur['label']} ({cur['start']}-{cur['end']})."
                if cur["kind"] == "p":
                    cur["form"].add_error("starts_at", msg_cur)
                    cur["form"].instance._skip_cross_validation = True
                    errors_added += 1
                if prev["kind"] == "p":
                    prev["form"].add_error("ends_at", msg_prev)
                    prev["form"].instance._skip_cross_validation = True
                    errors_added += 1

        if errors_added > 0:
            raise ValidationError("تحقّق من الأوقات: يوجد حقول ناقصة/مكررة أو تداخلات زمنية.")


PeriodFormSet = inlineformset_factory(
    parent_model=DaySchedule,
    model=Period,
    form=PeriodForm,
    formset=PeriodInlineFormSet,
    extra=0,
    can_delete=True,
    min_num=0,
    validate_min=True,
)

BreakFormSet = inlineformset_factory(
    parent_model=DaySchedule,
    model=Break,
    form=BreakForm,
    extra=0,
    can_delete=True,
    min_num=0,
    validate_min=True,
)


# ========================
# الإعلانات والتميز
# ========================

class AnnouncementForm(forms.ModelForm):
    scope = forms.ChoiceField(
        label="نطاق العرض",
        choices=[
            ("all", "جميع شاشات المدرسة"),
            ("screens", "شاشة محددة أو عدة شاشات"),
        ],
        initial="all",
        required=False,
    )

    class Meta:
        model = Announcement
        fields = [
            "title",
            "body",
            "level",
            "occasion_theme",
            "scope",
            "screens",
            "starts_at",
            "expires_at",
            "is_active",
        ]
        widgets = {
            "screens": forms.CheckboxSelectMultiple(),
            "starts_at": forms.DateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"type": "datetime-local"},
            ),
            "expires_at": forms.DateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"type": "datetime-local"},
            ),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        if school is not None:
            from core.occasion_customizations import customizations_for_school
            customizations = customizations_for_school(school.pk)
            self.fields["occasion_theme"].choices = [
                (key, customizations.get(key, {}).get("name", label))
                for key, label in self.fields["occasion_theme"].choices
            ]
        if school is not None:
            # الشاشات المعطلة (يدويًا أو تلقائيًا عند تجاوز حد الباقة) تبقى
            # معروضة إن كان التنبيه يستهدفها بالفعل. بدون ذلك يختفي استهدافها
            # من النموذج ويُحذف بصمت عند أول حفظ، فلا يعود عند إعادة التفعيل.
            targeted_ids = (
                set(self.instance.screens.values_list("id", flat=True))
                if self.instance.pk
                else set()
            )
            self.fields["screens"].queryset = (
                DisplayScreen.objects.filter(school=school)
                .filter(Q(is_active=True) | Q(pk__in=targeted_ids))
                .order_by("name")
            )
        else:
            self.fields["screens"].queryset = DisplayScreen.objects.none()
        if self.instance.pk and self.instance.screens.exists():
            self.fields["scope"].initial = "screens"
        for name in ("starts_at", "expires_at"):
            if name in self.fields:
                self.fields[name].input_formats = ["%Y-%m-%dT%H:%M"]

    def clean(self):
        cleaned = super().clean()
        starts_at = cleaned.get("starts_at")
        expires_at = cleaned.get("expires_at")
        scope = cleaned.get("scope") or "all"
        screens = cleaned.get("screens")
        if starts_at and expires_at and expires_at <= starts_at:
            raise ValidationError("وقت انتهاء التنبيه يجب أن يكون بعد وقت البداية.")
        if screens:
            # A concrete screen selection always wins, even if an older browser
            # submits the stale default value `scope=all`.
            cleaned["scope"] = "screens"
        elif scope == "screens":
            self.add_error("screens", "اختر شاشة واحدة على الأقل.")
        return cleaned


class EmergencyAlertForm(forms.ModelForm):
    scope = forms.ChoiceField(
        label="نطاق الإرسال",
        choices=[
            ("all", "كل شاشات المدرسة/المدارس المحددة"),
            ("screens", "شاشة محددة أو عدة شاشات"),
        ],
        initial="all",
    )

    class Meta:
        model = EmergencyAlert
        fields = [
            "kind",
            "title",
            "message",
            "schools",
            "scope",
            "screens",
            "expires_at",
        ]
        widgets = {
            "schools": forms.CheckboxSelectMultiple(),
            "screens": forms.CheckboxSelectMultiple(),
            "message": forms.Textarea(attrs={"rows": 5, "maxlength": 600}),
            "expires_at": forms.DateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"type": "datetime-local"},
            ),
        }

    def __init__(self, *args, allowed_schools=None, **kwargs):
        super().__init__(*args, **kwargs)
        allowed_schools = allowed_schools if allowed_schools is not None else School.objects.none()
        self.fields["schools"].queryset = allowed_schools.order_by("name")
        # كما في التنبيهات العادية: شاشة مستهدفة بالفعل تبقى معروضة حتى لو
        # عُطّلت، حتى لا يُمحى استهدافها بصمت عند أول حفظ.
        targeted_ids = (
            set(self.instance.screens.values_list("id", flat=True))
            if self.instance.pk
            else set()
        )
        self.fields["screens"].queryset = (
            DisplayScreen.objects.filter(school__in=allowed_schools)
            .filter(Q(is_active=True) | Q(pk__in=targeted_ids))
            .select_related("school")
            .order_by("school__name", "name")
        )
        self.fields["expires_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        if allowed_schools.count() == 1:
            school = allowed_schools.first()
            self.fields["schools"].initial = [school.pk]

    def clean(self):
        cleaned = super().clean()
        schools = cleaned.get("schools")
        screens = cleaned.get("screens")
        scope = cleaned.get("scope")
        if not schools:
            self.add_error("schools", "اختر مدرسة واحدة على الأقل.")
            return cleaned
        if scope == "screens" and not screens:
            self.add_error("screens", "اختر شاشة واحدة على الأقل.")
        if screens and any(screen.school_id not in {school.pk for school in schools} for screen in screens):
            self.add_error("screens", "إحدى الشاشات لا تتبع المدارس المحددة.")
        # A selected screen is authoritative. This prevents a stale/default
        # `scope=all` value from silently broadening an emergency alert.
        if screens:
            cleaned["scope"] = "screens"
        elif scope == "all":
            cleaned["screens"] = DisplayScreen.objects.none()
        expires_at = cleaned.get("expires_at")
        if expires_at and expires_at <= timezone.now():
            self.add_error("expires_at", "وقت الانتهاء يجب أن يكون في المستقبل.")
        return cleaned


class ExcellenceForm(forms.ModelForm):
    MAX_PHOTO_MB = 5
    MAX_SOURCE_PHOTO_MB = 20

    class Meta:
        model = Excellence
        fields = [
            "teacher_name",
            "reason",
            "photo",
            "photo_url",
            "start_at",
            "end_at",
            "priority",
        ]
        widgets = {
            "teacher_name": forms.TextInput(attrs={"maxlength": 100}),
            "reason": forms.TextInput(attrs={"maxlength": 200}),
            "start_at": forms.DateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"type": "datetime-local"},
            ),
            "end_at": forms.DateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"type": "datetime-local"},
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("start_at", "end_at"):
            if name in self.fields:
                self.fields[name].input_formats = ["%Y-%m-%dT%H:%M"]
        if "teacher_name" in self.fields:
            self.fields["teacher_name"].label = "اسم المتميز/ة"
        if "photo" in self.fields and hasattr(self.fields["photo"].widget, "attrs"):
            self.fields["photo"].widget.attrs.setdefault("accept", "image/*")

    def clean_photo(self):
        file = self.cleaned_data.get("photo")
        if not file:
            return file
        source_max_bytes = self.MAX_SOURCE_PHOTO_MB * 1024 * 1024
        size = getattr(file, "size", 0)
        if size and size > source_max_bytes:
            raise ValidationError(f"حجم الصورة الخام يتجاوز {self.MAX_SOURCE_PHOTO_MB} م.ب.")
        file = optimize_uploaded_image(
            file,
            max_width=1600,
            max_height=1600,
            quality=82,
        )
        max_bytes = self.MAX_PHOTO_MB * 1024 * 1024
        if getattr(file, "size", 0) > max_bytes:
            raise ValidationError(f"حجم الصورة بعد المعالجة يتجاوز {self.MAX_PHOTO_MB} م.ب.")
        return file

    def clean(self):
        cleaned = super().clean()
        start_at = cleaned.get("start_at")
        end_at = cleaned.get("end_at")
        if start_at and end_at and end_at <= start_at:
            raise ValidationError("وقت الانتهاء يجب أن يكون بعد وقت البداية.")
        return cleaned


# ========================
# حصص الانتظار
# ========================

class StandbyForm(forms.ModelForm):
    class_name = forms.ModelChoiceField(
        queryset=SchoolClass.objects.none(),
        label="الفصل",
        required=True,
        empty_label="— اختر الفصل —"
    )
    # ✅ تحويل teacher_name من CharField إلى ModelChoiceField (dropdown)
    teacher = forms.ModelChoiceField(
        queryset=Teacher.objects.none(),
        label="اسم المعلم/ـة",
        required=True,
        empty_label="— اختر المعلم/ـة —",
        help_text="اختر المعلم/ـة من القائمة",
        widget=forms.Select(attrs={
            'class': 'form-select',
            'data-placeholder': 'اختر المعلم/ـة'
        })
    )

    class Meta:
        model = StandbyAssignment
        # ✅ استبعاد teacher_name من fields لأننا نستخدم حقل مخصص "teacher"
        fields = ["date", "period_index", "class_name", "notes"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)
        self._school = school

        if school is not None:
            self.fields["class_name"].queryset = SchoolClass.objects.filter(
                settings__school=school
            ).order_by("name")
            # ✅ تحميل قائمة المعلمين من نفس المدرسة
            self.fields["teacher"].queryset = Teacher.objects.filter(
                school=school
            ).order_by("name")
        else:
            self.fields["class_name"].queryset = SchoolClass.objects.none()
            self.fields["teacher"].queryset = Teacher.objects.none()

        # ✅ عند التعديل، نحمل المعلم الحالي من teacher_name
        if self.instance and self.instance.pk and self.instance.teacher_name:
            try:
                existing_teacher = Teacher.objects.filter(
                    school=school,
                    name=self.instance.teacher_name
                ).first()
                if existing_teacher:
                    self.initial['teacher'] = existing_teacher.pk
            except Exception:
                pass

    def save(self, commit=True):
        instance = super().save(commit=False)
        class_obj = self.cleaned_data["class_name"]
        instance.class_name = class_obj.name
        
        # ✅ تحويل Teacher object إلى teacher_name string
        teacher_obj = self.cleaned_data.get("teacher")
        if teacher_obj:
            instance.teacher_name = teacher_obj.name
        else:
            instance.teacher_name = ""
            
        if getattr(self, "_school", None) is not None:
            instance.school = self._school
        if commit:
            instance.save()
        return instance


# ========================
# الإشراف والمناوبة
# ========================

class DutyAssignmentForm(forms.ModelForm):
    teacher = forms.ModelChoiceField(
        queryset=Teacher.objects.none(),
        label="اسم المعلم/ـة",
        empty_label="— اختر المعلم/ـة —",
        help_text="اختر المعلم/ـة من القائمة",
        widget=forms.Select(attrs={
            'class': 'form-select',
            'data-placeholder': 'اختر المعلم/ـة'
        }),
        required=False  # Legacy edits may keep an existing name without a linked teacher.
    )
    
    def __init__(self, *args, **kwargs):
        self._school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)
        # A new assignment needs an actual teacher. Existing imported duties
        # can retain their stored name if that teacher no longer has a row.
        self.fields["teacher"].required = not bool(
            self.instance.pk and (self.instance.teacher_name or "").strip()
        )

        # تحميل المعلمين الخاصين بالمدرسة
        if self._school:
            self.fields["teacher"].queryset = Teacher.objects.filter(
                school=self._school
            ).order_by("name")
        else:
            self.fields["teacher"].queryset = Teacher.objects.none()
        
        # إذا كان هناك قيمة موجودة في teacher_name، نحاول إيجاد المعلم
        if self.instance and self.instance.pk and self.instance.teacher_name:
            try:
                teacher = Teacher.objects.get(
                    school=self._school,
                    name=self.instance.teacher_name
                )
                self.initial['teacher'] = teacher
            except (Teacher.DoesNotExist, Teacher.MultipleObjectsReturned):
                pass
    
    def save(self, commit=True):
        instance = super().save(commit=False)
        # نحوّل Teacher object إلى اسم نصي
        teacher = self.cleaned_data.get('teacher')
        if teacher:
            instance.teacher_name = teacher.name
        if commit:
            instance.save()
        return instance

    class Meta:
        model = DutyAssignment
        fields = [
            "date",
            "teacher",  # نستخدم teacher بدلاً من teacher_name في Form
            "duty_type",
            "location",
            "priority",
            "is_active",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date", "class": "dp-input", "dir": "ltr", "lang": "en"}),
            "location": forms.TextInput(attrs={"maxlength": 120, "class": "dp-input"}),
            "priority": forms.NumberInput(attrs={"class": "dp-input"}),
        }


# ========================
# شاشات العرض والحصص
# ========================

class DisplayScreenForm(forms.ModelForm):
    class Meta:
        model = DisplayScreen
        fields = ["name", "is_active", "monitor_always_on"]
        widgets = {
            "name": forms.TextInput(attrs={"maxlength": "100"}),
        }


class LessonForm(forms.ModelForm):
    class Meta:
        model = ClassLesson
        fields = [
            "school_class",
            "weekday",
            "period_index",
            "subject",
            "teacher",
            "is_active",
        ]
        widgets = {
            "weekday": forms.Select(choices=WEEKDAYS),
        }

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)

        if school is not None:
            self.fields["school_class"].queryset = SchoolClass.objects.filter(
                settings__school=school
            ).order_by("name")
            self.fields["subject"].queryset = Subject.objects.filter(
                school=school
            ).order_by("name")
            self.fields["teacher"].queryset = Teacher.objects.filter(
                school=school
            ).order_by("name")
        else:
            self.fields["school_class"].queryset = SchoolClass.objects.none()
            self.fields["subject"].queryset = Subject.objects.none()
            self.fields["teacher"].queryset = Teacher.objects.none()

    def clean(self):
        cleaned = super().clean()
        school_class = cleaned.get("school_class")
        weekday = cleaned.get("weekday")
        index = cleaned.get("period_index")
        if school_class and weekday and index:
            duplicate = ClassLesson.objects.filter(
                settings_id=school_class.settings_id, school_class=school_class,
                weekday=weekday, period_index=index,
            ).exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise ValidationError("توجد حصة لهذا الفصل في اليوم ورقم الحصة المحددين. عدّل الحصة الحالية بدل إضافتها مجددًا.")
        return cleaned


# =========================
# نماذج لوحة إدارة النظام (SaaS Admin)
# =========================

class SchoolForm(forms.ModelForm):
    class Meta:
        model = School
        fields = ["name", "slug", "logo", "is_active"]
        labels = {
            "name": "اسم المدرسة",
            "slug": "الرابط (slug)",
            "logo": "شعار المدرسة",
            "is_active": "مدرسة مفعّلة",
        }
        widgets = {"logo": forms.ClearableFileInput()}

    def clean_logo(self):
        file_obj = self.cleaned_data.get("logo")
        if not file_obj:
            return file_obj
        return optimize_uploaded_image(
            file_obj,
            max_width=1200,
            max_height=1200,
            quality=84,
        )


class SelfServiceSchoolCreateForm(forms.Form):
    """Create an additional school and select its first paid plan.

    The plan is not activated here.  The selected plan is carried to the
    existing checkout page after the school has been created.
    """

    name = forms.CharField(
        label="اسم المدرسة",
        max_length=150,
        min_length=3,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "organization",
                "placeholder": "مثال: مدارس الرواد الأهلية",
            }
        ),
    )
    school_type = forms.ChoiceField(
        label="نوع المدرسة",
        choices=(("", "اختر نوع المدرسة"), *SchoolType.choices),
        widget=forms.Select(),
    )
    logo = forms.ImageField(
        label="شعار المدرسة (اختياري)",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={"accept": ".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"}
        ),
    )
    screen_count = forms.ChoiceField(
        label="عدد شاشات العرض",
        choices=(),
        help_text="سنُظهر الباقات المتاحة لهذا العدد فقط.",
        widget=forms.Select(),
    )
    plan = forms.ModelChoiceField(
        label="الباقة",
        queryset=SubscriptionPlan.objects.none(),
        empty_label=None,
        widget=forms.RadioSelect(),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        paid_plans = SubscriptionPlan.objects.filter(
            is_active=True,
            price__gt=0,
        ).order_by("sort_order", "price", "id")
        self.fields["plan"].queryset = paid_plans

        screen_limits = []
        has_unlimited = False
        for max_screens in paid_plans.values_list("max_screens", flat=True).distinct():
            if max_screens is None:
                has_unlimited = True
            else:
                screen_limits.append(int(max_screens))

        choices = [(str(count), f"{count} شاشة" if count == 1 else f"{count} شاشات") for count in sorted(set(screen_limits))]
        if has_unlimited:
            choices.append(("unlimited", "عدد غير محدود"))
        self.fields["screen_count"].choices = choices

        if not self.is_bound and choices:
            selected_count = choices[0][0]
            self.initial.setdefault("screen_count", selected_count)
            matching_plans = [
                plan
                for plan in paid_plans
                if self._screen_value(plan) == selected_count
            ]
            featured = next((plan for plan in matching_plans if plan.is_featured), None)
            selected_plan = featured or (matching_plans[0] if matching_plans else None)
            if selected_plan is not None:
                self.initial.setdefault("plan", selected_plan.pk)

    @staticmethod
    def _screen_value(plan) -> str:
        max_screens = getattr(plan, "max_screens", None)
        return "unlimited" if max_screens is None else str(int(max_screens))

    def clean_name(self):
        name = re.sub(r"\s+", " ", (self.cleaned_data.get("name") or "").strip())
        if self.user is not None:
            try:
                if self.user.profile.schools.filter(name__iexact=name).exists():
                    raise ValidationError("هذه المدرسة مرتبطة بحسابك بالفعل.")
            except UserProfile.DoesNotExist:
                pass
        return name

    def clean_logo(self):
        file_obj = self.cleaned_data.get("logo")
        if not file_obj:
            return file_obj
        return optimize_uploaded_image(
            file_obj,
            max_width=1200,
            max_height=1200,
            quality=84,
        )

    def clean(self):
        cleaned = super().clean()
        plan = cleaned.get("plan")
        screen_count = cleaned.get("screen_count")
        if plan is not None and screen_count and self._screen_value(plan) != screen_count:
            self.add_error("plan", "الباقة المختارة لا تطابق عدد الشاشات المحدد.")
        return cleaned


class SchoolSubscriptionForm(forms.ModelForm):
    """
    إدارة اشتراك المدرسة بناءً على موديل SchoolSubscription:
    fields = [school, plan, starts_at, ends_at, status, notes]
    """

    class Meta:
        model = SchoolSubscription
        fields = [
            "school",
            "plan",
            "starts_at",
            "ends_at",
            "status",
            "closure_reason",
            "closure_notes",
            "notes",
        ]
        labels = {
            "school": "المدرسة",
            "plan": "الخطة",
            "starts_at": "تاريخ بداية الاشتراك",
            "ends_at": "تاريخ نهاية الاشتراك",
            "status": "حالة الاشتراك",
            "closure_reason": "سبب الإلغاء أو عدم التجديد",
            "closure_notes": "تفاصيل السبب",
            "notes": "ملاحظات",
        }
        widgets = {
            "starts_at": forms.DateInput(attrs={"type": "date"}),
            "ends_at": forms.DateInput(attrs={"type": "date"}),
            "status": forms.Select(),
            "closure_reason": forms.Select(),
            "closure_notes": forms.Textarea(attrs={"rows": 3}),
        }

    payment_method = forms.ChoiceField(
        label="طريقة الدفع",
        required=False,
        choices=[
            ("", "— اختر —"),
            ("bank_transfer", "تحويل بنكي"),
            ("moyasar", "ميسر"),
            ("tamara", "تمارا"),
        ],
        widget=forms.Select(),
        help_text="يُطلب فقط عند إنشاء اشتراك مدفوع (غير مجاني).",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["school"].queryset = School.objects.all().order_by("name")
        self.fields["plan"].queryset = SubscriptionPlan.objects.all().order_by("name")

        # عند تعديل اشتراك سابق: عبّئ طريقة الدفع من آخر عملية دفع (إن وجدت)
        try:
            if getattr(self.instance, "pk", None) and "payment_method" in self.fields:
                from subscriptions.models import SubscriptionPaymentOperation

                op = (
                    SubscriptionPaymentOperation.objects.filter(
                        school=getattr(self.instance, "school", None),
                        subscription=self.instance,
                    )
                    .order_by("-created_at", "-id")
                    .first()
                )
                if op is not None and getattr(op, "method", None):
                    self.fields["payment_method"].initial = op.method
                    self.fields["payment_method"].disabled = True
                    self.fields["payment_method"].help_text = "طريقة الدفع محفوظة في سجل مالي سابق؛ تعديل الاشتراك لا يغيّر الدفعة أو فاتورتها."
        except Exception:
            pass

        # المطلوب: منع إدخال/تعديل تاريخ النهاية (يُحسب تلقائيًا من مدة الباقة).
        if "ends_at" in self.fields:
            self.fields["ends_at"].required = False
            self.fields["ends_at"].disabled = True
            self.fields["ends_at"].help_text = (
                "يتم حساب تاريخ النهاية تلقائيًا من مدة الباقة عند الحفظ."
            )
            self.fields["ends_at"].widget.attrs.update(
                {
                    "readonly": "readonly",
                    "disabled": "disabled",
                    "aria-disabled": "true",
                }
            )

    def clean(self):
        from subscriptions.models import SubscriptionPaymentOperation

        cleaned = super().clean()
        start = cleaned.get("starts_at")
        end = cleaned.get("ends_at")
        status = cleaned.get("status")
        closure_reason = cleaned.get("closure_reason")
        if start and end and end < start:
            raise ValidationError("تاريخ النهاية يجب أن يكون بعد تاريخ البداية.")
        if status in {"cancelled", "expired"} and not closure_reason:
            self.add_error(
                "closure_reason",
                "الرجاء تحديد سبب الإلغاء أو عدم التجديد لإكمال الإجراء.",
            )

        # في إضافة اشتراك يدويًا: نطلب طريقة الدفع للخطط المدفوعة فقط.
        plan = cleaned.get("plan")
        payment_method = (cleaned.get("payment_method") or "").strip()
        is_create = not getattr(self.instance, "pk", None)
        try:
            plan_price = getattr(plan, "price", 0) or 0
        except Exception:
            plan_price = 0

        # الباقة المجانية (السعر 0) لا نطلب طريقة دفع.
        #
        # طريقة الدفع ليست حقلاً تجميلياً: عملية الدفع هي ما يُبنى منه إصدار
        # الفاتورة وإرسالها للعميل. اشتراك مدفوع يُحفظ بدونها يبقى بلا فاتورة
        # وبلا رسالة إلى الأبد، فالتحقق هنا هو ما يمنع ذلك.
        #
        # كان هذا الشرط ملفوفاً بـ ``except Exception: pass`` ليتسامح مع سعر
        # غير رقمي، لكن ``ValidationError`` نفسه من ``Exception`` فكان يُبتلع
        # لحظة رفعه ولا يظهر للمستخدم أبداً. الالتقاط الآن محصور في تحويل
        # السعر وحده.
        if is_create and plan is not None:
            try:
                plan_price_value = float(plan_price)
            except (TypeError, ValueError):
                # سعر غير قابل للتحويل: لا نكسر النموذج ولا نفترض أنه مدفوع.
                plan_price_value = 0.0

            if plan_price_value > 0 and not payment_method:
                self.add_error(
                    "payment_method",
                    "الرجاء تحديد طريقة الدفع للاشتراك المدفوع.",
                )

        if self.instance.pk and SubscriptionPaymentOperation.objects.filter(subscription_id=self.instance.pk).exists():
            original_school_id = SchoolSubscription.objects.values_list("school_id", flat=True).get(pk=self.instance.pk)
            if cleaned.get("school") and cleaned["school"].pk != original_school_id:
                self.add_error("school", "لا يمكن نقل اشتراك ذي سجلات مالية إلى مدرسة أخرى.")
        return cleaned


class SubscriptionScreenAddonForm(forms.ModelForm):
    class Meta:
        model = SubscriptionScreenAddon
        fields = [
            "subscription",
            "screens_added",
            "pricing_cycle",
            "validity_days",
            "pricing_strategy",
            "bundle_price",
            "unit_price",
            "starts_at",
            "ends_at",
            "status",
            "notes",
        ]
        labels = {
            "subscription": "الاشتراك",
            "screens_added": "عدد الشاشات المضافة",
            "pricing_cycle": "دورة تسعير الإضافة",
            "validity_days": "مدة الصلاحية (أيام)",
            "pricing_strategy": "طريقة التسعير",
            "bundle_price": "سعر الإضافة للفترة",
            "unit_price": "سعر للشاشة",
            "starts_at": "بداية الإضافة",
            "ends_at": "نهاية الإضافة",
            "status": "الحالة",
            "notes": "ملاحظات",
        }
        widgets = {
            "starts_at": forms.DateInput(attrs={"type": "date"}),
            "ends_at": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subscription"].queryset = (
            SchoolSubscription.objects.select_related("school", "plan")
            .order_by("-starts_at", "-id")
        )


# =========================
# طلبات الاشتراك/التجديد (مستخدم المدرسة)
# =========================

class _ReceiptImageValidationMixin:
    receipt_max_size_bytes = 5 * 1024 * 1024
    receipt_source_max_size_bytes = 20 * 1024 * 1024
    receipt_allowed_exts = {".jpg", ".jpeg", ".png", ".webp"}

    def _validate_receipt_image(self, file_obj):
        if not file_obj:
            raise ValidationError("الرجاء إرفاق صورة الإيصال.")

        # content-type (best-effort)
        content_type = getattr(file_obj, "content_type", "") or ""
        if content_type and not content_type.lower().startswith("image/"):
            raise ValidationError("الملف المرفوع يجب أن يكون صورة فقط.")

        # extension
        ext = os.path.splitext(getattr(file_obj, "name", "") or "")[1].lower()
        if ext and ext not in self.receipt_allowed_exts:
            raise ValidationError("صيغة الإيصال غير مدعومة. الصيغ المسموحة: JPG, PNG, WEBP")

        # hard limit on raw upload before processing
        size = getattr(file_obj, "size", None)
        if size is not None and int(size) > self.receipt_source_max_size_bytes:
            raise ValidationError("حجم الصورة الخام كبير جدًا. الحد الأقصى 20MB.")

        file_obj = optimize_uploaded_image(
            file_obj,
            max_width=1800,
            max_height=1800,
            quality=80,
        )

        final_size = getattr(file_obj, "size", None)
        if final_size is not None and int(final_size) > self.receipt_max_size_bytes:
            raise ValidationError("حجم الصورة بعد المعالجة كبير جدًا. الحد الأقصى 5MB.")

        return file_obj


class SubscriptionRenewalRequestForm(forms.Form, _ReceiptImageValidationMixin):
    receipt_image = forms.ImageField(
        label="إيصال التحويل (صورة)",
        widget=forms.ClearableFileInput(
            attrs={"class": "dp-input"}
        ),
    )
    transfer_note = forms.CharField(
        label="ملاحظة (اختياري)",
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "dp-input"}
        ),
    )

    def clean_receipt_image(self):
        return self._validate_receipt_image(self.cleaned_data.get("receipt_image"))


class SubscriptionNewRequestForm(forms.Form, _ReceiptImageValidationMixin):
    plan = forms.ModelChoiceField(
        queryset=SubscriptionPlan.objects.filter(is_active=True).order_by("sort_order", "price", "id"),
        label="اختر الخطة",
        widget=forms.Select(
            attrs={"class": "dp-input"}
        ),
    )
    receipt_image = forms.ImageField(
        label="إيصال التحويل (صورة)",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={"class": "dp-input"}
        ),
    )
    transfer_note = forms.CharField(
        label="ملاحظة (اختياري)",
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "dp-input"}
        ),
    )

    def clean_receipt_image(self):
        receipt = self.cleaned_data.get("receipt_image")
        if not receipt:
            return None
        return self._validate_receipt_image(receipt)

    def clean(self):
        cleaned = super().clean()
        plan = cleaned.get("plan")
        receipt = cleaned.get("receipt_image")
        try:
            is_paid = bool(plan and (getattr(plan, "price", 0) or 0) > 0)
        except Exception:
            is_paid = True
        if is_paid and not receipt:
            self.add_error("receipt_image", "أرفق صورة إيصال التحويل للخطة المدفوعة.")
        return cleaned


# =========================
# نماذج المستخدمين (ربط المستخدم بالمدارس + active_school)
# =========================

class SystemUserCreateForm(UserCreationForm):
    """
    إنشاء مستخدم جديد + ربطه بالمدارس وتعيين مدرسة نشطة.
    يعتمد UserCreationForm للاستفادة من تحققات كلمة المرور.
    """
    schools = forms.ModelMultipleChoiceField(
        queryset=School.objects.all().order_by("name"),
        required=True,
        label="المدارس",
        help_text="المدارس التي يرتبط بها هذا المستخدم.",
        widget=forms.SelectMultiple(
            attrs={"class": "dp-input"}
        ),
    )
    active_school = forms.ModelChoiceField(
        queryset=School.objects.all().order_by("name"),
        required=False,
        label="المدرسة النشطة",
        help_text="اختياري: لو لم تحدد سيتم اختيار أول مدرسة مرتبطة.",
        widget=forms.Select(
            attrs={"class": "dp-input"}
        ),
    )
    mobile = forms.CharField(
        label="رقم الجوال",
        required=False,
        max_length=20,
        widget=forms.TextInput(
            attrs={"class": "dp-input"}
        ),
    )

    class Meta(UserCreationForm.Meta):
        model = UserModel
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "mobile",
            "is_active",
            "is_staff",
            "is_superuser",
        ]
        labels = {
            "username": "اسم المستخدم",
            "email": "البريد الإلكتروني",
            "first_name": "الاسم الأول",
            "last_name": "اسم العائلة",
            "is_active": "حساب نشط",
            "is_staff": "صلاحيات موظف (staff)",
            "is_superuser": "مدير نظام (superuser)",
        }

    def clean(self):
        cleaned = super().clean()
        schools = cleaned.get("schools")
        active_school = cleaned.get("active_school")
        if active_school and schools and active_school not in schools:
            raise ValidationError("المدرسة النشطة يجب أن تكون ضمن المدارس المرتبطة.")
        return cleaned

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=commit)
        profile = _get_profile(user)

        schools = self.cleaned_data.get("schools")
        profile.schools.set(schools)

        active_school = self.cleaned_data.get("active_school")
        if active_school:
            profile.active_school = active_school
        else:
            profile.active_school = profile.schools.order_by("id").first()

        profile.mobile = self.cleaned_data.get("mobile")
        profile.save()
        return user


class SystemUserUpdateForm(forms.ModelForm):
    """
    تعديل بيانات المستخدم + إدارة المدارس + تعيين active_school + تغيير كلمة المرور اختياريًا.
    """
    schools = forms.ModelMultipleChoiceField(
        queryset=School.objects.all().order_by("name"),
        required=False,
        label="المدارس",
        help_text="المدارس المرتبط بها المستخدم.",
        widget=forms.SelectMultiple(
            attrs={"class": "dp-input"}
        ),
    )
    active_school = forms.ModelChoiceField(
        queryset=School.objects.all().order_by("name"),
        required=False,
        label="المدرسة النشطة",
        help_text="اختياري: لو لم تحدد سيتم اختيار أول مدرسة مرتبطة.",
        widget=forms.Select(
            attrs={"class": "dp-input"}
        ),
    )
    mobile = forms.CharField(
        label="رقم الجوال",
        required=False,
        max_length=20,
        widget=forms.TextInput(
            attrs={"class": "dp-input"}
        ),
    )

    new_password1 = forms.CharField(
        label="كلمة المرور الجديدة",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        required=False,
        help_text="اترك الحقلين فارغين إذا لا تريد تغيير كلمة المرور."
    )
    new_password2 = forms.CharField(
        label="تأكيد كلمة المرور الجديدة",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        required=False,
    )

    class Meta:
        model = UserModel
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "mobile",
            "is_active",
            "is_staff",
            "is_superuser",
        ]
        labels = {
            "username": "اسم المستخدم",
            "email": "البريد الإلكتروني",
            "first_name": "الاسم الأول",
            "last_name": "اسم العائلة",
            "is_active": "حساب نشط",
            "is_staff": "صلاحيات موظف (staff)",
            "is_superuser": "مدير نظام (superuser)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if getattr(self.instance, "pk", None):
            profile = UserProfile.objects.filter(user=self.instance).first()
            if profile:
                self.fields["schools"].initial = list(profile.schools.all())
                self.fields["active_school"].initial = profile.active_school_id
                self.fields["mobile"].initial = profile.mobile

    def clean(self):
        cleaned = super().clean()

        schools = cleaned.get("schools")
        active_school = cleaned.get("active_school")
        if active_school and schools and active_school not in schools:
            raise ValidationError("المدرسة النشطة يجب أن تكون ضمن المدارس المرتبطة.")

        p1 = cleaned.get("new_password1")
        p2 = cleaned.get("new_password2")
        if p1 or p2:
            if not p1 or not p2:
                raise ValidationError("لابد من إدخال كلمة المرور الجديدة وتأكيدها.")
            if p1 != p2:
                raise ValidationError("كلمتا المرور غير متطابقتين.")
            if len(p1) < 8:
                raise ValidationError("يجب أن تكون كلمة المرور ٨ أحرف على الأقل.")
            _validate_updated_password(p1, self.instance, cleaned)

        return cleaned

    @transaction.atomic
    def save(self, commit=True):
        from core.owner_safety import protect_owner_change

        if commit and self.instance.pk:
            protect_owner_change(self.instance.pk, active=self.cleaned_data.get("is_active", False),
                                 superuser=self.cleaned_data.get("is_superuser", self.instance.is_superuser))
        user = super().save(commit=False)

        new_password = self.cleaned_data.get("new_password1")
        if new_password:
            user.set_password(new_password)

        if commit:
            user.save()

        profile = _get_profile(user)

        schools = self.cleaned_data.get("schools")
        if schools is not None:
            profile.schools.set(schools)

        active_school = self.cleaned_data.get("active_school")
        if active_school:
            profile.active_school = active_school
        else:
            profile.active_school = profile.schools.order_by("id").first()

        profile.mobile = self.cleaned_data.get("mobile")

        # ضمان: لو active_school ليست ضمن المدارس بعد التعديل
        if profile.active_school_id and profile.schools.filter(id=profile.active_school_id).exists() is False:
            profile.active_school = profile.schools.order_by("id").first()

        profile.save()
        return user


class SystemEmployeeCreateForm(UserCreationForm):
    """إنشاء موظف نظام (بدون ربط بمدارس)."""

    ROLE_SUPPORT = ROLE_SUPPORT_KEY

    role = forms.ChoiceField(
        label="الدور الوظيفي",
        choices=ROLE_CHOICES,
        widget=forms.Select(
            attrs={"class": "dp-input"}
        ),
    )
    permissions = forms.MultipleChoiceField(
        label="صلاحيات المنصة",
        required=True,
        choices=[(item["key"], item["label"]) for item in PERMISSION_DEFINITIONS],
        widget=forms.CheckboxSelectMultiple,
    )

    mobile = forms.CharField(
        label="رقم الجوال",
        required=False,
        max_length=20,
        widget=forms.TextInput(
            attrs={"class": "dp-input"}
        ),
    )

    class Meta(UserCreationForm.Meta):
        model = UserModel
        fields = [
            "username",
            "email",
            "first_name",
            "last_name",
            "mobile",
            "is_active",
            "role",
            "permissions",
        ]
        labels = {
            "username": "اسم المستخدم",
            "email": "البريد الإلكتروني",
            "first_name": "الاسم الأول",
            "last_name": "اسم العائلة",
            "is_active": "حساب نشط",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields["role"].initial = ROLE_SUPPORT_KEY
            self.fields["permissions"].initial = role_permissions(ROLE_SUPPORT_KEY)

    def clean_permissions(self):
        permissions = normalize_permission_keys(self.cleaned_data.get("permissions"))
        if not permissions:
            raise ValidationError("اختر صلاحية واحدة على الأقل للموظف.")
        return permissions

    @transaction.atomic
    def save(self, commit=True, *, created_by=None):
        user = super().save(commit=False)

        # الموظف يجب أن يكون staff دائمًا
        user.is_staff = True
        role = self.cleaned_data.get("role")
        user.is_superuser = False

        if commit:
            user.save()

        # ربط/إنشاء بروفايل بدون مدارس
        profile = _get_profile(user)
        profile.schools.clear()
        profile.active_school = None
        profile.mobile = self.cleaned_data.get("mobile")
        profile.save()

        from django.contrib.auth.models import Group

        platform_group, _ = Group.objects.get_or_create(name="Platform Staff")
        support_group, _ = Group.objects.get_or_create(name="Support")
        user.groups.add(platform_group)
        if role == self.ROLE_SUPPORT:
            user.groups.add(support_group)
        else:
            user.groups.remove(support_group)

        SystemEmployeeProfile.objects.update_or_create(
            user=user,
            defaults={
                "role": role,
                "permission_keys": self.cleaned_data["permissions"],
                "created_by": created_by,
            },
        )

        return user


class SystemEmployeeUpdateForm(forms.ModelForm):
    """Update a platform employee without exposing tenant or superuser fields."""

    role = forms.ChoiceField(
        label="الدور الوظيفي",
        choices=ROLE_CHOICES,
        widget=forms.Select(
            attrs={"class": "dp-input"}
        ),
    )
    permissions = forms.MultipleChoiceField(
        label="صلاحيات المنصة",
        required=True,
        choices=[(item["key"], item["label"]) for item in PERMISSION_DEFINITIONS],
        widget=forms.CheckboxSelectMultiple,
    )
    mobile = forms.CharField(
        label="رقم الجوال",
        required=False,
        max_length=20,
        widget=forms.TextInput(
            attrs={"class": "dp-input"}
        ),
    )
    new_password1 = forms.CharField(
        label="كلمة مرور جديدة",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="اترك الحقلين فارغين للإبقاء على كلمة المرور الحالية.",
    )
    new_password2 = forms.CharField(
        label="تأكيد كلمة المرور الجديدة",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    class Meta:
        model = UserModel
        fields = ["username", "email", "first_name", "last_name", "mobile", "is_active"]
        labels = {
            "username": "اسم المستخدم",
            "email": "البريد الإلكتروني",
            "first_name": "الاسم الأول",
            "last_name": "اسم العائلة",
            "is_active": "حساب نشط",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            employee = self.instance.system_employee_profile
        except SystemEmployeeProfile.DoesNotExist:
            employee = None
        profile = UserProfile.objects.filter(user=self.instance).first()
        if profile:
            self.fields["mobile"].initial = profile.mobile
        self.fields["role"].initial = employee.role if employee else ROLE_SUPPORT_KEY
        self.fields["permissions"].initial = (
            normalize_permission_keys(employee.permission_keys)
            if employee
            else role_permissions(ROLE_SUPPORT_KEY)
        )

    def clean_permissions(self):
        permissions = normalize_permission_keys(self.cleaned_data.get("permissions"))
        if not permissions:
            raise ValidationError("اختر صلاحية واحدة على الأقل للموظف.")
        return permissions

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("new_password1")
        password2 = cleaned.get("new_password2")
        if password1 or password2:
            if not password1 or not password2:
                raise ValidationError("أدخل كلمة المرور الجديدة وتأكيدها.")
            if password1 != password2:
                raise ValidationError("كلمتا المرور غير متطابقتين.")
            if len(password1) < 8:
                raise ValidationError("يجب أن تكون كلمة المرور 8 أحرف على الأقل.")
            _validate_updated_password(password1, self.instance, cleaned)
        return cleaned

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = True
        user.is_superuser = False
        if self.cleaned_data.get("new_password1"):
            user.set_password(self.cleaned_data["new_password1"])
        if commit:
            user.save()

        profile = _get_profile(user)
        profile.schools.clear()
        profile.active_school = None
        profile.mobile = self.cleaned_data.get("mobile")
        profile.save()

        from django.contrib.auth.models import Group

        platform_group, _ = Group.objects.get_or_create(name="Platform Staff")
        support_group, _ = Group.objects.get_or_create(name="Support")
        user.groups.add(platform_group)
        role = self.cleaned_data["role"]
        if role == ROLE_SUPPORT_KEY:
            user.groups.add(support_group)
        else:
            user.groups.remove(support_group)
        SystemEmployeeProfile.objects.update_or_create(
            user=user,
            defaults={
                "role": role,
                "permission_keys": self.cleaned_data["permissions"],
            },
        )
        return user


from core.models import SupportTicket

class SubscriptionPlanForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["duration_days"].required = True
        self.fields["code"].help_text = "رمز داخلي فريد بالإنجليزية، مثل: basic أو annual-pro."
        self.fields["price"].help_text = "السعر الكامل للباقة بالريال السعودي."
        self.fields["duration_days"].help_text = "عدد أيام الاشتراك، مثل 365 للباقة السنوية."
        self.fields["description"].help_text = "وصف قصير يساعد المدرسة على اختيار الباقة المناسبة."
        self.fields["card_features"].help_text = "اكتب كل ميزة في سطر مستقل، وستظهر بالترتيب نفسه داخل البطاقة."

    class Meta:
        model = SubscriptionPlan
        fields = (
            "name",
            "description",
            "code",
            "price",
            "duration_days",
            "max_screens",
            "card_badge_text",
            "card_duration_text",
            "card_price_caption",
            "card_monthly_text",
            "card_features",
            "card_screen_text",
            "card_cta_text",
            "show_card_badge",
            "show_card_duration",
            "show_monthly_equivalent",
            "show_screen_limit",
            "sort_order",
            "is_featured",
            "is_active",
        )
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "مثال: الباقة السنوية"}),
            "description": forms.Textarea(
                attrs={"rows": "3", "placeholder": "مثال: مناسبة للمدارس التي تشغّل ثلاث شاشات."}
            ),
            "code": forms.TextInput(attrs={"placeholder": "annual-pro", "dir": "ltr"}),
            "price": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
            "duration_days": forms.NumberInput(attrs={"min": "1"}),
            "max_screens": forms.NumberInput(attrs={"min": "1", "placeholder": "فارغ = غير محدود"}),
            "card_badge_text": forms.TextInput(attrs={"placeholder": "مثال: متاحة للاشتراك"}),
            "card_duration_text": forms.TextInput(attrs={"placeholder": "فارغ = يُنشأ تلقائياً"}),
            "card_price_caption": forms.TextInput(attrs={"placeholder": "فارغ = يُنشأ تلقائياً"}),
            "card_monthly_text": forms.TextInput(attrs={"placeholder": "فارغ = يُحسب تلقائياً"}),
            "card_features": forms.Textarea(
                attrs={"rows": "5", "placeholder": "جميع مزايا النظام كاملة\nدعم فني مباشر"}
            ),
            "card_screen_text": forms.TextInput(attrs={"placeholder": "فارغ = يُنشأ من عدد الشاشات"}),
            "card_cta_text": forms.TextInput(attrs={"placeholder": "اطلب هذه الباقة"}),
            "sort_order": forms.NumberInput(attrs={"min": "0"}),
        }

    def clean_code(self):
        return (self.cleaned_data.get("code") or "").strip().lower()

    def clean(self):
        cleaned = super().clean()
        for field_name in ("duration_days",):
            value = cleaned.get(field_name)
            if value is not None and value < 1:
                self.add_error(field_name, "يجب أن تكون القيمة 1 أو أكثر.")
        return cleaned

    def save(self, commit=True):
        plan = super().save(commit=False)
        if commit:
            plan.save()
            if plan.is_featured:
                SubscriptionPlan.objects.exclude(pk=plan.pk).filter(is_featured=True).update(
                    is_featured=False
                )
        return plan

from core.models import SupportTicket, TicketComment
from core.tenant_access import authorized_active_school

class TicketCommentForm(forms.ModelForm):
    class Meta:
        model = TicketComment
        fields = ["message"]
        widgets = {
            "message": forms.Textarea(attrs={"rows": 3, "class": "dp-input", "placeholder": "أضف ردك هنا..."}),
        }

class SupportTicketForm(forms.ModelForm):
    class Meta:
        model = SupportTicket
        fields = ["subject", "message", "priority"]
        widgets = {
            "subject": forms.TextInput(attrs={"class": "dp-input"}),
            "message": forms.Textarea(attrs={"class": "dp-input", "rows": 4}),
            "priority": forms.Select(attrs={"class": "dp-input"}),
        }


class CustomerSupportTicketForm(forms.ModelForm):
    school_name = forms.CharField(
        label="اسم المدرسة",
        required=False,
        widget=forms.TextInput(attrs={"class": "dp-input dp-input--readonly", "readonly": "readonly"})
    )
    admin_name = forms.CharField(
        label="اسم المسؤول",
        required=False,
        widget=forms.TextInput(attrs={"class": "dp-input dp-input--readonly", "readonly": "readonly"})
    )
    mobile_number = forms.CharField(
        label="رقم الجوال",
        required=False,
        widget=forms.TextInput(attrs={"class": "dp-input dp-input--readonly", "readonly": "readonly"})
    )

    class Meta:
        model = SupportTicket
        fields = ["subject", "message"]
        widgets = {
            "subject": forms.TextInput(attrs={"class": "dp-input"}),
            "message": forms.Textarea(attrs={"class": "dp-input", "rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if user:
            profile = getattr(user, 'profile', None)
            school = authorized_active_school(profile, user=user, clear_invalid=False) if profile else None
            if school:
                self.fields['school_name'].initial = school.name
            
            self.fields['admin_name'].initial = user.get_full_name() or user.username
            
            if profile and profile.mobile:
                self.fields['mobile_number'].initial = profile.mobile


class DiscountCodeForm(forms.ModelForm):
    """نموذج مدير المنصة لإنشاء أكواد الخصم وتعديلها."""

    class Meta:
        from subscriptions.models import DiscountCode as _DiscountCode

        model = _DiscountCode
        fields = (
            "code",
            "discount_type",
            "percent",
            "amount",
            "valid_from",
            "valid_until",
            "max_uses",
            "plans",
            "is_active",
            "notes",
        )
        widgets = {
            "code": forms.TextInput(
                attrs={"placeholder": "SAVE20", "dir": "ltr", "style": "text-transform: uppercase;"}
            ),
            "plans": forms.CheckboxSelectMultiple(),
            "percent": forms.NumberInput(attrs={"min": "0.01", "max": "100", "step": "0.01"}),
            "amount": forms.NumberInput(attrs={"min": "0.01", "step": "0.01"}),
            "valid_from": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "valid_until": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "max_uses": forms.NumberInput(attrs={"min": "1"}),
            "notes": forms.TextInput(attrs={"placeholder": "ملاحظة داخلية (اختياري)"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].help_text = "يُحفظ بأحرف كبيرة؛ هو ما يدخله المشترك عند الدفع."
        self.fields["valid_until"].help_text = "اتركه فارغاً لعدم تحديد نهاية زمنية."
        self.fields["max_uses"].help_text = "عدد الأكواد المصدرة؛ يتوقف الكود تلقائياً عند بلوغه."
        for name in ("valid_from", "valid_until"):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"]
        from core.models import SubscriptionPlan as _SubscriptionPlan

        self.fields["plans"].required = False
        self.fields["plans"].queryset = _SubscriptionPlan.objects.filter(is_active=True).order_by(
            "sort_order", "price", "id"
        )
        self.fields["plans"].help_text = "اتركها كلها بلا تحديد ليصلح الكود لكل الباقات."

    def clean_code(self):
        from subscriptions.models import DiscountCode as _DiscountCode

        return _DiscountCode.normalize(self.cleaned_data.get("code"))

    def clean(self):
        cleaned = super().clean()
        dtype = cleaned.get("discount_type")
        if dtype == "percent":
            cleaned["amount"] = None
        elif dtype == "amount":
            cleaned["percent"] = None
        max_uses = cleaned.get("max_uses")
        if self.instance.pk and max_uses is not None:
            used = self.instance.used_count
            if max_uses < used:
                self.add_error(
                    "max_uses",
                    f"لا يمكن تحديد العدد بأقل من الاستخدامات المسجلة فعلاً ({used}).",
                )
        return cleaned
