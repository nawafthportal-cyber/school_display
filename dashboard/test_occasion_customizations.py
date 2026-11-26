from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from core import occasions
from core.models import DisplayScreen, School, SubscriptionPlan, UserProfile
from core.occasion_customizations import cards_for_school, customizations_for_school
from notices.models import Announcement
from schedule.api_serializers import SchoolSettingsSerializer
from schedule.api_views import _build_final_snapshot
from schedule.models import SchoolOccasionTheme, SchoolSettings
from subscriptions.models import SchoolSubscription


class OccasionCustomizationTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة الاختبار", slug="occasion-own")
        self.other = School.objects.create(name="مدرسة أخرى", slug="occasion-other")
        self.settings = SchoolSettings.objects.create(school=self.school, name=self.school.name)
        plan = SubscriptionPlan.objects.create(code="occasion-test", name="اختبار", price=0, duration_days=30, max_screens=2)
        SchoolSubscription.objects.create(school=self.school, plan=plan, starts_at=timezone.localdate(), status="active")
        self.user = get_user_model().objects.create_user(username="occasion-manager", password="test-password")
        profile = UserProfile.objects.create(user=self.user, active_school=self.school)
        profile.schools.add(self.school)
        self.client.force_login(self.user)
        self.url = reverse("dashboard:occasion_theme_edit", args=["national_day"])

    def payload(self, **changes):
        original = occasions.get("national_day")
        data = {key: getattr(original, key) for key in ("tagline", "deep", "accent", "highlight", "soft")}
        data.update(name="هوية مدرستنا الوطنية", pattern="dots")
        data.update(changes)
        return data

    def test_form_starts_with_original_name_palette_and_pattern(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["name"].value(), "أخضر الوطن")
        self.assertEqual(response.context["form"]["pattern"].value(), "original")

    def test_school_event_can_be_pinned_scheduled_and_returned_to_auto(self):
        entrance = DisplayScreen.objects.create(school=self.school, name="المدخل")
        second = DisplayScreen.objects.create(school=self.school, name="المسرح")
        create = reverse("dashboard:occasion_theme_create") + f"?screen={entrance.pk}"
        response = self.client.post(create, self.payload(name="معرض العلوم"))
        event = SchoolOccasionTheme.objects.get(school=self.school)
        key = event.occasion_key
        self.assertRegex(key, r"^event_[0-9a-f]{16}$")
        self.assertIn(f"occasion={key}&screen={entrance.pk}", response.url)
        assign = reverse("dashboard:occasion_assign")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(assign, {"screen": entrance.pk, "occasion": key}).status_code, 302)
        entrance.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((entrance.occasion_theme, second.occasion_theme), (key, "auto"))
        self.assertContains(self.client.get(reverse("dashboard:screen_edit", args=[entrance.pk])), f'value="{key}" selected')
        board = self.client.get(f"/s/{entrance.short_code}/?preview=1")
        self.assertContains(board, f'data-screen-occasion-theme="{key}"')
        self.assertEqual(board.context["occasion_customizations"][key]["name"], event.name)
        schedule = reverse("dashboard:ann_create")
        form = self.client.get(schedule, {"template": key, "screen": second.pk}).context["form"]
        self.assertEqual(form.initial["title"], event.name)
        self.assertEqual(form.initial["screens"], [second.pk])
        self.assertIn((key, event.name), list(form.fields["occasion_theme"].choices))
        self.assertEqual(self.client.post(schedule, {
            "scope": "screens", "screens": [second.pk], "title": event.name, "occasion_theme": key,
            "level": "info", "starts_at": "2026-11-25T08:00", "is_active": "on",
        }).status_code, 302)
        self.assertEqual(list(Announcement.objects.get(occasion_theme=key).screens.all()), [second])
        self.client.post(assign, {"screen": entrance.pk, "occasion": "auto"})
        entrance.refresh_from_db()
        self.assertEqual(entrance.occasion_theme, "auto")

    def test_foreign_screens_and_events_cannot_be_used_in_school_designs(self):
        foreign_key = "event_0123456789abcdef"
        SchoolOccasionTheme.objects.create(school=self.other, occasion_key=foreign_key, **self.payload())
        own = DisplayScreen.objects.create(school=self.school, name="المدخل")
        foreign = DisplayScreen.objects.create(school=self.other, name="شاشة أخرى")
        assign = reverse("dashboard:occasion_assign")
        self.assertEqual(self.client.get(assign).status_code, 405)
        for screen, key in ((own, foreign_key), (foreign, "national_day"), (own, "unknown")):
            self.assertEqual(self.client.post(assign, {"screen": screen.pk, "occasion": key}).status_code, 404)
        self.assertEqual(self.client.get(reverse("dashboard:occasion_theme_edit", args=[foreign_key])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("dashboard:occasion_templates")), foreign_key)
        self.assertFalse(DisplayScreen.objects.exclude(occasion_theme="auto").exists())

    def test_card_link_presets_the_only_screen_and_offline_status_without_applying(self):
        screen = DisplayScreen.objects.create(
            school=self.school, name="شاشة المدخل", bound_device_id="occasion-test-tv",
            last_seen=timezone.now() - timedelta(hours=1),
        )
        url = reverse("dashboard:occasion_templates")
        response = self.client.get(url, {"occasion": "founding_day"})
        self.assertEqual(response.context["selected_screen"], screen)
        self.assertContains(response, "الشاشة منقطعة. سيظهر التغيير عند عودة الاتصال.")
        self.assertContains(response, 'data-occasion-apply="founding_day"')
        self.assertContains(response, f'?occasion=founding_day&amp;screen={screen.pk}#occasion-assignment')
        screen.refresh_from_db()
        self.assertEqual(screen.occasion_theme, "auto")

        DisplayScreen.objects.create(school=self.school, name="شاشة المسرح")
        response = self.client.get(url, {"occasion": "founding_day"})
        self.assertIsNone(response.context["selected_screen"])

    def test_custom_edits_keep_the_event_key_and_duplicate_names_are_rejected(self):
        create = reverse("dashboard:occasion_theme_create")
        self.client.post(create, self.payload(name="معرض العلوم"))
        event = SchoolOccasionTheme.objects.get(school=self.school)
        self.assertIn("name", self.client.post(create, self.payload(name=event.name)).context["form"].errors)
        edit = reverse("dashboard:occasion_theme_edit", args=[event.occasion_key])
        self.assertRedirects(self.client.post(edit, self.payload(name="أسبوع العلوم")), edit)
        self.assertEqual(self.client.post(edit, {"action": "reset"}).status_code, 404)
        event.refresh_from_db()
        self.assertEqual(event.name, "أسبوع العلوم")

    def test_name_colors_and_pattern_save_once_and_appear_in_selector(self):
        for name in ("هوية أولى", "هويتنا النهائية"):
            self.assertRedirects(self.client.post(self.url, self.payload(name=name)), self.url)
        self.assertEqual(SchoolOccasionTheme.objects.filter(school=self.school).count(), 1)
        response = self.client.get(reverse("dashboard:ann_create"), {"template": "national_day"})
        self.assertIn(("national_day", "هويتنا النهائية"), list(response.context["form"].fields["occasion_theme"].choices))
        self.assertContains(response, "تعديل اسم هذا الثيم وتصميمه")
        self.assertEqual(cards_for_school(self.school.pk)[0]["name"], "هويتنا النهائية")
        self.assertEqual(occasions.get("national_day").name, "اليوم الوطني السعودي")

    def test_school_scope_ignores_submitted_foreign_school_and_keeps_other_palette(self):
        foreign = SchoolOccasionTheme.objects.create(school=self.other, occasion_key="national_day", **self.payload(name="ثيم خاص آخر"))
        self.client.post(self.url, self.payload(school=self.other.pk))
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "ثيم خاص آخر")
        self.assertEqual(customizations_for_school(self.school.pk)["national_day"]["name"], "هوية مدرستنا الوطنية")
        self.assertNotIn("ثيم خاص آخر", self.client.get(self.url).content.decode())

    def test_invalid_contrast_css_and_unknown_patterns_are_rejected(self):
        for changes in ({"soft": "#001d16"}, {"deep": "url(https://invalid.example)"}, {"pattern": "url(evil)"}, {"name": "   "}):
            with self.subTest(changes=changes):
                response = self.client.post(self.url, self.payload(**changes))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.assertFalse(SchoolOccasionTheme.objects.exists())

    def test_snapshot_and_serializer_expose_only_current_school_customization(self):
        self.client.post(self.url, self.payload())
        data = SchoolSettingsSerializer(self.settings).data["occasion_customizations"]
        self.assertEqual(data["national_day"]["name"], "هوية مدرستنا الوطنية")
        Announcement.objects.create(school=self.school, title="مناسبة", occasion_theme="national_day", starts_at=timezone.now()-timedelta(minutes=1))
        snap = _build_final_snapshot(RequestFactory().get("/"), self.settings)
        self.assertEqual(snap["settings"]["occasion_customizations"], data)
        self.assertEqual(snap["announcements"][0]["occasion_theme_label"], "هوية مدرستنا الوطنية")
        self.assertEqual(customizations_for_school(self.other.pk), {})

    def test_reset_is_post_only_and_keeps_other_schools(self):
        self.client.post(self.url, self.payload())
        SchoolOccasionTheme.objects.create(school=self.other, occasion_key="national_day", **self.payload())
        self.client.get(self.url, {"action": "reset"})
        self.assertTrue(SchoolOccasionTheme.objects.filter(school=self.school).exists())
        self.assertRedirects(self.client.post(self.url, {"action": "reset"}), self.url)
        self.assertFalse(SchoolOccasionTheme.objects.filter(school=self.school).exists())
        self.assertTrue(SchoolOccasionTheme.objects.filter(school=self.other).exists())

    def test_unknown_keys_and_anonymous_requests_do_not_mutate(self):
        self.assertEqual(self.client.post(reverse("dashboard:occasion_theme_edit", args=["unknown"]), self.payload()).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 302)
        self.assertFalse(SchoolOccasionTheme.objects.exists())

    def test_names_are_escaped_in_editor_json_and_markup(self):
        self.client.post(self.url, self.payload(name='</script><img src=x onerror="alert(1)">'))
        response = self.client.get(self.url)
        self.assertNotContains(response, '</script><img src=x')
        self.assertContains(response, '\\u003C/script\\u003E')

    @patch("dashboard.views_themes._refresh_school_displays")
    def test_save_and_reset_refresh_screens_after_commit(self, refresh):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, self.payload())
        refresh.assert_called_once_with(self.school)
        refresh.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {"action": "reset"})
        refresh.assert_called_once_with(self.school)
