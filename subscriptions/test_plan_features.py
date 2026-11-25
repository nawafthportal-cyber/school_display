from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import DisplayScreen, School, SubscriptionPlan, SystemEmployeeProfile, UserProfile
from notices.models import Announcement
from schedule.models import SchoolSettings
from subscriptions.features import PLAN_FEATURES
from subscriptions.models import SchoolSubscription


@override_settings(
    TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False,
    DISPLAY_REQUIRE_ACTIVE_SUBSCRIPTION=True,
    DISPLAY_SNAPSHOT_ASYNC_BUILD=False,
)
class SubscriptionPlanFeatureTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.school = School.objects.create(name="Feature school", slug="feature-school")
        SchoolSettings.objects.create(school=self.school, name=self.school.name, theme="indigo")
        self.plan = SubscriptionPlan.objects.create(
            code="features-monthly", name="Monthly", price=129, duration_days=30, max_screens=5,
        )
        self.subscription = SchoolSubscription.objects.create(school=self.school, plan=self.plan)
        self.manager = get_user_model().objects.create_user(username="feature-manager")
        profile = UserProfile.objects.create(user=self.manager, active_school=self.school)
        profile.schools.add(self.school)
        self.client.force_login(self.manager)

    def set_disabled(self, *features):
        self.plan.disabled_features = list(features)
        self.plan.save(update_fields=["disabled_features"])

    def subscription_features(self, client, url):
        response = client.get(url)
        self.assertContains(response, "خصائص اشتراك المدرسة")
        return {row["key"]: row["enabled"] for row in response.context["school_subscription_features"]}

    def test_platform_admin_creates_and_edits_plan_with_feature_switches(self):
        admin = get_user_model().objects.create_superuser(username="feature-owner")
        self.client.force_login(admin)
        create_url = reverse("dashboard:system_plan_create")
        response = self.client.get(create_url)
        self.assertContains(response, '<input type="checkbox" name="enabled_features"', count=len(PLAN_FEATURES))
        self.assertEqual(set(response.context["form"]["enabled_features"].value()), set(PLAN_FEATURES))
        form_data = {
            "name": "Half year",
            "code": "features-half-year",
            "price": "650.00",
            "duration_days": 180,
            "max_screens": 5,
            "sort_order": 0,
            "is_active": "on",
            "features_submitted": "True",
        }
        plans_url = reverse("dashboard:system_plans_list")
        response = self.client.post(create_url, {**form_data, "enabled_features": ["standby", "duty"]})
        self.assertRedirects(response, plans_url)
        plan = SubscriptionPlan.objects.get(code=form_data["code"])
        self.assertEqual(set(plan.disabled_features), set(PLAN_FEATURES) - {"standby", "duty"})
        self.assertEqual(plan.max_screens, 5)

        edit_url = reverse("dashboard:system_plan_edit", args=[plan.pk])
        response = self.client.get(edit_url)
        self.assertEqual(set(response.context["form"]["enabled_features"].value()), {"standby", "duty"})
        self.assertRedirects(self.client.post(edit_url, form_data), plans_url)
        plan.refresh_from_db()
        self.assertEqual(set(plan.disabled_features), set(PLAN_FEATURES))

        response = self.client.post(edit_url, {**form_data, "enabled_features": list(PLAN_FEATURES)})
        self.assertRedirects(response, plans_url)
        plan.refresh_from_db()
        self.assertEqual(plan.disabled_features, [])

    def test_school_dashboard_and_platform_admin_show_the_effective_paid_plan(self):
        trial = SubscriptionPlan.objects.create(
            name="Trial", code="features-trial", price=0, duration_days=90, max_screens=1,
        )
        SchoolSubscription.objects.create(school=self.school, plan=trial)
        self.set_disabled("announcements", "duty")

        features_url = reverse("dashboard:my_subscription")
        features = self.subscription_features(self.client, features_url)
        self.assertFalse(features["announcements"])
        self.assertFalse(features["duty"])
        self.assertTrue(features["standby"])
        self.assertTrue(features["screen_customization"])
        self.assertNotContains(self.client.get(features_url), reverse("dashboard:ann_list"))

        admin = get_user_model().objects.create_superuser(username="subscription-owner")
        admin_client = Client()
        admin_client.force_login(admin)
        edit_url = reverse("dashboard:system_subscription_edit", args=[self.subscription.pk])
        self.assertEqual(self.subscription_features(admin_client, edit_url), features)

    def test_school_manager_and_view_only_employee_cannot_change_features(self):
        create_url = reverse("dashboard:system_plan_create")
        edit_url = reverse("dashboard:system_plan_edit", args=[self.plan.pk])
        submitted = {"features_submitted": "True"}
        for response in (self.client.get(create_url), self.client.get(edit_url), self.client.post(edit_url, submitted)):
            self.assertRedirects(response, reverse("dashboard:index"), fetch_redirect_response=False)

        employee = get_user_model().objects.create_user(username="plans-reader", is_staff=True)
        SystemEmployeeProfile.objects.create(user=employee, role="custom", permission_keys=["plans.view"])
        self.client.force_login(employee)
        self.assertEqual(self.client.get(create_url).status_code, 403)
        self.assertEqual(self.client.get(edit_url).status_code, 403)
        self.assertEqual(self.client.post(edit_url, submitted).status_code, 403)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.disabled_features, [])

    def test_disabling_blocks_direct_writes_and_reenable_keeps_saved_data(self):
        announcement = Announcement.objects.create(school=self.school, title="Saved school announcement")
        list_url = reverse("dashboard:ann_list")
        delete_url = reverse("dashboard:ann_delete", args=[announcement.pk])
        self.assertContains(self.client.get(list_url), announcement.title)

        self.set_disabled("announcements")
        self.assertRedirects(
            self.client.post(delete_url), reverse("dashboard:my_subscription"), fetch_redirect_response=False,
        )
        self.assertTrue(Announcement.objects.filter(pk=announcement.pk).exists())
        response = self.client.get(list_url, headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "feature_not_in_plan", "feature": "announcements"})

        self.set_disabled()
        self.assertContains(self.client.get(list_url), announcement.title)

    def test_disabled_import_cannot_be_confirmed_from_a_saved_preview(self):
        self.set_disabled("smart_import")
        cache.set(
            f"school-data-import:{self.manager.pk}:{self.school.pk}:smart-draft",
            {"requires_document_review": True},
        )
        import_url = reverse("dashboard:school_data_import")
        subscription_url = reverse("dashboard:my_subscription")
        self.assertRedirects(
            self.client.post(import_url, {"action": "import", "import_token": "smart-draft"}),
            subscription_url,
            fetch_redirect_response=False,
        )
        self.assertContains(self.client.get(import_url), "Excel")
        self.set_disabled("excel_import")
        self.assertRedirects(
            self.client.post(import_url, {"action": "preview"}), subscription_url, fetch_redirect_response=False,
        )
        self.assertRedirects(
            self.client.get(import_url), reverse("dashboard:school_document_import"), fetch_redirect_response=False,
        )

    @patch("schedule.api_views._snapshot_rate_limit_allow", return_value=True)
    def test_screen_content_is_filtered_even_when_reusing_the_original_snapshot(self, _rate_limit):
        tv = Client()
        screen = DisplayScreen.objects.create(school=self.school, name="Reception TV")
        announcement = Announcement.objects.create(school=self.school, title="Announcement visible on screen")
        tv_url = reverse("core_api:display_api:snapshot_token", args=[screen.token])
        tv_headers = {"X-Display-Device": "features-reception-tv"}

        response = tv.get(tv_url, headers=tv_headers)
        self.assertContains(response, announcement.title)
        original_etag = response["ETag"]

        # Preserve the old snapshot until commit, as a live screen might have cached it.
        with patch("subscriptions.signals.refresh_school_features") as refresh:
            with self.captureOnCommitCallbacks(execute=True):
                self.set_disabled("announcements")
                response = tv.get(tv_url, headers={**tv_headers, "If-None-Match": original_etag})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["X-Snapshot-Cache"], "HIT")
                self.assertEqual(response.json()["announcements"], [])
                self.assertNotContains(response, announcement.title)
                self.assertEqual(response["X-Display-Feature-Policy"], "announcements")
                self.assertNotEqual(response["ETag"], original_etag)
            refresh.assert_called_once_with(self.school.pk)

        response = tv.get(tv_url, headers={**tv_headers, "If-None-Match": response["ETag"]})
        self.assertEqual(response.status_code, 304)
        self.set_disabled()
        self.assertContains(tv.get(tv_url, headers=tv_headers), announcement.title)

    def test_switching_school_subscription_invalidates_previous_plan_settings(self):
        self.set_disabled("screen_customization")
        screen = DisplayScreen.objects.create(school=self.school, name="Library TV", theme_override="emerald")
        url = reverse("dashboard:screen_edit", args=[screen.pk])
        self.assertRedirects(self.client.get(url), reverse("dashboard:my_subscription"))
        tv = Client()
        screen_url = reverse("website:home")
        snapshot_url = reverse("core_api:display_api:snapshot_token", args=[screen.token])
        tv_headers = {"X-Display-Device": "features-library-tv"}
        self.assertContains(tv.get(screen_url, {"token": screen.token}), 'data-theme="indigo"')
        self.assertEqual(tv.get(snapshot_url, headers=tv_headers)["X-Display-Feature-Policy"], "screen_customization")

        replacement = SubscriptionPlan.objects.create(
            code="features-annual", name="Annual", price=1090, duration_days=365, max_screens=5,
        )
        self.subscription.plan = replacement
        self.subscription.ends_at = timezone.localdate() + timedelta(days=365)
        self.subscription.save(update_fields=["plan", "ends_at"])
        self.assertContains(self.client.get(url), screen.name)
        self.assertContains(tv.get(screen_url, {"token": screen.token}), 'data-theme="emerald"')
        self.assertEqual(tv.get(snapshot_url, headers=tv_headers)["X-Display-Feature-Policy"], "-")
