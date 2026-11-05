from django.contrib import messages
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from core.display_themes import theme_payload
from core.models import DisplayScreen
from schedule.models import DisplayTheme, SchoolSettings
from .access import get_active_school_or_redirect
from .decorators import manager_required
from .theme_forms import DisplayThemeForm
from .forms import SchoolSettingsForm
from .views_screens import _refresh_school_displays


@never_cache
@manager_required
def theme_list(request):
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    themes = list(DisplayTheme.objects.filter(school=school))
    return render(request, "dashboard/theme_list.html", {
        "themes": themes, "theme_data": [theme_payload(theme) for theme in themes],
        "screens": DisplayScreen.objects.filter(school=school).order_by("name"),
        "school": school,
    })


@never_cache
@manager_required
def theme_edit(request, pk=None):
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    theme = get_object_or_404(DisplayTheme, school=school, pk=pk) if pk else DisplayTheme(school=school)
    form = DisplayThemeForm(request.POST if request.method == "POST" else None, instance=theme, school=school)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                form.save()
                if pk:
                    transaction.on_commit(lambda: _refresh_school_displays(school))
        except IntegrityError:
            form.add_error("name", "تعذر الحفظ بهذا الاسم. اختر اسمًا آخر.")
        else:
            messages.success(request, "تم حفظ الثيم. يمكنك تطبيقه من مكتبة الثيمات أو إعدادات الشاشة.")
            return redirect("dashboard:theme_list")
    return render(request, "dashboard/theme_edit.html", {
        "form": form, "theme": theme, "school": school,
        "theme_data": theme_payload(theme), "is_edit": bool(pk),
    })


@manager_required
@require_POST
def theme_action(request, pk, action):
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    theme = get_object_or_404(DisplayTheme, school=school, pk=pk)
    if action == "apply":
        target = request.POST.get("target", "school")
        with transaction.atomic():
            if target == "school":
                obj, _ = SchoolSettings.objects.get_or_create(school=school, defaults={"name": school.name})
                obj.custom_theme = theme
                obj.theme = theme.base_theme
                obj.display_accent_color = theme.accent_color
                obj.save(update_fields=("custom_theme", "theme", "display_accent_color"))
                transaction.on_commit(lambda: _refresh_school_displays(school))
            elif target == "all":
                obj, _ = SchoolSettings.objects.get_or_create(school=school, defaults={"name": school.name})
                obj.custom_theme = theme
                obj.theme = theme.base_theme
                obj.display_accent_color = theme.accent_color
                obj.save(update_fields=("custom_theme", "theme", "display_accent_color"))
                DisplayScreen.objects.filter(school=school).update(
                    custom_theme_override=None, theme_override="", display_accent_color_override="",
                )
                transaction.on_commit(lambda: _refresh_school_displays(school))
            else:
                if not target.isdecimal():
                    from django.http import Http404
                    raise Http404
                screen = get_object_or_404(DisplayScreen, school=school, pk=int(target))
                screen.custom_theme_override = theme
                screen.theme_override = theme.base_theme
                screen.display_accent_color_override = theme.accent_color
                screen.save(update_fields=("custom_theme_override", "theme_override", "display_accent_color_override"))
                transaction.on_commit(lambda: _refresh_school_displays(school, target_screen=screen))
        messages.success(request, "تم تطبيق الثيم وإرسال تحديث إلى الشاشات المعنية.")
    elif action == "duplicate":
        with transaction.atomic():
            # Serialize copies per school so concurrent clicks cannot share a name.
            from core.models import School
            School.objects.select_for_update().get(pk=school.pk)
            base = theme.name[:54]
            index = 1
            name = f"{base} — نسخة {index}"
            while DisplayTheme.objects.filter(school=school, name=name).exists():
                index += 1
                name = f"{base} — نسخة {index}"
            theme.pk = None
            theme.name = name
            theme.save()
        messages.success(request, "تم نسخ الثيم. يمكنك تعديل النسخة الجديدة.")
        return redirect("dashboard:theme_edit", pk=theme.pk)
    elif action == "delete":
        with transaction.atomic():
            fallback = SchoolSettingsForm.THEME_ACCENTS.get(theme.base_theme, "#6366F1")
            SchoolSettings.objects.filter(school=school, custom_theme=theme).update(display_accent_color=fallback)
            DisplayScreen.objects.filter(school=school, custom_theme_override=theme).update(display_accent_color_override=fallback)
            theme.delete()
            transaction.on_commit(lambda: _refresh_school_displays(school))
        messages.success(request, "تم حذف الثيم. الشاشات التي كانت تستخدمه عادت إلى ألوان الثيم الجاهز.")
    else:
        from django.http import Http404
        raise Http404
    return redirect("dashboard:theme_list")
