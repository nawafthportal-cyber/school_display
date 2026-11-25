"""Smoke coverage for the redesigned dashboard pages.

These eight templates were rebuilt at once (three for looks, five for flow), so
the point here is narrow and blunt: every one of them still renders for a real
manager, and the handful of behaviours the redesign *added* actually reach the
page — the fill-progress hooks on the timetable, the day steppers on the
day-scoped lists, the batch add on school data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as utc_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import School, SubscriptionPlan, UserProfile
from schedule.models import ClassLesson, DaySchedule, Period, SchoolClass, SchoolSettings, Subject, Teacher
from subscriptions.models import SchoolSubscription


class RedesignedDashboardPagesTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة القوالب", slug="redesign-school")
        self.user = get_user_model().objects.create_user(
            username="redesign_manager", password="StrongPass123!"
        )
        self.profile = UserProfile.objects.create(user=self.user, active_school=self.school)
        self.profile.schools.add(self.school)
        self.settings = SchoolSettings.objects.create(name=self.school.name, school=self.school)
        # Without a live subscription the tenant middleware bounces every
        # dashboard page, and each assertion below would only prove that.
        plan = SubscriptionPlan.objects.create(
            code="redesign-plan",
            name="خطة الاختبار",
            price=100,
            duration_days=365,
            max_screens=3,
            max_users=4,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate(),
            status="active",
        )
        self.client.force_login(self.user)

    # ---------------------------------------------------------------- render

    def test_every_redesigned_page_renders(self):
        for name in (
            "dashboard:index",
            "dashboard:help_getting_started",
            "dashboard:settings",
            "dashboard:timetable_day",
            "dashboard:school_data",
            "dashboard:days_list",
            "dashboard:standby_list",
            "dashboard:duty_list",
        ):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "dashboard-premium.css")

    # ------------------------------------------------------------- timetable

    def test_timetable_day_exposes_fill_progress_and_day_chips(self):
        day = DaySchedule.objects.filter(settings=self.settings, weekday=7).first()
        if day is None:
            day = DaySchedule.objects.create(settings=self.settings, weekday=7, periods_count=1)
        Period.objects.create(day=day, index=1, starts_at="07:00", ends_at="07:45")
        SchoolClass.objects.create(settings=self.settings, name="أول/أ")
        Subject.objects.create(school=self.school, name="الرياضيات")
        Teacher.objects.create(school=self.school, name="أ. سارة")

        response = self.client.get(reverse("dashboard:timetable_day"), {"weekday": 7})

        self.assertEqual(response.status_code, 200)
        # The progress readout and the per-row state hooks the editor drives.
        self.assertContains(response, "data-tt-bar")
        self.assertContains(response, "data-tt-summary")
        self.assertContains(response, "data-tt-row")
        # Days are chips now, not a dropdown that needs a second action.
        self.assertContains(response, "data-tt-day")
        self.assertContains(response, "data-tt-autosave=\"true\"")
        self.assertContains(response, "data-tt-autosave-toast")

    def test_timetable_defaults_to_the_school_local_weekday(self):
        SchoolClass.objects.create(settings=self.settings, name="أول/أ")
        school_class = SchoolClass.objects.create(settings=self.settings, name="ثاني/أ")
        daily_url = reverse("dashboard:timetable_day")
        school_clock_time = datetime(2026, 11, 24, 21, 5, tzinfo=utc_timezone.utc)
        with patch("dashboard.views_schedule.timezone", wraps=timezone) as school_clock:
            school_clock.localdate.side_effect = lambda **kwargs: timezone.localdate(school_clock_time, **kwargs)
            default = self.client.get(daily_url)
            explicit = self.client.get(daily_url, {"weekday": 7, "class_id": school_class.pk})
            returned = self.client.get(daily_url)
            school_clock_time += timedelta(days=1)
            following_day = self.client.get(daily_url)
        self.assertEqual(default.context["weekday"], 3)
        self.assertEqual(explicit.context["weekday"], 7)
        self.assertEqual(returned.context["weekday"], 7)
        self.assertEqual(returned.context["selected_class"], school_class)
        self.assertEqual(following_day.context["weekday"], 4)
        self.assertEqual(following_day.context["selected_class"], school_class)

    def test_lesson_page_keeps_filters_when_leaving_edit(self):
        school_class = SchoolClass.objects.create(settings=self.settings, name="ثاني/أ")
        subject = Subject.objects.create(school=self.school, name="الرياضيات")
        teacher = Teacher.objects.create(school=self.school, name="أ. سارة")
        ClassLesson.objects.bulk_create([
            ClassLesson(settings=self.settings, school_class=school_class, subject=subject, teacher=teacher,
                        weekday=day, period_index=index)
            for day in range(1, 5) for index in range(1, 8)
        ])
        list_url = reverse("dashboard:lessons_list")
        response = self.client.get(list_url, {"class_id": school_class.pk, "page": 2})
        page = response.context["page"]
        self.assertEqual((page.number, len(page.object_list), page.paginator.count), (2, 3, 28))
        self.assertContains(response, f"class_id={school_class.pk}&amp;page=1")
        return_to = response.wsgi_request.get_full_path()
        edit = self.client.get(reverse("dashboard:edit_lesson", args=[page.object_list[0].pk]), {"next": return_to})
        self.assertEqual(edit.context["list_url"], return_to)

    def test_timetable_day_ajax_post_saves_without_redirect(self):
        day = DaySchedule.objects.create(settings=self.settings, weekday=7, periods_count=1)
        Period.objects.create(day=day, index=1, starts_at="07:00", ends_at="07:45")
        school_class = SchoolClass.objects.create(settings=self.settings, name="أول/أ")
        subject = Subject.objects.create(school=self.school, name="الرياضيات")
        teacher = Teacher.objects.create(school=self.school, name="أ. سارة")

        response = self.client.post(
            reverse("dashboard:timetable_day"),
            {
                "weekday": "7",
                "class_id": str(school_class.id),
                f"subject-{school_class.id}-1": str(subject.id),
                f"teacher-{school_class.id}-1": str(teacher.id),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(
            ClassLesson.objects.filter(
                settings=self.settings,
                school_class=school_class,
                weekday=7,
                period_index=1,
                subject=subject,
                teacher=teacher,
            ).exists()
        )

    def test_timetable_day_without_periods_points_at_the_day_setup(self):
        SchoolClass.objects.create(settings=self.settings, name="ثاني/ب")

        response = self.client.get(reverse("dashboard:timetable_day"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("dashboard:days_list"))

    # ------------------------------------------------------------ school data

    def test_school_data_accepts_a_pasted_list_of_names(self):
        response = self.client.post(
            reverse("dashboard:add_teacher"),
            {"name": "أ. منى\nأ. هند، أ. ليلى\n\nأ. منى"},
        )

        self.assertEqual(response.status_code, 302)
        # Deduplicated, and the trailing blank line ignored.
        self.assertEqual(
            sorted(Teacher.objects.filter(school=self.school).values_list("name", flat=True)),
            sorted(["أ. منى", "أ. هند", "أ. ليلى"]),
        )
        # The redirect marks which panel to send the caret back to.
        self.assertIn("added=teachers", response["Location"])

    def test_school_data_rejects_an_empty_submission(self):
        response = self.client.post(reverse("dashboard:add_class"), {"name": "   \n  "})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SchoolClass.objects.filter(settings=self.settings).exists())

    # ------------------------------------------------------------------ days

    def test_days_list_flags_an_active_day_with_no_period_times(self):
        response = self.client.get(reverse("dashboard:days_list"))

        self.assertEqual(response.status_code, 200)
        # Freshly seeded days are active but have no periods, which is exactly
        # the state that used to leave the board silently blank.
        self.assertContains(response, "يحتاج ضبط")
        self.assertContains(response, "بلا أوقات حصص")

    def test_days_list_links_each_day_to_its_own_timetable(self):
        # The weekday rows are seeded lazily by the view, so create one here
        # rather than relying on a prior request having run.
        day, _ = DaySchedule.objects.get_or_create(
            settings=self.settings, weekday=7, defaults={"periods_count": 1, "is_active": True}
        )
        Period.objects.create(day=day, index=1, starts_at="07:00", ends_at="07:45")

        response = self.client.get(reverse("dashboard:days_list"))

        self.assertContains(
            response,
            f'{reverse("dashboard:timetable_day")}?weekday={day.weekday}',
        )

    # -------------------------------------------------------- day-scoped lists

    def test_standby_and_duty_share_the_day_stepper(self):
        for name in ("dashboard:standby_list", "dashboard:duty_list"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertContains(response, "data-date-filter")
                self.assertContains(response, 'data-date-step="-1"')
                self.assertContains(response, 'data-date-step="1"')
                self.assertContains(response, "data-date-today")

    # -------------------------------------------------------------- settings

    def test_settings_runtime_tab_groups_the_monitoring_rules(self):
        response = self.client.get(reverse("dashboard:settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-monitor-summary")
        self.assertContains(response, "runtime-presets")
        self.assertContains(response, "متى نعتبر الشاشة منقطعة")
        self.assertContains(response, "حدود التكرار")
