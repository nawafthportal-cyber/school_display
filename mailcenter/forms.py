from __future__ import annotations

import re

from django import forms
from django.core.validators import validate_email
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from core.models import School, UserProfile
from subscriptions.models import SchoolSubscription


MAX_RECIPIENTS = 100


def school_matches_subscription_filter(school, status):
    if status == "active":
        return school.mail_has_active_subscription
    if status == "inactive":
        return not school.mail_has_active_subscription
    if status == "expired_trial":
        return (
            not school.mail_has_active_subscription
            and school.mail_has_ended_trial
            and not school.mail_has_paid_subscription
        )
    return True


class SchoolRecipientWidget(forms.CheckboxSelectMultiple):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if school := getattr(value, "instance", None):
            option["attrs"]["data-subscription-active"] = "1" if school.mail_has_active_subscription else "0"
            option["attrs"]["data-expired-trial"] = "1" if school_matches_subscription_filter(school, "expired_trial") else "0"
        return option


class SchoolRecipientChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, school: School) -> str:
        count = getattr(school, "mail_recipient_count", 0)
        if count == 0:
            suffix = "لا يوجد بريد مدير نشط"
        elif count == 1:
            suffix = "بريد مدير واحد"
        else:
            suffix = f"{count} من مديري المدرسة"
        return f"{school.name} — {suffix}"


class ComposeMailForm(forms.Form):
    subscription_status = forms.ChoiceField(
        label="تصفية المدارس حسب الاشتراك",
        required=False,
        choices=[
            ("", "جميع المدارس"),
            ("active", "المدارس ذات الاشتراك النشط"),
            ("inactive", "المدارس التي ليس لديها اشتراك نشط"),
            ("expired_trial", "المدارس التي توقفت بعد التجربة المجانية"),
        ],
        help_text="الاشتراك النشط يشمل التجربة المجانية السارية. غير النشط يشمل المنتهي والملغى ومن دون اشتراك. اختر الفئة ثم اضغط «تحديد الظاهر» أو حدد مدارس بعينها. تغيير الفئة يلغي تحديد المدارس خارجها؛ العناوين الإضافية لا تتأثر.",
    )
    schools = SchoolRecipientChoiceField(
        label="المدارس المستلمة",
        queryset=School.objects.none(),
        required=False,
        help_text="اختر مدرسة أو عدة مدارس؛ ستُرسل نسخة مستقلة إلى بريد كل مدير نشط.",
        widget=SchoolRecipientWidget(),
    )
    recipients = forms.CharField(
        label="عناوين إضافية (اختياري)",
        max_length=2000,
        required=False,
        help_text="يمكن إضافة بريد يدوي عند الحاجة، والفصل بين العناوين بفاصلة.",
        widget=forms.TextInput(attrs={"placeholder": "customer@example.com", "dir": "ltr"}),
    )
    subject = forms.CharField(label="الموضوع", max_length=998)
    body = forms.CharField(
        label="نص الرسالة",
        max_length=20000,
        widget=forms.Textarea(attrs={"rows": 12}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        subscriptions = SchoolSubscription.objects.filter(school_id=OuterRef("pk"))
        self.fields["schools"].queryset = School.objects.annotate(
            mail_has_active_subscription=Exists(
                subscriptions.filter(status="active", starts_at__lte=today).filter(
                    Q(ends_at__isnull=True) | Q(ends_at__gte=today)
                )
            ),
            mail_has_ended_trial=Exists(
                subscriptions.filter(plan__price=0, starts_at__lte=today).filter(
                    Q(status__in=["expired", "cancelled"]) | Q(ends_at__lt=today)
                )
            ),
            mail_has_paid_subscription=Exists(
                subscriptions.filter(
                    plan__price__gt=0, starts_at__lte=today,
                    status__in=["active", "expired", "cancelled"],
                )
            ),
            mail_recipient_count=Count(
                "users",
                filter=Q(
                    users__user__is_active=True,
                    users__user__is_staff=False,
                    users__user__is_superuser=False,
                )
                & ~Q(users__user__email=""),
                distinct=True,
            )
        ).order_by("name", "id")

    def clean_recipients(self) -> list[str]:
        raw = self.cleaned_data.get("recipients", "")
        values = [item.strip().casefold() for item in re.split(r"[,;\n]+", raw) if item.strip()]
        unique: list[str] = []
        for value in values:
            try:
                validate_email(value)
            except forms.ValidationError as exc:
                raise forms.ValidationError(f"عنوان البريد غير صالح: {value}") from exc
            if value not in unique:
                unique.append(value)
        return unique

    def clean(self):
        cleaned_data = super().clean()
        if any(field not in cleaned_data for field in ("schools", "recipients", "subscription_status")):
            return cleaned_data

        schools = list(cleaned_data["schools"])
        if any(not school_matches_subscription_filter(school, cleaned_data["subscription_status"]) for school in schools):
            self.add_error("schools", "بعض المدارس المحددة لا تطابق فئة الاشتراك الحالية. راجع التحديد ثم أعد الإرسال.")
            return cleaned_data
        recipients = list(cleaned_data["recipients"])
        emails_by_school: dict[int, list[str]] = {school.pk: [] for school in schools}
        if schools:
            rows = (
                UserProfile.objects.filter(
                    schools__in=schools,
                    user__is_active=True,
                    user__is_staff=False,
                    user__is_superuser=False,
                )
                .exclude(user__email="")
                .values_list("schools__id", "user__email")
            )
            for school_id, raw_email in rows:
                email = (raw_email or "").strip().casefold()
                try:
                    validate_email(email)
                except forms.ValidationError:
                    continue
                if email not in emails_by_school[school_id]:
                    emails_by_school[school_id].append(email)
                if email not in recipients:
                    recipients.append(email)

        empty_schools = [school.name for school in schools if not emails_by_school[school.pk]]
        if empty_schools:
            self.add_error(
                "schools",
                "لا يوجد بريد صالح لمدير نشط في: " + "، ".join(empty_schools),
            )
        if not schools and not recipients:
            raise forms.ValidationError("اختر مدرسة واحدة على الأقل أو أدخل عنوان بريد إضافيًا.")
        if len(recipients) > MAX_RECIPIENTS:
            raise forms.ValidationError(f"الحد الأقصى {MAX_RECIPIENTS} مستلم في عملية الإرسال الواحدة.")

        cleaned_data["recipients"] = recipients
        return cleaned_data
