"""The weekly timetable: school days, periods, lessons and CSV export."""

from __future__ import annotations

from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.http import Http404

from .view_helpers import *  # noqa: F401,F403  (shared view layer)


@manager_required
def days_list(request):
    SchoolSettings = SchoolSettingsModel()
    DaySchedule = DayScheduleModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if not settings_obj:
        messages.warning(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:settings")

    existing = set(
        DaySchedule.objects.filter(settings=settings_obj, weekday__in=SCHOOL_WEEKDAY_IDS)
        .values_list("weekday", flat=True)
    )

    for w in SCHOOL_WEEKDAY_IDS:
        if w not in existing:
            DaySchedule.objects.create(
                settings=settings_obj,
                weekday=w,
                periods_count=7 if w in (7, 1) else 6,
                is_active=(w not in WEEKEND_WEEKDAY_IDS),
            )

    days = list(
        DaySchedule.objects.filter(settings=settings_obj, weekday__in=SCHOOL_WEEKDAY_IDS)
        .prefetch_related("periods", "breaks")
    )
    days.sort(key=lambda d: WEEKDAY_SORT.get(d.weekday, 99))

    total_periods = 0
    active_days_count = 0
    holiday_days_count = 0
    for d in days:
        d.day_name = WEEKDAY_MAP.get(d.weekday, str(d.weekday))
        d.breaks_count = d.breaks.count()
        periods = sorted(d.periods.all(), key=lambda p: p.starts_at)
        # A day switched on but with no period times is the single most common
        # reason a board shows nothing. The template flags it, so the count has
        # to be the periods that actually exist — not the declared target.
        d.periods_actual = len(periods)
        d.needs_times = bool(d.is_active) and not periods
        if periods:
            d.first_period_time = periods[0].starts_at.strftime("%H:%M")
            d.last_period_time = periods[-1].ends_at.strftime("%H:%M")
            start_dt = datetime.combine(date.today(), periods[0].starts_at)
            end_dt = datetime.combine(date.today(), periods[-1].ends_at)
            diff = end_dt - start_dt
            hours, remainder = divmod(diff.seconds, 3600)
            minutes = remainder // 60
            d.total_duration = f"{hours:02d}:{minutes:02d}"
        else:
            d.first_period_time = "--:--"
            d.last_period_time = "--:--"
            d.total_duration = "--:--"
        if d.is_active:
            active_days_count += 1
            # Count the periods that exist, not the declared target: the cards
            # show the same number, and the screen only ever plays real periods.
            total_periods += d.periods_actual
        else:
            holiday_days_count += 1

    active_days = [d for d in days if d.is_active]
    avg_periods = total_periods / active_days_count if active_days_count else 0
    max_periods_day = max(active_days, key=lambda d: d.periods_count) if active_days else None
    min_periods_day = min(active_days, key=lambda d: d.periods_count) if active_days else None
    last_updated = timezone.localtime().strftime("%H:%M")
    avg_periods_display = f"{avg_periods:.1f}"

    return render(
        request,
        "dashboard/days_list.html",
        {
            "days": days,
            "total_periods": total_periods,
            "avg_periods": avg_periods,
            "avg_periods_display": avg_periods_display,
            "active_days_count": active_days_count,
            "holiday_days_count": holiday_days_count,
            "max_periods_day": max_periods_day,
            "min_periods_day": min_periods_day,
            "last_updated": last_updated,
        },
    )

@manager_required
@transaction.atomic
def day_edit(request, weekday: int):
    # Backward compatibility: older dashboards used Sunday=0
    if weekday == 0:
        weekday = 7
    if weekday not in SCHOOL_WEEKDAY_IDS:
        messages.error(request, "رقم اليوم غير صالح.")
        return redirect("dashboard:days_list")

    SchoolSettings = SchoolSettingsModel()
    DaySchedule = DayScheduleModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if not settings_obj:
        messages.warning(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:settings")

    day = get_object_or_404(DaySchedule, settings=settings_obj, weekday=weekday)
    day.day_name = WEEKDAY_MAP[weekday]

    if request.method == "POST":
        form = DayScheduleForm(request.POST, instance=day)
        p_formset = PeriodFormSet(request.POST, instance=day, prefix="p")
        b_formset = BreakFormSet(request.POST, instance=day, prefix="b")

        if form.is_valid() and p_formset.is_valid() and b_formset.is_valid():
            form.save()
            p_formset.save()
            b_formset.save()
            
            # ✅ Invalidate display cache + WS broadcast
            _invalidate_display_cache(school)
            
            messages.success(request, "تم حفظ جدول اليوم بنجاح.")
            return redirect("dashboard:days_list")

        detail = _collect_form_errors(form, p_formset, b_formset)
        if not detail:
            detail = "تحقق من الأوقات: يوجد حقول ناقصة/مكررة أو تداخلات زمنية."
        messages.error(request, detail)
    else:
        form = DayScheduleForm(instance=day)
        p_formset = PeriodFormSet(instance=day, prefix="p")
        b_formset = BreakFormSet(instance=day, prefix="b")

    return render(
        request,
        "dashboard/day_edit.html",
        {
            "day": day,
            "form": form,
            "p_formset": p_formset,
            "b_formset": b_formset,
            "autofill_seed": _build_day_autofill_seed(day),
        },
    )

@manager_required
@transaction.atomic
@require_POST
def day_autofill(request, weekday: int):
    if weekday not in SCHOOL_WEEKDAY_IDS:
        messages.error(request, "رقم اليوم غير صالح.")
        return redirect("dashboard:days_list")

    SchoolSettings = SchoolSettingsModel()
    DaySchedule = DayScheduleModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if not settings_obj:
        messages.error(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:settings")

    day = get_object_or_404(DaySchedule, settings=settings_obj, weekday=weekday)

    try:
        def _required_int_param(name: str, label: str, *, min_value: int) -> int:
            raw = (request.POST.get(name) or "").strip()
            if raw == "":
                raise ValueError(f"حقل '{label}' مطلوب.")
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"قيمة '{label}' غير صالحة.")
            if value < min_value:
                raise ValueError(f"قيمة '{label}' يجب أن تكون {min_value} أو أكثر.")
            return value

        start_time_str = (request.POST.get("start_time") or "").strip()
        if not start_time_str:
            raise ValueError("حقل 'بداية اليوم' مطلوب.")

        new_count = _required_int_param("target_periods_count", "عدد الحصص المطلوب", min_value=1)
        period_minutes = _required_int_param("period_minutes", "زمن الحصة", min_value=1)
        gap_minutes = _required_int_param("gap_minutes", "فاصل بين الحصص", min_value=0)
        break_after = _required_int_param("break_after", "الفسحة بعد حصة رقم", min_value=0)
        break_minutes = _required_int_param("break_minutes", "مدة الفسحة", min_value=0)

        if new_count > 20:
            raise ValueError("عدد الحصص يجب ألا يتجاوز 20 حصة.")
        if break_minutes > 240:
            raise ValueError("مدة الفسحة يجب ألا تتجاوز 240 دقيقة.")

        period_seconds = 0
        gap_seconds = 0
        break_seconds = 0

        start_t = _parse_hhmm_or_hhmmss(start_time_str)

        p_len = timedelta(minutes=period_minutes, seconds=period_seconds)
        gap = timedelta(minutes=gap_minutes, seconds=gap_seconds)
        brk = timedelta(minutes=break_minutes, seconds=break_seconds)

        if p_len.total_seconds() <= 0:
            raise ValueError("طول الحصة يجب أن يكون أكبر من صفر.")

        if break_after > new_count:
            raise ValueError("قيمة 'الفسحة بعد الحصة رقم' خارج النطاق.")

        periods_mgr = _rev_manager(day, "periods", "period_set")
        breaks_mgr = _rev_manager(day, "breaks", "break_set")
        if periods_mgr is None or breaks_mgr is None:
            raise ValueError("تعذر الوصول لعلاقات الحصص/الفسح (تحقق من related_name).")

        base_date = timezone.localdate()
        cursor = datetime.combine(base_date, start_t)
        total_span = p_len * new_count + gap * (new_count - 1)
        if break_minutes and break_after:
            total_span += brk
        if (cursor + total_span).date() != base_date:
            raise ValueError("يجب أن تنتهي الحصص والفسح قبل منتصف الليل في اليوم نفسه.")

        day.periods_count = new_count
        day.save(update_fields=["periods_count"])
        periods_mgr.all().delete()
        breaks_mgr.all().delete()

        break_minutes_final = (
            int(math.ceil(max(0, brk.total_seconds()) / 60.0))
            if brk.total_seconds() > 0
            else 0
        )

        for i in range(1, day.periods_count + 1):
            start_period = cursor
            end_period = cursor + p_len
            periods_mgr.create(index=i, starts_at=start_period.time(), ends_at=end_period.time())
            cursor = end_period

            if break_minutes_final > 0 and break_after == i:
                breaks_mgr.create(label="فسحة", starts_at=cursor.time(), duration_min=break_minutes_final)
                cursor += timedelta(minutes=break_minutes_final)

            cursor += gap

        # ✅ Invalidate display cache + WS broadcast
        _invalidate_display_cache(school)

        messages.success(request, "تمت التعبئة التلقائية للجدول.")
        return redirect("dashboard:day_edit", weekday=weekday)

    except Exception as e:
        # This exception is handled inside the atomic view: explicitly roll back
        # the count, deletions and any rows created before the failure.
        transaction.set_rollback(True)
        logger.exception("day_autofill failed")
        messages.error(request, f"تعذّر تنفيذ التعبئة: {e}")
        return redirect("dashboard:day_edit", weekday=weekday)

@manager_required
@require_POST
def day_toggle(request, weekday: int):
    if weekday not in SCHOOL_WEEKDAY_IDS:
        messages.error(request, "اليوم غير صالح.")
        return redirect("dashboard:days_list")

    SchoolSettings = SchoolSettingsModel()
    DaySchedule = DayScheduleModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if not settings_obj:
        messages.error(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:settings")

    day, _ = DaySchedule.objects.get_or_create(settings=settings_obj, weekday=weekday)
    day.is_active = not bool(getattr(day, "is_active", True))
    day.save(update_fields=["is_active"])

    # ✅ Invalidate display cache + WS broadcast
    _invalidate_display_cache(school)

    status = "تفعيل" if day.is_active else "تعطيل"
    messages.success(request, f"تم {status} يوم {WEEKDAY_MAP.get(weekday, str(weekday))}.")
    return redirect("dashboard:days_list")

@manager_required
@transaction.atomic
@require_POST
def day_clear(request, weekday: int):
    if weekday not in SCHOOL_WEEKDAY_IDS:
        messages.error(request, "اليوم خارج أيام الأسبوع الدراسية.")
        return redirect("dashboard:days_list")

    SchoolSettings = SchoolSettingsModel()
    DaySchedule = DayScheduleModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if not settings_obj:
        messages.warning(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:settings")

    day = get_object_or_404(DaySchedule, settings=settings_obj, weekday=weekday)

    periods_mgr = _rev_manager(day, "periods", "period_set")
    breaks_mgr = _rev_manager(day, "breaks", "break_set")

    if periods_mgr is not None:
        periods_mgr.all().delete()
    if breaks_mgr is not None:
        breaks_mgr.all().delete()

    messages.success(request, "تم مسح جميع الحصص والفسح لهذا اليوم.")
    return redirect("dashboard:day_edit", weekday=weekday)

@manager_required
@transaction.atomic
@require_POST
def day_reindex(request, weekday: int):
    if weekday not in SCHOOL_WEEKDAY_IDS:
        messages.error(request, "اليوم خارج أيام الأسبوع الدراسية.")
        return redirect("dashboard:days_list")

    SchoolSettings = SchoolSettingsModel()
    DaySchedule = DayScheduleModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if not settings_obj:
        messages.warning(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:settings")

    day = get_object_or_404(DaySchedule, settings=settings_obj, weekday=weekday)

    periods_mgr = _rev_manager(day, "periods", "period_set")
    if periods_mgr is None:
        messages.error(request, "تعذر الوصول للحصص (تحقق من related_name).")
        return redirect("dashboard:day_edit", weekday=weekday)

    periods = list(periods_mgr.all())
    periods.sort(key=lambda p: (p.starts_at or time.min, p.ends_at or time.min))

    changed = [(p, i) for i, p in enumerate(periods, start=1) if p.index != i]
    occupied = {p.index for p in periods} | set(range(1, len(periods) + 1))
    available = (value for value in range(1, 32768) if value not in occupied)
    # Move changed rows out of the way before assigning their final numbers;
    # saving a 1 <-> 2 swap directly violates unique(day, index).
    for p, _ in changed:
        periods_mgr.filter(pk=p.pk).update(index=next(available))
    for i, p in enumerate(periods, start=1):
        if p.index != i:
            p.index = i
            p.save(update_fields=["index"])

    messages.success(request, "تمت إعادة ترقيم الحصص حسب الترتيب الزمني (١..ن).")
    return redirect("dashboard:day_edit", weekday=weekday)

@manager_required
def lessons_list(request):
    SchoolSettings = SchoolSettingsModel()
    ClassLesson = ClassLessonModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    lessons = ClassLesson.objects.none()
    classes = _classes_qs_from_settings(settings_obj).order_by("name") if settings_obj else []
    if settings_obj:
        lessons = (
            ClassLesson.objects.filter(settings=settings_obj)
            .select_related("school_class", "subject", "teacher")
            .order_by("weekday", "period_index", "school_class__name", "pk")
        )

    search = (request.GET.get("search") or "").strip()
    day = (request.GET.get("day") or "").strip()
    class_id = (request.GET.get("class_id") or "").strip()

    if search:
        lessons = lessons.filter(
            Q(school_class__name__icontains=search)
            | Q(subject__name__icontains=search)
            | Q(teacher__name__icontains=search)
        )

    if day in {str(value) for value, _ in ClassLesson._meta.get_field("weekday").choices}:
        lessons = lessons.filter(weekday=int(day))
    if class_id.isascii() and class_id.isdecimal() and len(class_id) <= 19:
        lessons = lessons.filter(school_class_id=int(class_id))

    page = Paginator(lessons, 25).get_page(request.GET.get("page"))
    filters = request.GET.copy()
    filters.pop("page", None)

    return render(
        request,
        "dashboard/lessons_list.html",
        {
            "lessons": page.object_list,
            "page": page,
            "page_range": page.paginator.get_elided_page_range(page.number),
            "page_query": filters.urlencode(),
            "classes": classes,
            "selected_class_id": class_id,
            "search": search,
            "selected_day": day,
            "weekday_choices": ClassLesson._meta.get_field("weekday").choices,
        },
    )

def _lesson_list_url(request):
    list_url = reverse("dashboard:lessons_list")
    candidate = _safe_next_url(request, default_name="dashboard:lessons_list")
    parsed = urlsplit(candidate)
    if not parsed.netloc and not parsed.scheme and parsed.path == list_url:
        return candidate
    return list_url

@manager_required
def lesson_create(request):
    SchoolSettings = SchoolSettingsModel()
    Subject = SubjectModel()
    Teacher = TeacherModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if not settings_obj:
        messages.error(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:lessons_list")

    list_url = _lesson_list_url(request)
    form = LessonForm(request.POST or None)

    classes_qs = _classes_qs_from_settings(settings_obj).order_by("name")
    form.fields["school_class"].queryset = classes_qs
    form.fields["subject"].queryset = Subject.objects.filter(school=school).order_by("name")
    form.fields["teacher"].queryset = Teacher.objects.filter(school=school).order_by("name")

    if request.method == "POST":
        if form.is_valid():
            lesson = form.save(commit=False)
            lesson.settings = settings_obj
            lesson.save()
            _invalidate_display_cache(school)
            messages.success(request, "تمت إضافة الحصة بنجاح.")
            return redirect(list_url)
        messages.error(request, "الرجاء تصحيح الأخطاء.")

    return render(request, "dashboard/lesson_form.html", {"form": form, "title": "إضافة حصة", "list_url": list_url})

@manager_required
def lesson_edit(request, pk: int):
    SchoolSettings = SchoolSettingsModel()
    ClassLesson = ClassLessonModel()
    Subject = SubjectModel()
    Teacher = TeacherModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if settings_obj is None:
        messages.error(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:lessons_list")

    obj = get_object_or_404(ClassLesson, pk=pk, settings=settings_obj)

    list_url = _lesson_list_url(request)
    form = LessonForm(request.POST or None, instance=obj)

    classes_qs = _classes_qs_from_settings(settings_obj).order_by("name")
    form.fields["school_class"].queryset = classes_qs
    form.fields["subject"].queryset = Subject.objects.filter(school=school).order_by("name")
    form.fields["teacher"].queryset = Teacher.objects.filter(school=school).order_by("name")

    if request.method == "POST":
        if form.is_valid():
            form.save()
            _invalidate_display_cache(school)
            messages.success(request, "تم تعديل الحصة بنجاح.")
            return redirect(list_url)
        messages.error(request, "الرجاء تصحيح الأخطاء.")

    return render(request, "dashboard/lesson_form.html", {"form": form, "title": "تعديل حصة", "list_url": list_url})

@manager_required
@require_POST
def lesson_delete(request, pk: int):
    SchoolSettings = SchoolSettingsModel()
    ClassLesson = ClassLessonModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if settings_obj is None:
        messages.error(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:lessons_list")

    obj = get_object_or_404(ClassLesson, pk=pk, settings=settings_obj)
    obj.delete()
    _invalidate_display_cache(school)
    messages.success(request, "تم حذف الحصة.")
    return redirect(_lesson_list_url(request))

@manager_required
def timetable_day_view(request):
    SchoolSettings = SchoolSettingsModel()
    Period = PeriodModel()
    Subject = SubjectModel()
    Teacher = TeacherModel()
    ClassLesson = ClassLessonModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if settings_obj is None:
        messages.error(request, "يجب أولاً إعداد إعدادات المدرسة والجدول الزمني.")
        return redirect("dashboard:settings")

    weekdays_choices = list(SCHOOL_WEEK)
    try:
        school_timezone = ZoneInfo(settings_obj.timezone_name or "Asia/Riyadh")
    except (ValueError, ZoneInfoNotFoundError):
        school_timezone = ZoneInfo("Asia/Riyadh")
    today = timezone.localdate(timezone=school_timezone)
    default_weekday = today.isoweekday()
    selection_key = f"timetable_day_selection_{school.pk}"
    selection = request.session.get(selection_key, {})

    weekday_param = request.GET.get("weekday") if request.method == "GET" else request.POST.get("weekday")
    class_param = request.GET.get("class_id") if request.method == "GET" else request.POST.get("class_id")
    if request.method == "GET":
        if weekday_param is None and selection.get("date") == today.isoformat():
            weekday_param = selection.get("weekday")
        if class_param is None:
            class_param = selection.get("class_id")
    if request.method == "POST" and weekday_param in (None, ""):
        return JsonResponse({"ok": False, "message": "حدد اليوم المطلوب حفظ جدوله."}, status=400)

    try:
        weekday = int(weekday_param) if weekday_param not in (None, "") else int(default_weekday)
    except (TypeError, ValueError):
        if request.method == "POST":
            return JsonResponse({"ok": False, "message": "اليوم المحدد غير صالح."}, status=400)
        weekday = int(default_weekday)
    if weekday not in SCHOOL_WEEKDAY_IDS:
        if request.method == "POST":
            return JsonResponse({"ok": False, "message": "اليوم المحدد غير صالح."}, status=400)
        weekday = default_weekday

    classes_qs = _classes_qs_from_settings(settings_obj).order_by("name")

    selected_class = None
    if request.method == "POST" and not class_param:
        return JsonResponse({"ok": False, "message": "حدد الفصل المطلوب حفظ جدوله."}, status=400)
    if classes_qs.exists():
        try:
            if class_param not in (None, ""):
                selected_class = classes_qs.get(pk=int(class_param))
            else:
                selected_class = classes_qs.first()
        except (ValueError, classes_qs.model.DoesNotExist):
            if request.method == "POST":
                raise Http404("الفصل المحدد غير موجود في المدرسة النشطة.")
            selected_class = classes_qs.first()
    if request.method == "POST" and selected_class is None:
        raise Http404("الفصل المحدد غير موجود في المدرسة النشطة.")

    selected_class_id = selected_class.id if selected_class else None
    if request.method == "GET":
        updated_selection = {"date": today.isoformat(), "weekday": weekday, "class_id": selected_class_id}
        if updated_selection != selection:
            request.session[selection_key] = updated_selection

    periods_qs = Period.objects.filter(day__settings=settings_obj, day__weekday=weekday).order_by("index")
    subjects_qs = Subject.objects.filter(school=school).order_by("name")
    teachers_qs = Teacher.objects.filter(school=school).order_by("name")

    if selected_class is not None:
        existing_lessons_qs = ClassLesson.objects.filter(
            settings=settings_obj,
            weekday=weekday,
            school_class=selected_class,
        ).select_related("subject", "teacher")
    else:
        existing_lessons_qs = ClassLesson.objects.none()

    lessons_map: dict[int, object] = {l.period_index: l for l in existing_lessons_qs}

    if request.method == "POST" and selected_class is not None:
        created_count = 0
        updated_count = 0
        deleted_count = 0

        subjects_by_id = {s.id: s for s in subjects_qs}
        teachers_by_id = {t.id: t for t in teachers_qs}

        # Validate every submitted choice before changing any row. A foreign,
        # deleted or malformed identifier is not an instruction to clear a slot.
        errors = []
        for period in periods_qs:
            subject_field = f"subject-{selected_class.id}-{period.index}"
            teacher_field = f"teacher-{selected_class.id}-{period.index}"
            if subject_field not in request.POST and teacher_field not in request.POST:
                continue
            subject_raw = (request.POST.get(subject_field) or "").strip()
            teacher_raw = (request.POST.get(teacher_field) or "").strip()
            if subject_field not in request.POST or teacher_field not in request.POST or bool(subject_raw) != bool(teacher_raw):
                errors.append(f"الحصة {period.index}: اختر المادة والمعلم معًا، أو أفرغ الحقلين لحذف الحصة.")
            for kind, choices, label in (("subject", subjects_by_id, "المادة"), ("teacher", teachers_by_id, "المعلم/ـة")):
                raw = (request.POST.get(f"{kind}-{selected_class.id}-{period.index}") or "").strip()
                if raw and (len(raw) > 20 or not raw.isdecimal() or int(raw) not in choices):
                    errors.append(f"الحصة {period.index}: {label} المحدد غير موجود في هذه المدرسة.")
        if errors:
            message = " ".join(errors)
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "message": message, "errors": errors, "level": "error"}, status=400)
            messages.error(request, message)
            return redirect(f"{reverse('dashboard:timetable_day')}?weekday={weekday}&class_id={selected_class.id}")

        with transaction.atomic():
            for period in periods_qs:
                period_index = period.index
                existing_lesson = lessons_map.get(period_index)

                subject_field = f"subject-{selected_class.id}-{period_index}"
                teacher_field = f"teacher-{selected_class.id}-{period_index}"
                if subject_field not in request.POST and teacher_field not in request.POST:
                    continue

                subject_raw = (request.POST.get(subject_field) or "").strip()
                teacher_raw = (request.POST.get(teacher_field) or "").strip()

                subject_obj = None
                teacher_obj = None

                if subject_raw:
                    try:
                        subject_obj = subjects_by_id.get(int(subject_raw))
                    except (TypeError, ValueError):
                        subject_obj = None

                if teacher_raw:
                    try:
                        teacher_obj = teachers_by_id.get(int(teacher_raw))
                    except (TypeError, ValueError):
                        teacher_obj = None

                if subject_obj is None and teacher_obj is None:
                    if existing_lesson is not None:
                        existing_lesson.delete()
                        deleted_count += 1
                    continue

                if existing_lesson is not None:
                    if subject_obj is None:
                        subject_obj = existing_lesson.subject
                    if teacher_obj is None:
                        teacher_obj = existing_lesson.teacher

                if subject_obj is None or teacher_obj is None:
                    continue

                if existing_lesson is None:
                    ClassLesson.objects.create(
                        settings=settings_obj,
                        school_class=selected_class,
                        weekday=weekday,
                        period_index=period_index,
                        subject=subject_obj,
                        teacher=teacher_obj,
                        is_active=True,
                    )
                    created_count += 1
                else:
                    changed = False
                    if existing_lesson.subject_id != subject_obj.id:
                        existing_lesson.subject = subject_obj
                        changed = True
                    if existing_lesson.teacher_id != teacher_obj.id:
                        existing_lesson.teacher = teacher_obj
                        changed = True
                    if not existing_lesson.is_active:
                        existing_lesson.is_active = True
                        changed = True
                    if changed:
                        existing_lesson.save()
                        updated_count += 1

        if created_count or updated_count or deleted_count:
            msg_parts = []
            if created_count:
                msg_parts.append(f"تم إنشاء {created_count} حصة جديدة.")
            if updated_count:
                msg_parts.append(f"تم تحديث {updated_count} حصة.")
            if deleted_count:
                msg_parts.append(f"تم حذف {deleted_count} حصة فارغة.")
            message = " ".join(msg_parts)
            message_level = "success"
        else:
            message = "لم يتم رصد أي تغييرات في جدول هذا الفصل."
            message_level = "info"

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "ok": True,
                    "message": message,
                    "level": message_level,
                    "created": created_count,
                    "updated": updated_count,
                    "deleted": deleted_count,
                    "weekday": weekday,
                    "class_id": selected_class.id,
                }
            )

        if message_level == "success":
            messages.success(request, message)
        else:
            messages.info(request, message)

        url = reverse("dashboard:timetable_day")
        url = f"{url}?weekday={weekday}&class_id={selected_class.id}"
        return redirect(url)

    rows: list[dict] = []
    for period in periods_qs:
        lesson = lessons_map.get(period.index)
        rows.append(
            {
                "period": period,
                "subject_id": lesson.subject_id if lesson else None,
                "teacher_id": lesson.teacher_id if lesson else None,
            }
        )

    return render(
        request,
        "dashboard/timetable_day.html",
        {
            "school": school,
            "settings": settings_obj,
            "weekdays": weekdays_choices,
            "weekday": weekday,
            "classes": classes_qs,
            "selected_class": selected_class,
            "selected_class_id": selected_class_id,
            "periods": periods_qs,
            "rows": rows,
            "subjects": subjects_qs,
            "teachers": teachers_qs,
        },
    )

@manager_required
def timetable_week_view(request):
    SchoolSettings = SchoolSettingsModel()
    Period = PeriodModel()
    ClassLesson = ClassLessonModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if settings_obj is None:
        messages.error(request, "يجب أولاً إعداد إعدادات المدرسة والجدول الزمني.")
        return redirect("dashboard:settings")

    class_param = request.GET.get("class_id")
    classes_qs = _classes_qs_from_settings(settings_obj).order_by("name")

    if not classes_qs.exists():
        messages.error(request, "لا توجد فصول مسجلة لهذه المدرسة.")
        return redirect("dashboard:timetable_day")

    try:
        selected_class = classes_qs.get(pk=int(class_param)) if class_param else classes_qs.first()
    except (ValueError, classes_qs.model.DoesNotExist):
        selected_class = classes_qs.first()

    all_periods_qs = Period.objects.filter(
        day__settings=settings_obj, day__weekday__in=SCHOOL_WEEKDAY_IDS
    ).order_by("day__weekday", "index")

    periods_by_weekday: dict[int, list] = {}
    for p in all_periods_qs:
        periods_by_weekday.setdefault(p.day.weekday, []).append(p)

    all_lessons_qs = ClassLesson.objects.filter(
        settings=settings_obj,
        school_class=selected_class,
        weekday__in=SCHOOL_WEEKDAY_IDS,
    ).select_related("subject", "teacher")

    lessons_by_weekday_period: dict[int, dict[int, object]] = {}
    for lesson in all_lessons_qs:
        lessons_by_weekday_period.setdefault(lesson.weekday, {})[lesson.period_index] = lesson

    days_data = []
    for weekday, label in SCHOOL_WEEK:
        rows = []
        for period in periods_by_weekday.get(weekday, []):
            lesson = lessons_by_weekday_period.get(weekday, {}).get(period.index)
            rows.append(
                {
                    "period": period,
                    "lesson": lesson,
                    "subject_name": lesson.subject.name if lesson and lesson.subject else "",
                    "teacher_name": lesson.teacher.name if lesson and lesson.teacher else "",
                }
            )
        days_data.append({"weekday": weekday, "label": label, "rows": rows})

    return render(
        request,
        "dashboard/timetable_week.html",
        {"school": school, "settings": settings_obj, "selected_class": selected_class, "days_data": days_data},
    )

@manager_required
def timetable_export_csv(request):
    SchoolSettings = SchoolSettingsModel()
    Period = PeriodModel()
    ClassLesson = ClassLessonModel()

    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if settings_obj is None:
        messages.error(request, "يجب أولاً إعداد إعدادات المدرسة والجدول الزمني.")
        return redirect("dashboard:settings")

    class_param = request.GET.get("class_id")
    if not class_param:
        messages.error(request, "لم يتم تحديد الفصل المطلوب للتصدير.")
        return redirect("dashboard:timetable_day")

    classes_qs = _classes_qs_from_settings(settings_obj)
    try:
        school_class = classes_qs.get(pk=int(class_param))
    except (ValueError, classes_qs.model.DoesNotExist):
        messages.error(request, "الفصل المحدد غير موجود.")
        return redirect("dashboard:timetable_day")

    periods_qs = Period.objects.filter(day__settings=settings_obj).select_related("day")
    period_map: dict[tuple[int, int], object] = {(p.day.weekday, p.index): p for p in periods_qs}

    lessons = (
        ClassLesson.objects.filter(settings=settings_obj, school_class=school_class)
        .select_related("subject", "teacher")
        .order_by("weekday", "period_index")
    )

    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = f"timetable_{school_class.name}.csv"
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.write("\ufeff")

    writer = csv.writer(resp)
    writer.writerow(["اليوم", "رقم الحصة", "وقت البداية", "وقت النهاية", "المادة", "المعلم/ـة"])

    for lesson in lessons:
        period = period_map.get((lesson.weekday, lesson.period_index))
        day_label = WEEKDAY_MAP.get(lesson.weekday, str(lesson.weekday))
        start_str = period.starts_at.strftime("%H:%M") if period and period.starts_at else ""
        end_str = period.ends_at.strftime("%H:%M") if period and period.ends_at else ""
        subject_name = lesson.subject.name if lesson.subject else ""
        teacher_name = lesson.teacher.name if lesson.teacher else ""
        writer.writerow([day_label, lesson.period_index, start_str, end_str, subject_name, teacher_name])

    return resp


# ───────────────────────────────────────────────────────────────────────
# تقويم الإجازات
# ───────────────────────────────────────────────────────────────────────
# الجدول الأسبوعي يعرف «الأحد» ولا يعرف «الأحد الذي يقع في إجازة العيد».
# هذه الشاشة تسدّ ذلك: مدىً بتاريخين يوقف عرض الجدول ويكتم تنبيه الانقطاع
# ويترك الشاشة تنام حتى أول يوم دوام.

class SchoolClosureForm(forms.ModelForm):
    class Meta:
        model = None  # يُضبط في __init__ لأن النموذج يُحمَّل بالاسم كبقية الملف
        fields = ("title", "start_date", "end_date")
        widgets = {
            "title": forms.TextInput(attrs={"class": "dp-input", "placeholder": "إجازة عيد الفطر"}),
            "start_date": forms.DateInput(attrs={"class": "dp-input", "type": "date"}),
            "end_date": forms.DateInput(attrs={"class": "dp-input", "type": "date"}),
        }

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "تاريخ النهاية قبل تاريخ البداية.")
        return cleaned


def _closure_form_class():
    """يبني الصنف عند الطلب: النماذج تُحمَّل بالاسم في هذا الملف."""
    from schedule.models import SchoolClosure

    meta = type("Meta", (SchoolClosureForm.Meta,), {"model": SchoolClosure})
    return type("BoundSchoolClosureForm", (SchoolClosureForm,), {"Meta": meta})


@manager_required
def closures_list(request):
    from schedule.models import SchoolClosure

    SchoolSettings = SchoolSettingsModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    if not settings_obj:
        messages.warning(request, "فضلاً أضف إعدادات المدرسة أولاً.")
        return redirect("dashboard:settings")

    form_class = _closure_form_class()
    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            closure = form.save(commit=False)
            closure.settings = settings_obj
            closure.save()
            messages.success(
                request,
                f"سُجّلت «{closure.title}». لن تُرسل تنبيهات انقطاع خلالها، وستعرض الشاشات رسالة الإجازة.",
            )
            return redirect("dashboard:closures_list")
        messages.error(request, "تعذّر حفظ الإجازة. راجع التواريخ المدخلة.")
    else:
        form = form_class()

    today = timezone.localdate()
    closures = list(SchoolClosure.objects.filter(settings=settings_obj))
    for closure in closures:
        closure.is_current = closure.covers(today)
        closure.is_past = bool(closure.end_date and closure.end_date < today)

    upcoming = [c for c in closures if not c.is_past]
    return render(
        request,
        "dashboard/closures_list.html",
        {
            "form": form,
            "closures": closures,
            "today": today,
            "current_closure": next((c for c in closures if c.is_current), None),
            "upcoming_count": len(upcoming),
        },
    )


@manager_required
@require_POST
def closure_delete(request, pk: int):
    from schedule.models import SchoolClosure

    SchoolSettings = SchoolSettingsModel()
    school, response = get_active_school_or_redirect(request)
    if response:
        return response

    settings_obj = SchoolSettings.objects.filter(school=school).first()
    closure = SchoolClosure.objects.filter(pk=pk, settings=settings_obj).first()
    if closure is None:
        messages.error(request, "الإجازة غير موجودة.")
    else:
        title = closure.title
        closure.delete()
        messages.success(request, f"حُذفت «{title}». عادت أيامها أيام دوام عادية.")
    return redirect("dashboard:closures_list")
