from django import forms

from core.display_themes import COLOR_FIELDS
from schedule.models import DisplayTheme, SchoolOccasionTheme, SchoolSettings


class DisplayThemeForm(forms.ModelForm):
    class Meta:
        model = DisplayTheme
        fields = ["name", "base_theme", *COLOR_FIELDS, "font", "font_scale", "card_radius", "background_style"]
        widgets = {
            **{field: forms.TextInput(attrs={"type": "color", "data-theme-color": field}) for field in COLOR_FIELDS},
            "font_scale": forms.NumberInput(attrs={"min": 90, "max": 115, "step": 5}),
            "card_radius": forms.NumberInput(attrs={"min": 0, "max": 32, "step": 2}),
        }

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.school = school
        self.fields["base_theme"].choices = SchoolSettings.THEME_CHOICES[:7]
        self.fields["name"].widget.attrs.update({"placeholder": "مثل: هوية مدرستنا", "maxlength": 70})

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if DisplayTheme.objects.filter(school=self.instance.school, name=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("يوجد ثيم بهذا الاسم. اختر اسمًا آخر.")
        return name


class SchoolOccasionThemeForm(forms.ModelForm):
    class Meta:
        model = SchoolOccasionTheme
        fields = ("name", "tagline", "deep", "accent", "highlight", "soft", "pattern")
        widgets = {key: forms.TextInput(attrs={"type": "color"}) for key in ("deep", "accent", "highlight", "soft")}

    def clean_name(self):
        return self.cleaned_data["name"].strip()
