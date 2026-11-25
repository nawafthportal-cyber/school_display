from uuid import uuid4

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from core.display_themes import effective_custom_theme, theme_payload
from core.models import DisplayScreen
from schedule.models import DisplayTheme, SchoolSettings
from subscriptions.features import PLAN_FEATURES, school_disabled_features
from .access import get_active_school_or_redirect
from .decorators import manager_required
from .theme_forms import DisplayThemeForm
from .forms import SchoolSettingsForm
from .views_screens import _refresh_school_displays


@never_cache
@manager_required
def occasion_theme_edit(request, key=None):
    from core import occasions
    from core.occasion_customizations import SCHOOL_EVENT, SCHOOL_EVENT_KEY, customization_payload, resolve_occasion
    from core.models import School
    from schedule.models import SchoolOccasionTheme
    from .theme_forms import SchoolOccasionThemeForm

    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    screen_id = request.GET.get("screen", "")
    selected_screen = None
    if screen_id.isascii() and screen_id.isdigit() and len(screen_id) <= 18:
        selected_screen = DisplayScreen.objects.filter(school=school, pk=screen_id).first()
    target_query = f"&screen={selected_screen.pk}" if selected_screen else ""
    creating = key is None
    key = key or f"event_{uuid4().hex[:16]}"
    instance = SchoolOccasionTheme.objects.filter(school=school, occasion_key=key).first()
    school_event = bool(SCHOOL_EVENT_KEY.fullmatch(key))
    original = occasions.get(key)
    if creating:
        original = SCHOOL_EVENT
    elif original is None:
        payload = customization_payload(instance) if instance else None
        if not school_event or not payload:
            raise Http404
        original = resolve_occasion(key, {key: payload})
    is_custom = instance is not None
    if request.method == "POST" and request.POST.get("action") == "reset":
        if school_event:
            raise Http404
        with transaction.atomic():
            SchoolOccasionTheme.objects.filter(school=school, occasion_key=original.key).delete()
            transaction.on_commit(lambda: _refresh_school_displays(school))
        messages.success(request, "تمت استعادة اسم ثيم المناسبة وتصميمه الافتراضيين لهذه المدرسة.")
        return redirect("dashboard:occasion_theme_edit", key=original.key)
    if instance is None:
        instance = SchoolOccasionTheme(
            school=school, occasion_key=key, name="" if creating else original.theme_name, tagline=original.tagline,
            deep=original.deep, accent=original.accent, highlight=original.highlight, soft=original.soft,
            pattern="dots" if creating else "original",
        )
    form = SchoolOccasionThemeForm(request.POST if request.method == "POST" else None, instance=instance)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            School.objects.select_for_update().get(pk=school.pk)
            # Concurrent first saves still update one school/occasion row.
            existing = SchoolOccasionTheme.objects.filter(school=school, occasion_key=key).first()
            if existing is not None:
                form.instance.pk = existing.pk
            name_taken = school_event and (
                any(item.name == form.cleaned_data["name"] for item in occasions.all_occasions())
                or SchoolOccasionTheme.objects.filter(school=school, name=form.cleaned_data["name"]).exclude(occasion_key=key).exists()
            )
            if name_taken:
                form.add_error("name", "توجد مناسبة بهذا الاسم. اختر اسمًا يميّز تصميمك.")
            else:
                form.save()
                if not creating:
                    transaction.on_commit(lambda: _refresh_school_displays(school))
        if not form.errors and creating:
            messages.success(request, "حُفظ تصميم المناسبة. اختر الشاشة التي تريد عرضها عليها أو جدْوِل موعدها.")
            return redirect(reverse("dashboard:occasion_templates") + f"?occasion={key}{target_query}#occasion-assignment")
        if not form.errors:
            messages.success(request, "تم حفظ اسم الثيم وتصميمه وتحديث شاشات المدرسة التي تستخدم هذه المناسبة.")
            return redirect(request.get_full_path())
    return render(request, "dashboard/occasion_theme_edit.html", {
        "form": form, "occasion": original, "school": school, "is_custom": is_custom and not school_event,
        "creating": creating, "school_event": school_event, "selected_screen": selected_screen,
        "original_pattern": SCHOOL_EVENT.pattern_css if school_event else original.pattern_css,
        "occasion_data": {**original.as_card_dict(), **(customization_payload(instance) or {})},
    })


@manager_required
@require_POST
def occasion_assign(request):
    from core.occasion_customizations import choices_for_school

    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    screen_id = request.POST.get("screen", "")
    key = request.POST.get("occasion", "")
    if not screen_id.isascii() or not screen_id.isdigit() or len(screen_id) > 18:
        raise Http404
    labels = dict(choices_for_school(school.pk, DisplayScreen.OCCASION_THEME_CHOICES))
    if key not in labels:
        raise Http404
    with transaction.atomic():
        screen = get_object_or_404(DisplayScreen.objects.select_for_update(), school=school, pk=screen_id)
        screen.occasion_theme = key
        screen.save(update_fields=["occasion_theme"])
        transaction.on_commit(lambda: _refresh_school_displays(school, target_screen=screen))
    messages.success(request, f"حُفظ إعداد «{labels[key]}» لشاشة «{screen.name}».")
    return redirect(reverse("dashboard:occasion_templates") + f"?screen={screen.pk}#occasion-assignment")


@never_cache
@manager_required
def theme_list(request):
    school, response = get_active_school_or_redirect(request)
    if response:
        return response
    themes = list(DisplayTheme.objects.filter(school=school))
    customization_enabled = "screen_customization" not in school_disabled_features(school.pk)
    screens = list(DisplayScreen.objects.filter(school=school).select_related("custom_theme_override").order_by("name")) if customization_enabled else []
    settings = SchoolSettings.objects.filter(school=school).select_related("custom_theme").first()
    by_theme = {theme.pk: [] for theme in themes}
    for screen in screens:
        effective = effective_custom_theme(settings, screen)
        if effective and effective.pk in by_theme:
            by_theme[effective.pk].append(screen)
    for theme in themes:
        theme.assigned_screens = by_theme[theme.pk]
    return render(request, "dashboard/theme_list.html", {
        "themes": themes, "theme_data": [theme_payload(theme) for theme in themes],
        "selected_screen": next((screen for screen in screens if str(screen.pk) == request.GET.get("screen")), None),
        "screens": screens, "default_theme_id": getattr(settings, "custom_theme_id", None),
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
        customization_enabled = "screen_customization" not in school_disabled_features(school.pk)
        if target not in ("school", "all") and not customization_enabled:
            messages.warning(request, f"خاصية «{PLAN_FEATURES['screen_customization']}» غير مفعّلة في اشتراك المدرسة.")
            return redirect("dashboard:my_subscription")
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
                if customization_enabled:
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
