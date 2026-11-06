from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from core import occasions
from core.models import School, SubscriptionPlan, UserProfile
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
