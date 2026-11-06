from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import School, SubscriptionPlan, UserProfile
from subscriptions.models import SchoolSubscription

from .forms import ComposeMailForm, school_matches_subscription_filter


class SubscriptionRecipientFixtures:
    def create_recipients(self):
        self.today = timezone.localdate()
        self.paid = SubscriptionPlan.objects.create(name="مدفوعة", code="mail-paid", price=100, duration_days=30)
        self.trial = SubscriptionPlan.objects.create(name="تجربة مجانية", code="mail-trial", price=0, duration_days=14)
        self.schools = {}
        for key in ("active", "open", "ends_today", "trial_active", "expired", "cancelled", "pending", "future", "no_subscription", "trial_ended", "trial_cancelled", "converted", "paid_ended"):
            school = School.objects.create(name=f"مدرسة {key}", slug=f"mail-{key}", is_active=False)
            manager = get_user_model().objects.create_user(username=f"manager_{key}", email=f"{key}@example.com")
            profile = UserProfile.objects.create(user=manager, active_school=school)
            profile.schools.add(school)
            self.schools[key] = school
        self.add_subscription("active")
        self.add_subscription("open")
        SchoolSubscription.objects.filter(school=self.schools["open"]).update(ends_at=None)
        self.add_subscription("ends_today", ends_at=self.today)
        self.add_subscription("trial_active", plan=self.trial)
        self.add_subscription("expired", ends_at=self.today-timedelta(days=1))
        self.add_subscription("cancelled", status="cancelled")
        self.add_subscription("pending", status="pending")
        self.add_subscription("future", starts_at=self.today+timedelta(days=1))
        self.add_subscription("trial_ended", plan=self.trial, ends_at=self.today-timedelta(days=1))
        self.add_subscription("trial_cancelled", plan=self.trial, status="cancelled")
        self.add_subscription("converted", plan=self.trial, status="expired")
        self.add_subscription("converted")
        self.add_subscription("paid_ended", plan=self.trial, status="expired")
        self.add_subscription("paid_ended", status="expired")

    def add_subscription(self, key, **kwargs):
        values = dict(school=self.schools[key], plan=self.paid, starts_at=self.today-timedelta(days=10), ends_at=self.today+timedelta(days=10), status="active")
        values.update(kwargs)
        return SchoolSubscription.objects.create(**values)

    def form_data(self, status, keys):
        return dict(subscription_status=status, schools=[self.schools[key].pk for key in keys], recipients="", subject="تحديث الاشتراك", body="تفاصيل الرسالة")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SubscriptionRecipientTests(SubscriptionRecipientFixtures, TestCase):
    def setUp(self):
        self.create_recipients()
        owner = get_user_model().objects.create_superuser(username="mail_filter_owner", email="owner@example.com", password="StrongPass123!")
        self.client.force_login(owner)

    def matching_keys(self, status):
        schools = ComposeMailForm().fields["schools"].queryset
        return {key for key, original in self.schools.items() if any(school.pk == original.pk and school_matches_subscription_filter(school, status) for school in schools)}

    def test_active_uses_current_coverage_including_live_trials_and_end_date(self):
        self.assertEqual(self.matching_keys("active"), {"active", "open", "ends_today", "trial_active", "converted"})

    def test_inactive_is_complement_including_missing_future_and_stale_active_rows(self):
        self.assertEqual(self.matching_keys("inactive"), set(self.schools)-self.matching_keys("active"))
        self.assertTrue({"no_subscription", "future", "expired", "cancelled", "pending", "trial_ended"}.issubset(self.matching_keys("inactive")))

    def test_stopped_trials_exclude_schools_that_upgraded_even_when_paid_term_ended(self):
        self.assertEqual(self.matching_keys("expired_trial"), {"trial_ended", "trial_cancelled"})

    def test_page_renders_filter_and_subscription_metadata(self):
        response = self.client.get(reverse("dashboard:system_mail_compose"))
        self.assertContains(response, 'id="id_subscription_status"')
        self.assertContains(response, 'data-subscription-active="1"')
        self.assertContains(response, 'data-expired-trial="1"')
        self.assertContains(response, "المدارس التي توقفت بعد التجربة المجانية")

    def test_each_group_sends_only_selected_schools_as_private_messages(self):
        for status, keys in (("active", ["active", "trial_active"]), ("inactive", ["expired", "no_subscription"]), ("expired_trial", ["trial_ended", "trial_cancelled"])):
            with self.subTest(status=status):
                mail.outbox.clear()
                response = self.client.post(reverse("dashboard:system_mail_compose"), self.form_data(status, keys))
                self.assertEqual(response.status_code, 302)
                self.assertCountEqual([email.to for email in mail.outbox], [[f"{key}@example.com"] for key in keys])

    def test_out_of_group_post_is_rejected_without_sending(self):
        for status, key in (("active", "expired"), ("inactive", "active"), ("expired_trial", "paid_ended"), ("invalid", "active")):
            with self.subTest(status=status):
                response = self.client.post(reverse("dashboard:system_mail_compose"), self.form_data(status, [key]))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
                self.assertEqual(len(mail.outbox), 0)

    def test_filter_alone_never_sends_to_entire_group(self):
        form = ComposeMailForm(self.form_data("active", []))
        self.assertFalse(form.is_valid())

    def test_missing_filter_remains_compatible_and_additional_addresses_are_deduplicated(self):
        data = self.form_data("", ["active", "expired"])
        data.pop("subscription_status")
        data["recipients"] = "ACTIVE@example.com; extra@example.com"
        form = ComposeMailForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertCountEqual(form.cleaned_data["recipients"], ["active@example.com", "expired@example.com", "extra@example.com"])

    def test_recipient_limit_and_missing_manager_still_apply(self):
        data = self.form_data("active", ["active"])
        with patch("mailcenter.forms.MAX_RECIPIENTS", 1):
            data["recipients"] = "extra@example.com"
            self.assertFalse(ComposeMailForm(data).is_valid())
        get_user_model().objects.filter(username="manager_trial_ended").update(is_active=False)
        form = ComposeMailForm(self.form_data("expired_trial", ["trial_ended"]))
        self.assertFalse(form.is_valid())
        self.assertIn("لا يوجد بريد صالح", str(form.errors))
