import json
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.display_themes import effective_custom_theme, theme_payload
from core.models import DisplayScreen, School, SubscriptionPlan, UserProfile
from dashboard.forms import SchoolSettingsForm, ScreenDisplayCustomizationForm
from dashboard.theme_forms import DisplayThemeForm
from schedule.api_serializers import SchoolSettingsSerializer
from schedule.models import DisplayTheme, SchoolSettings
from subscriptions.models import SchoolSubscription


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CustomThemeTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة الألوان", slug="theme-school")
        self.other = School.objects.create(name="مدرسة أخرى", slug="theme-other")
        self.user = get_user_model().objects.create_user(username="theme-manager", password="TestPassword123!")
        profile = UserProfile.objects.create(user=self.user, active_school=self.school)
        profile.schools.add(self.school)
        self.settings = SchoolSettings.objects.create(school=self.school, name=self.school.name, theme="indigo")
        self.screen = DisplayScreen.objects.create(school=self.school, name="شاشة البهو")
        self.other_screen = DisplayScreen.objects.create(school=self.other, name="شاشة المدرسة الأخرى")
        self.theme = DisplayTheme.objects.create(school=self.school, name="هوية المدرسة")
        self.foreign = DisplayTheme.objects.create(school=self.other, name="ثيم سري")
        plan = SubscriptionPlan.objects.create(code="theme-plan", name="باقة", price=100, duration_days=365, max_screens=10)
        SchoolSubscription.objects.create(school=self.school, plan=plan, starts_at=timezone.localdate(), status="active")
        self.client.force_login(self.user)

    def payload(self, **changes):
        data = {name: getattr(self.theme, name) for name in DisplayThemeForm.Meta.fields}
        data["name"] = "ثيم جديد"
        data.update(changes)
        return data

    def action(self, action, theme=None, **data):
        return self.client.post(reverse("dashboard:theme_action", args=[(theme or self.theme).pk, action]), data)

    def screen_payload(self, **changes):
        data = {"name": self.screen.name, "is_active": "on", "theme": "indigo", "custom_theme": self.theme.pk,
                "featured_panel": "excellence", "inherit_groups": ["messages", "motion"], "occasion_theme": "auto"}
        data.update(changes)
        return data

    def test_library_only_exposes_active_school_themes(self):
        response = self.client.get(reverse("dashboard:theme_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.theme.name)
        self.assertNotContains(response, self.foreign.name)
        self.assertNotContains(response, self.other_screen.name)

    def test_create_and_edit_store_valid_theme(self):
        response = self.client.post(reverse("dashboard:theme_create"), self.payload())
        self.assertRedirects(response, reverse("dashboard:theme_list"))
        theme = DisplayTheme.objects.get(school=self.school, name="ثيم جديد")
        response = self.client.post(reverse("dashboard:theme_edit", args=[theme.pk]), self.payload(name="هوية جديدة", font="arial", font_scale=110))
        self.assertEqual(response.status_code, 302)
        theme.refresh_from_db()
        self.assertEqual((theme.name, theme.font, theme.font_scale), ("هوية جديدة", "arial", 110))

    def test_invalid_contrast_cannot_be_saved(self):
        form = DisplayThemeForm(data=self.payload(text_color="#142033"), school=self.school)
        self.assertFalse(form.is_valid())
        self.assertIn("text_color", form.errors)

    def test_css_urls_and_unknown_fonts_are_rejected(self):
        form = DisplayThemeForm(data=self.payload(background_color="url(https://evil.invalid)", font="evil-font"), school=self.school)
        self.assertFalse(form.is_valid())
        self.assertIn("background_color", form.errors)
        self.assertIn("font", form.errors)

    def test_duplicate_name_returns_form_error(self):
        response = self.client.post(reverse("dashboard:theme_create"), self.payload(name=self.theme.name))
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)

    def test_copy_preserves_appearance_but_has_distinct_name(self):
        response = self.action("duplicate")
        copy = DisplayTheme.objects.filter(school=self.school).exclude(pk=self.theme.pk).get()
        self.assertRedirects(response, reverse("dashboard:theme_edit", args=[copy.pk]))
        self.assertNotEqual(copy.name, self.theme.name)
        self.assertEqual(copy.card_color, self.theme.card_color)

    def test_every_write_is_school_scoped(self):
        self.assertEqual(self.client.get(reverse("dashboard:theme_edit", args=[self.foreign.pk])).status_code, 404)
        for action in ("apply", "duplicate", "delete"):
            self.assertEqual(self.action(action, theme=self.foreign).status_code, 404)
        self.assertEqual(self.action("apply", target=self.other_screen.pk).status_code, 404)

    def test_mutations_require_post(self):
        for action in ("apply", "duplicate", "delete"):
            response = self.client.get(reverse("dashboard:theme_action", args=[self.theme.pk, action]))
            self.assertEqual(response.status_code, 405)

    def test_selecting_foreign_theme_in_settings_is_rejected(self):
        form = SchoolSettingsForm(instance=self.settings, user=self.user, mode="display")
        data = dict(form.initial, custom_theme=self.foreign.pk)
        bound = SchoolSettingsForm(data=data, instance=self.settings, user=self.user, mode="display")
        self.assertFalse(bound.is_valid())
        self.assertIn("custom_theme", bound.errors)

    def test_global_settings_save_selected_theme_without_overwriting_custom_accent(self):
        form = SchoolSettingsForm(instance=self.settings, user=self.user, mode="display")
        data = dict(form.initial, custom_theme=self.theme.pk)
        bound = SchoolSettingsForm(data=data, instance=self.settings, user=self.user, mode="display")
        self.assertTrue(bound.is_valid(), bound.errors)
        bound.save()
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.custom_theme_id, self.theme.pk)
        self.assertEqual(self.settings.display_accent_color, self.theme.accent_color)

    def test_school_apply_keeps_independent_screen_theme(self):
        self.screen.theme_override = "rose"
        self.screen.save()
        self.assertEqual(self.action("apply", target="school").status_code, 302)
        self.settings.refresh_from_db(); self.screen.refresh_from_db()
        self.assertEqual(self.settings.custom_theme_id, self.theme.pk)
        self.assertIsNone(effective_custom_theme(self.settings, self.screen))

    def test_apply_to_all_only_resets_theme_overrides_in_own_school(self):
        self.screen.theme_override = "rose"
        self.screen.featured_panel_override = "duty"
        self.screen.save()
        self.action("apply", target="all")
        self.screen.refresh_from_db(); self.settings.refresh_from_db(); self.other_screen.refresh_from_db()
        self.assertEqual(self.screen.theme_override, "")
        self.assertEqual(self.screen.featured_panel_override, "duty")
        self.assertEqual(effective_custom_theme(self.settings, self.screen).pk, self.theme.pk)
        self.assertIsNone(self.other_screen.custom_theme_override_id)

    def test_screen_form_sets_custom_theme_and_can_restore_inheritance(self):
        form = ScreenDisplayCustomizationForm(self.screen_payload(), instance=self.screen, school_settings=self.settings)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(self.screen.custom_theme_override_id, self.theme.pk)
        form = ScreenDisplayCustomizationForm(self.screen_payload(inherit_groups=["appearance", "messages", "motion"]), instance=self.screen, school_settings=self.settings)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertIsNone(self.screen.custom_theme_override_id)
        self.assertEqual(self.screen.theme_override, "")

    def test_foreign_theme_cannot_be_selected_for_screen(self):
        form = ScreenDisplayCustomizationForm(self.screen_payload(custom_theme=self.foreign.pk), instance=self.screen, school_settings=self.settings)
        self.assertFalse(form.is_valid())
        self.assertIn("custom_theme", form.errors)

    def test_apply_specific_screen_does_not_change_school_default(self):
        self.action("apply", target=str(self.screen.pk))
        self.screen.refresh_from_db(); self.settings.refresh_from_db()
        self.assertEqual(self.screen.custom_theme_override_id, self.theme.pk)
        self.assertIsNone(self.settings.custom_theme_id)

    def test_delete_clears_foreign_keys_and_restores_preset_accent(self):
        self.action("apply", target="school")
        self.action("apply", target=str(self.screen.pk))
        self.action("delete")
        self.screen.refresh_from_db(); self.settings.refresh_from_db()
        self.assertIsNone(self.settings.custom_theme_id)
        self.assertIsNone(self.screen.custom_theme_override_id)
        self.assertEqual(self.screen.display_accent_color_override, "#6366F1")

    def test_server_payload_is_safe_and_serializer_contains_school_theme(self):
        self.settings.custom_theme = self.theme
        self.assertEqual(SchoolSettingsSerializer(self.settings).data["custom_theme"]["name"], self.theme.name)
        self.theme.card_color = "url(evil)"
        self.assertIsNone(theme_payload(self.theme))

    def test_snapshot_includes_custom_theme_for_live_updates(self):
        from django.test import RequestFactory
        from schedule.api_views import _build_final_snapshot
        self.settings.custom_theme = self.theme
        snapshot = _build_final_snapshot(RequestFactory().get("/"), self.settings, merge_real_data=False)
        self.assertEqual(snapshot["settings"]["custom_theme"]["id"], self.theme.pk)

    def test_model_validation_prevents_cross_school_assignment(self):
        self.screen.custom_theme_override = self.foreign
        with self.assertRaises(ValidationError):
            self.screen.full_clean()
        self.settings.custom_theme = self.foreign
        with self.assertRaises(ValidationError):
            self.settings.full_clean()

    def test_tv_html_embeds_effective_theme_as_escaped_json(self):
        self.action("apply", target=str(self.screen.pk))
        response = self.client.get(f"/s/{self.screen.short_code}/?preview=1")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'<script id="customDisplayTheme" type="application/json">(.*?)</script>', response.content.decode(), re.S)
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match.group(1))["id"], self.theme.pk)

    def test_preset_preview_overrides_saved_custom_theme(self):
        self.action("apply", target=str(self.screen.pk))
        response = self.client.get(f"/s/{self.screen.short_code}/?preview=1&preview_theme=rose")
        self.assertIsNone(response.context["custom_display_theme"])
        self.assertEqual(response.context["theme"], "rose")

    @patch("dashboard.views_themes._refresh_school_displays")
    def test_edit_schedules_refresh_after_commit(self, refresh):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("dashboard:theme_edit", args=[self.theme.pk]), self.payload(name=self.theme.name))
        refresh.assert_called_once_with(self.school)
