from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.checks import Warning as CheckWarning
from django.core.checks import run_checks
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import School, SubscriptionPlan, UserProfile
from schedule.models import SchoolSettings

from .access import school_max_screens, school_subscription_is_active
from .models import (
    MoyasarCheckout,
    SchoolSubscription,
    SubscriptionInvoice,
    SubscriptionPaymentOperation,
    SubscriptionRefund,
    SubscriptionRequest,
)
from .moyasar import MoyasarClient
from .moyasar_processing import (
    MoyasarVerificationError,
    apply_payment_details,
    reconcile_pending_checkouts,
)
from .utils import school_has_active_subscription


@override_settings(
    DEBUG=True,
    MOYASAR_ENABLED=True,
    MOYASAR_LIVE_MODE=False,
    MOYASAR_ACTIVATE_TEST_PAYMENTS=False,
    MOYASAR_API_BASE_URL="https://api.moyasar.com/v1",
    MOYASAR_PUBLISHABLE_KEY="pk_test_publishable",
    MOYASAR_SECRET_KEY="sk_test_secret",
    MOYASAR_WEBHOOK_SECRET="webhook-test-secret",
    MOYASAR_CALLBACK_BASE_URL="https://school-display.com",
    MOYASAR_APPLE_PAY_COUNTRY="SA",
    MOYASAR_APPLE_PAY_LABEL="لوحة العرض الذكية",
    MOYASAR_APPLE_PAY_VALIDATE_MERCHANT_URL="https://api.moyasar.com/v1/applepay/initiate",
    MOYASAR_GOOGLE_PAY_MERCHANT_ID="test-merchant-id",
    MOYASAR_GOOGLE_PAY_COUNTRY="SA",
    MOYASAR_GOOGLE_PAY_LABEL="لوحة العرض الذكية",
    MOYASAR_GOOGLE_PAY_ENVIRONMENT="TEST",
    MOYASAR_HTTP_TIMEOUT_SECONDS=5,
)
class MoyasarCheckoutTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة ميسر", slug="moyasar-school")
        self.plan = SubscriptionPlan.objects.create(
            code="moyasar-plan",
            name="الخطة السنوية",
            price=Decimal("500.00"),
            duration_days=365,
            max_screens=3,
        )
        self.owner = get_user_model().objects.create_superuser(
            username="moyasar_owner",
            password="StrongPass123!",
            email="owner@example.com",
        )
        owner_profile = UserProfile.objects.create(user=self.owner, active_school=self.school)
        owner_profile.schools.add(self.school)
        self.manager = get_user_model().objects.create_user(
            username="moyasar_manager",
            password="StrongPass123!",
            email="manager@example.com",
        )
        manager_profile = UserProfile.objects.create(
            user=self.manager,
            active_school=self.school,
            # Checkout requires a proven address so the invoice can be delivered.
            email_verified_at=timezone.now(),
        )
        manager_profile.schools.add(self.school)
        self.manager_profile = manager_profile

    def _checkout(self, *, live_mode: bool = False, created_by=None) -> MoyasarCheckout:
        return MoyasarCheckout.objects.create(
            school=self.school,
            created_by=created_by or self.owner,
            plan=self.plan,
            request_type="new",
            starts_at=timezone.localdate(),
            amount=self.plan.price,
            currency="SAR",
            live_mode=live_mode,
        )

    def _details(self, checkout: MoyasarCheckout, *, status: str = "paid", amount: int = 50000):
        return {
            "id": "79cced57-9deb-4c4b-8f48-59c124f79688",
            "status": status,
            "amount": amount,
            "currency": "SAR",
            "live": checkout.live_mode,
            "metadata": {"merchant_reference": checkout.merchant_reference},
        }

    @override_settings(DEBUG=False)
    def test_test_checkout_is_restricted_to_superuser(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("subscriptions:moyasar_start"),
            {"request_type": "new", "plan_id": self.plan.pk},
        )

        self.assertRedirects(response, reverse("dashboard:my_subscription"))
        self.assertFalse(MoyasarCheckout.objects.exists())

    def test_start_creates_test_checkout_and_never_exposes_secret_key(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("subscriptions:moyasar_start"),
            {"request_type": "new", "plan_id": self.plan.pk},
        )

        checkout = MoyasarCheckout.objects.get()
        self.assertRedirects(
            response,
            reverse("subscriptions:moyasar_checkout", kwargs={"reference": checkout.merchant_reference}),
        )
        page = self.client.get(response.url)
        self.assertContains(page, "pk_test_publishable")
        self.assertNotContains(page, "sk_test_secret")
        self.assertContains(page, "بيئة اختبار")

    def test_checkout_config_includes_all_supported_moyasar_methods(self):
        """The checkout config should pass through all supported Moyasar methods
        and include the Apple Pay configuration block when Apple Pay is enabled."""
        self.client.force_login(self.manager)

        start = self.client.post(
            reverse("subscriptions:moyasar_start"),
            {"request_type": "new", "plan_id": self.plan.pk},
        )
        checkout = MoyasarCheckout.objects.get()
        self.assertRedirects(
            start,
            reverse("subscriptions:moyasar_checkout", kwargs={"reference": checkout.merchant_reference}),
        )

        page = self.client.get(start.url)
        self.assertEqual(page.status_code, 200)

        raw = page.content.decode()
        marker = raw.index('id="moyasar-config"')
        blob = raw[raw.index(">", marker) + 1 : raw.index("</script>", marker)]
        config = json.loads(blob)

        self.assertEqual(config["methods"], ["creditcard", "applepay", "googlepay"])
        self.assertEqual(
            config["apple_pay"],
            {
                "country": "SA",
                "label": "لوحة العرض الذكية",
                "validate_merchant_url": "https://api.moyasar.com/v1/applepay/initiate",
            },
        )
        self.assertEqual(
            config["google_pay"],
            {
                "merchant_id": "test-merchant-id",
                "country": "SA",
                "label": "لوحة العرض الذكية",
                "environment": "TEST",
            },
        )
        # Every field the form validates as always-required must be present.
        for required in ("amount", "currency", "description", "publishable_api_key", "callback_url"):
            self.assertTrue(config.get(required), f"missing required config: {required}")
        self.assertTrue(config["callback_url"].startswith("https://"))
        self.assertEqual(
            config["callback_url"],
            "https://school-display.com"
            + reverse(
                "subscriptions:moyasar_return_for_checkout",
                kwargs={"reference": checkout.merchant_reference},
            ),
        )
        self.assertEqual(
            config["payment_sync_url"],
            reverse(
                "subscriptions:moyasar_sync",
                kwargs={"reference": checkout.merchant_reference},
            ),
        )

    def test_wallet_methods_are_passed_through_for_moyasar(self):
        # The checkout layer should keep every enabled method and add the extra
        # Apple Pay config separately.
        from config import settings as settings_module

        resolved = [
            method
            for method in ("creditcard", "applepay", "googlepay")
            if method in settings_module._MOYASAR_SUPPORTED_METHODS
        ] or ["creditcard"]
        self.assertEqual(resolved, ["creditcard", "applepay", "googlepay"])
        self.assertEqual(settings_module.MOYASAR_PAYMENT_METHODS, ["creditcard", "applepay", "googlepay"])

    def test_stc_pay_is_off_by_default_but_can_be_restored_by_configuration(self):
        """STC Pay is hidden, not removed.

        It stays in the supported set so bringing it back is an environment
        change; only the default list drops it.
        """
        from config import settings as settings_module

        self.assertNotIn("stcpay", settings_module.MOYASAR_PAYMENT_METHODS)
        self.assertIn("stcpay", settings_module._MOYASAR_SUPPORTED_METHODS)

    @override_settings(
        DEBUG=False,
        RUNNING_TESTS=False,
        MOYASAR_ENABLED=True,
        MOYASAR_LIVE_MODE=True,
        MOYASAR_GOOGLE_PAY_MERCHANT_ID="",
    )
    def test_deploy_checks_warn_when_google_pay_merchant_id_is_missing(self):
        issues = run_checks(tags=["subscriptions"])
        self.assertTrue(any(isinstance(issue, CheckWarning) and issue.id == "subscriptions.W003" for issue in issues))

    def test_start_is_blocked_until_the_email_is_verified(self):
        """An unverified address means the invoice may never reach the buyer."""
        self.manager_profile.email_verified_at = None
        self.manager_profile.save(update_fields=["email_verified_at"])
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("subscriptions:moyasar_start"),
            {"request_type": "new", "plan_id": self.plan.pk},
        )

        self.assertRedirects(response, reverse("dashboard:my_subscription"))
        self.assertFalse(MoyasarCheckout.objects.exists())

    def test_paid_test_payment_does_not_activate_real_subscription(self):
        checkout = self._checkout(live_mode=False)

        result = apply_payment_details(checkout.pk, self._details(checkout), event_type="return")

        self.assertEqual(result.status, "paid")
        self.assertIsNone(result.subscription_id)
        self.assertIsNone(result.payment_operation_id)

    def test_documented_payment_response_without_live_field_is_accepted(self):
        """Fetch Payment selects its environment by API key, not a response field."""
        checkout = self._checkout(live_mode=False)
        details = self._details(checkout)
        details.pop("live")

        result = apply_payment_details(checkout.pk, details, event_type="return")

        self.assertEqual(result.status, "paid")
        self.assertEqual(result.payment_id, details["id"])

    def test_cancel_marks_own_initiated_checkout_as_voided(self):
        checkout = self._checkout(created_by=self.manager)
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse(
                "subscriptions:moyasar_cancel",
                kwargs={"reference": checkout.merchant_reference},
            )
        )

        self.assertRedirects(response, reverse("dashboard:my_subscription"))
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "voided")
        self.assertEqual(checkout.last_event, "customer_canceled")
        self.assertIsNotNone(checkout.processed_at)
        self.assertIsNone(checkout.payment_id)
        self.assertIsNone(checkout.payment_operation_id)

    def test_cancel_cannot_change_another_users_checkout(self):
        checkout = self._checkout(created_by=self.owner)
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse(
                "subscriptions:moyasar_cancel",
                kwargs={"reference": checkout.merchant_reference},
            )
        )

        self.assertEqual(response.status_code, 404)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "initiated")

    @patch("subscriptions.moyasar_views.MoyasarClient.fetch_payment")
    def test_on_completed_sync_saves_payment_id_before_redirect(self, fetch_payment):
        checkout = self._checkout(created_by=self.manager)
        details = self._details(checkout, status="initiated")
        details.pop("live")
        fetch_payment.return_value = details
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse(
                "subscriptions:moyasar_sync",
                kwargs={"reference": checkout.merchant_reference},
            ),
            data=json.dumps({"id": details["id"]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        checkout.refresh_from_db()
        self.assertEqual(checkout.payment_id, details["id"])
        self.assertEqual(checkout.status, "initiated")
        self.assertEqual(checkout.last_event, "on_completed")

    @override_settings(
        MOYASAR_LIVE_MODE=True,
        MOYASAR_PUBLISHABLE_KEY="pk_live_publishable",
        MOYASAR_SECRET_KEY="sk_live_secret",
    )
    @patch("subscriptions.moyasar.MoyasarClient.fetch_payment")
    def test_return_verifies_and_activates_live_payment_once(self, fetch_payment):
        checkout = self._checkout(live_mode=True, created_by=self.manager)
        details = self._details(checkout)
        fetch_payment.return_value = details
        self.client.force_login(self.manager)
        return_url = reverse("subscriptions:moyasar_return")

        first = self.client.get(
            return_url,
            {"reference": checkout.merchant_reference, "id": details["id"]},
        )
        second = self.client.get(
            return_url,
            {"reference": checkout.merchant_reference, "id": details["id"]},
        )

        self.assertRedirects(first, reverse("dashboard:my_subscription"))
        self.assertRedirects(second, reverse("dashboard:my_subscription"))
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "paid")
        self.assertIsNotNone(checkout.subscription_id)
        self.assertEqual(SubscriptionPaymentOperation.objects.filter(method="moyasar").count(), 1)
        self.assertTrue(
            SubscriptionInvoice.objects.filter(operation_id=checkout.payment_operation_id).exists()
        )

    def test_amount_mismatch_is_rejected(self):
        checkout = self._checkout()

        with self.assertRaises(MoyasarVerificationError):
            apply_payment_details(
                checkout.pk,
                self._details(checkout, amount=49999),
                event_type="return",
            )

        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "initiated")
        self.assertIsNone(checkout.subscription_id)

    def test_webhook_rejects_invalid_secret(self):
        response = self.client.post(
            reverse("subscriptions:moyasar_webhook"),
            data=json.dumps({"secret_token": "wrong", "data": {}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    @override_settings(
        MOYASAR_LIVE_MODE=True,
        MOYASAR_PUBLISHABLE_KEY="pk_live_publishable",
        MOYASAR_SECRET_KEY="sk_live_secret",
    )
    def test_signed_webhook_is_idempotent(self):
        checkout = self._checkout(live_mode=True)
        body = {
            "id": "event-1",
            "type": "payment_paid",
            "secret_token": "webhook-test-secret",
            "live": True,
            "data": self._details(checkout),
        }

        first = self.client.post(
            reverse("subscriptions:moyasar_webhook"),
            data=json.dumps(body),
            content_type="application/json",
        )
        second = self.client.post(
            reverse("subscriptions:moyasar_webhook"),
            data=json.dumps(body),
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        checkout.refresh_from_db()
        self.assertIsNotNone(checkout.payment_operation_id)
        self.assertEqual(SubscriptionPaymentOperation.objects.filter(method="moyasar").count(), 1)

    @override_settings(
        MOYASAR_LIVE_MODE=True,
        MOYASAR_PUBLISHABLE_KEY="pk_live_publishable",
        MOYASAR_SECRET_KEY="sk_live_secret",
    )
    def test_webhook_uses_environment_from_event_envelope(self):
        checkout = self._checkout(live_mode=True)
        details = self._details(checkout)
        details.pop("live")

        response = self.client.post(
            reverse("subscriptions:moyasar_webhook"),
            data=json.dumps(
                {
                    "type": "payment_paid",
                    "secret_token": "webhook-test-secret",
                    "live": True,
                    "data": details,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "paid")
        self.assertIsNotNone(checkout.payment_operation_id)


@override_settings(
    DEBUG=False,
    RUNNING_TESTS=True,
    MOYASAR_ENABLED=True,
    MOYASAR_LIVE_MODE=True,
    MOYASAR_ACTIVATE_TEST_PAYMENTS=False,
    MOYASAR_API_BASE_URL="https://api.moyasar.com/v1",
    MOYASAR_PUBLISHABLE_KEY="pk_live_publishable",
    MOYASAR_SECRET_KEY="sk_live_secret",
    MOYASAR_WEBHOOK_SECRET="webhook-test-secret",
    MOYASAR_CALLBACK_BASE_URL="https://school-display.com",
    MOYASAR_HTTP_TIMEOUT_SECONDS=5,
    SUBSCRIPTION_ACCESS_CACHE_TTL=300,
)
class PaidAccessIsGrantedWithoutStaffTests(TestCase):
    """A completed card payment must open the product with zero human steps.

    These tests assert on *access* — the gate the customer actually feels —
    rather than only on the rows written, and they cover each of the three
    independent confirmation paths (return URL, webhook, reconciliation).
    """

    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة الدفع", slug="pay-school")
        self.plan = SubscriptionPlan.objects.create(
            code="pay-plan",
            name="الباقة السنوية",
            price=Decimal("500.00"),
            duration_days=365,
            max_screens=3,
        )
        self.manager = get_user_model().objects.create_user(
            username="paying_manager",
            password="StrongPass123!",
            email="pay@example.com",
        )
        profile = UserProfile.objects.create(
            user=self.manager,
            active_school=self.school,
            email_verified_at=timezone.now(),
        )
        profile.schools.add(self.school)
        SchoolSettings.objects.create(school=self.school, name=self.school.name)

    def _checkout(self, **overrides):
        defaults = dict(
            school=self.school,
            created_by=self.manager,
            plan=self.plan,
            request_type="new",
            starts_at=timezone.localdate(),
            amount=self.plan.price,
            currency="SAR",
            live_mode=True,
        )
        defaults.update(overrides)
        return MoyasarCheckout.objects.create(**defaults)

    def _paid_details(self, checkout, *, payment_id="pay_live_0000000000001"):
        return {
            "id": payment_id,
            "status": "paid",
            "amount": int(Decimal(checkout.amount) * 100),
            "currency": "SAR",
            "live": True,
            "metadata": {"merchant_reference": checkout.merchant_reference},
        }

    def _assert_locked_out(self):
        self.assertFalse(school_subscription_is_active(self.school.id))
        self.client.force_login(self.manager)
        response = self.client.get(reverse("dashboard:index"))
        self.assertRedirects(
            response,
            reverse("dashboard:my_subscription"),
            fetch_redirect_response=False,
        )

    def _assert_access_open(self):
        # Cached predicate — this is what the dashboard and display middleware read.
        self.assertTrue(school_subscription_is_active(self.school.id))
        self.assertEqual(school_max_screens(self.school.id), 3)
        self.school.refresh_from_db()
        self.assertTrue(self.school.is_active)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("dashboard:index")).status_code, 200)

    def _assert_no_staff_action_needed(self):
        """Nothing may be left sitting in a queue a human has to work."""
        self.assertFalse(
            SubscriptionRequest.objects.filter(
                school=self.school, status__in=["submitted", "under_review"]
            ).exists(),
            "الدفع الإلكتروني أنشأ طلبًا يحتاج اعتماد موظف",
        )
        subscription = SchoolSubscription.objects.get(school=self.school)
        self.assertEqual(subscription.status, "active")
        self.assertIsNotNone(subscription.ends_at, "الاشتراك بلا تاريخ انتهاء")
        self.assertEqual(
            subscription.ends_at,
            subscription.starts_at + timedelta(days=self.plan.duration_days),
        )
        operation = SubscriptionPaymentOperation.objects.get(school=self.school)
        self.assertEqual(operation.method, "moyasar")
        self.assertTrue(SubscriptionInvoice.objects.filter(operation=operation).exists())

    # ---- path 1: customer returns to the callback URL -------------------
    @patch("subscriptions.moyasar_views.MoyasarClient.fetch_payment")
    def test_return_url_opens_access_immediately(self, fetch_payment):
        checkout = self._checkout()
        fetch_payment.return_value = self._paid_details(checkout)
        self._assert_locked_out()

        self.client.force_login(self.manager)
        self.client.get(
            reverse("subscriptions:moyasar_return"),
            {"reference": checkout.merchant_reference, "id": "pay_live_0000000000001"},
        )

        self._assert_access_open()
        self._assert_no_staff_action_needed()

    # ---- path 2: customer closed the browser; only the webhook fires ----
    def test_webhook_alone_opens_access_when_customer_never_returns(self):
        checkout = self._checkout()
        self._assert_locked_out()
        self.client.logout()

        response = self.client.post(
            reverse("subscriptions:moyasar_webhook"),
            data=json.dumps(
                {
                    "type": "payment_paid",
                    "secret_token": "webhook-test-secret",
                    "data": self._paid_details(checkout),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self._assert_access_open()
        self._assert_no_staff_action_needed()

    # ---- path 3: no return, no webhook; the worker sweeps it up ---------
    @patch("subscriptions.moyasar.MoyasarClient.list_payments")
    @patch("subscriptions.moyasar.MoyasarClient.fetch_payment")
    @patch("subscriptions.moyasar.MoyasarClient.__init__", return_value=None)
    def test_reconciliation_worker_opens_access_with_no_callback_at_all(
        self, _init, fetch_payment, list_payments
    ):
        checkout = self._checkout()
        self._assert_locked_out()
        self.client.logout()

        # The checkout never learned its payment id, so the worker has to find
        # the payment by scanning Moyasar's ledger for the merchant reference.
        fetch_payment.side_effect = AssertionError("should scan, not fetch")
        list_payments.return_value = ([self._paid_details(checkout)], False)

        result = reconcile_pending_checkouts()

        self.assertEqual(result.activated, 1)
        self._assert_access_open()
        self._assert_no_staff_action_needed()

    # ---- money safety ---------------------------------------------------
    def test_a_tampered_amount_never_opens_access(self):
        checkout = self._checkout()
        tampered = self._paid_details(checkout)
        tampered["amount"] = 100  # 1.00 SAR instead of 500.00

        with self.assertRaises(MoyasarVerificationError):
            apply_payment_details(checkout.pk, tampered, event_type="return")

        self.assertFalse(school_subscription_is_active(self.school.id))
        self.assertFalse(SchoolSubscription.objects.filter(school=self.school).exists())

    def test_extra_screens_paid_at_checkout_raise_the_limit_at_once(self):
        checkout = self._checkout(extra_screens=2)
        self.assertEqual(school_max_screens(self.school.id), 0)

        apply_payment_details(checkout.pk, self._paid_details(checkout), event_type="return")

        # plan (3) + purchased (2) with no staff step in between.
        self.assertEqual(school_max_screens(self.school.id), 5)

    def test_renewal_extends_the_term_without_a_gap(self):
        today = timezone.localdate()
        current = SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
            starts_at=today - timedelta(days=350),
            ends_at=today + timedelta(days=5),
            status="active",
        )
        checkout = self._checkout(
            request_type="renewal",
            starts_at=current.ends_at + timedelta(days=1),
        )

        apply_payment_details(checkout.pk, self._paid_details(checkout), event_type="return")

        renewal = SchoolSubscription.objects.exclude(pk=current.pk).get(school=self.school)
        self.assertEqual(renewal.status, "active")
        self.assertEqual(renewal.starts_at, current.ends_at + timedelta(days=1))
        # Access holds continuously across the handover date.
        self.assertTrue(school_has_active_subscription(self.school.id, on_date=current.ends_at))
        self.assertTrue(school_has_active_subscription(self.school.id, on_date=renewal.starts_at))

    def test_new_same_plan_purchases_append_to_paid_coverage_once_per_payment(self):
        today = timezone.localdate()
        current = SchoolSubscription.objects.create(
            school=self.school, plan=self.plan,
            starts_at=today - timedelta(days=30), ends_at=today + timedelta(days=10),
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("subscriptions:moyasar_start"),
            {"request_type": "new", "plan_id": self.plan.pk},
        )
        checkout = MoyasarCheckout.objects.get(school=self.school)
        self.assertRedirects(
            response, reverse("subscriptions:moyasar_checkout", args=[checkout.merchant_reference]),
            fetch_redirect_response=False,
        )
        self.assertEqual(checkout.starts_at, current.ends_at + timedelta(days=1))

        # A separate checkout can quote the same start before either payment succeeds.
        second_checkout = self._checkout(starts_at=checkout.starts_at)
        first = apply_payment_details(checkout.pk, self._paid_details(checkout), event_type="return")
        self.assertEqual(first.subscription.starts_at, checkout.starts_at)
        self.assertFalse(first.subscription.is_active)
        second_payment = self._paid_details(second_checkout, payment_id="pay_live_0000000000002")
        second = apply_payment_details(second_checkout.pk, second_payment, event_type="webhook")
        replay = apply_payment_details(second_checkout.pk, second_payment, event_type="return")

        self.assertEqual(second.subscription.starts_at, first.subscription.ends_at + timedelta(days=1))
        self.assertEqual(replay.subscription_id, second.subscription_id)
        self.assertEqual(SchoolSubscription.objects.filter(school=self.school).count(), 3)
        self.assertEqual(SubscriptionPaymentOperation.objects.filter(school=self.school).count(), 2)
        current.refresh_from_db()
        self.assertEqual(current.ends_at, today + timedelta(days=10))

    def test_different_plan_begins_on_confirmation_even_with_a_running_plan(self):
        today = timezone.localdate()
        other_plan = SubscriptionPlan.objects.create(
            code="other-paid-plan", name="A different plan", price=100, duration_days=30,
        )
        SchoolSubscription.objects.create(
            school=self.school, plan=other_plan,
            starts_at=today - timedelta(days=20), ends_at=today + timedelta(days=10),
        )
        checkout = self._checkout(starts_at=today - timedelta(days=2))
        activated = apply_payment_details(checkout.pk, self._paid_details(checkout), event_type="return")

        self.assertEqual(activated.subscription.starts_at, today)
        self.assertTrue(activated.subscription.is_active)

    # ---- an outage must not turn a real payment into a void -------------
    @patch("subscriptions.moyasar.MoyasarClient.list_payments")
    @patch("subscriptions.moyasar.MoyasarClient.__init__", return_value=None)
    def test_payment_older_than_the_lookback_is_still_recovered(self, _init, list_payments):
        checkout = self._checkout()
        stale = timezone.now() - timedelta(hours=96)
        MoyasarCheckout.objects.filter(pk=checkout.pk).update(created_at=stale, updated_at=stale)
        list_payments.return_value = ([self._paid_details(checkout)], False)

        result = reconcile_pending_checkouts()

        # The sweep must actually ask Moyasar about it rather than write it off.
        self.assertTrue(list_payments.called)
        self.assertEqual(result.activated, 1)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "paid")
        self._assert_access_open()

    @patch("subscriptions.moyasar.MoyasarClient.list_payments")
    @patch("subscriptions.moyasar.MoyasarClient.__init__", return_value=None)
    def test_truly_abandoned_attempt_is_still_voided(self, _init, list_payments):
        checkout = self._checkout()
        stale = timezone.now() - timedelta(hours=96)
        MoyasarCheckout.objects.filter(pk=checkout.pk).update(created_at=stale, updated_at=stale)
        list_payments.return_value = ([], False)  # ledger has nothing for it

        result = reconcile_pending_checkouts()

        self.assertEqual(result.expired, 1)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "voided")
        self.assertFalse(school_subscription_is_active(self.school.id))

    @patch("subscriptions.moyasar.MoyasarClient.list_payments")
    @patch("subscriptions.moyasar.MoyasarClient.__init__", return_value=None)
    def test_a_failed_ledger_sweep_never_voids_anything(self, _init, list_payments):
        checkout = self._checkout()
        stale = timezone.now() - timedelta(hours=96)
        MoyasarCheckout.objects.filter(pk=checkout.pk).update(created_at=stale, updated_at=stale)
        list_payments.side_effect = RuntimeError("moyasar unreachable")

        result = reconcile_pending_checkouts()

        self.assertEqual(result.expired, 0)
        checkout.refresh_from_db()
        # Still open, so the next healthy tick can recover it.
        self.assertEqual(checkout.status, "initiated")

    # ---- gateway-side refunds must reach our own books ------------------
    def test_refund_issued_inside_moyasar_is_recorded_locally(self):
        checkout = self._checkout()
        apply_payment_details(checkout.pk, self._paid_details(checkout), event_type="return")
        operation = SubscriptionPaymentOperation.objects.get(school=self.school)

        refunded = self._paid_details(checkout)
        refunded["status"] = "refunded"
        refunded["refunded"] = 50000  # full amount, in halalas
        apply_payment_details(checkout.pk, refunded, event_type="webhook")

        refund = SubscriptionRefund.objects.get(operation=operation)
        self.assertEqual(refund.amount, Decimal("500.00"))
        self.assertEqual(refund.status, "completed")
        self.assertFalse(refund.revokes_access, "الاسترداد يجب ألا يوقف الخدمة تلقائيًا")
        # Access deliberately survives: revoking it stays a human decision.
        self.assertTrue(school_subscription_is_active(self.school.id))

    def test_repeated_refund_notifications_do_not_double_book(self):
        checkout = self._checkout()
        apply_payment_details(checkout.pk, self._paid_details(checkout), event_type="return")
        refunded = self._paid_details(checkout)
        refunded["status"] = "refunded"
        refunded["refunded"] = 50000

        apply_payment_details(checkout.pk, refunded, event_type="webhook")
        apply_payment_details(checkout.pk, refunded, event_type="webhook")
        apply_payment_details(checkout.pk, refunded, event_type="reconcile:fetch")

        self.assertEqual(SubscriptionRefund.objects.count(), 1)

    def test_partial_refund_then_the_rest_books_only_the_difference(self):
        checkout = self._checkout()
        apply_payment_details(checkout.pk, self._paid_details(checkout), event_type="return")

        partial = self._paid_details(checkout)
        partial["status"] = "refunded"
        partial["refunded"] = 20000  # 200.00 so far
        apply_payment_details(checkout.pk, partial, event_type="webhook")

        rest = dict(partial, refunded=50000)  # Moyasar reports the cumulative total
        apply_payment_details(checkout.pk, rest, event_type="webhook")

        amounts = sorted(r.amount for r in SubscriptionRefund.objects.all())
        self.assertEqual(amounts, [Decimal("200.00"), Decimal("300.00")])

    # ---- a lapsed row must not be revived with a dead end date ----------
    def test_paying_for_a_lapsed_term_gives_a_full_term_not_an_expired_one(self):
        today = timezone.localdate()
        lapsed = SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
            starts_at=today,
            ends_at=today - timedelta(days=1),
            status="expired",
        )
        checkout = self._checkout(starts_at=today)

        apply_payment_details(checkout.pk, self._paid_details(checkout), event_type="return")

        lapsed.refresh_from_db()
        self.assertEqual(lapsed.status, "active")
        self.assertEqual(lapsed.ends_at, today + timedelta(days=self.plan.duration_days))
        self._assert_access_open()


class MoyasarPaginationTests(TestCase):
    """``has_next`` gates whether a sweep counts as complete, so it must not
    report "end of ledger" for a shape it does not understand."""

    def test_known_shapes(self):
        cases = [
            ({"current_page": 1, "next_page": 2}, 1, True),
            ({"current_page": 2, "next_page": None}, 2, False),
            ({"current_page": 1, "total_pages": 3}, 1, True),
            ({"current_page": 3, "total_pages": 3}, 3, False),
        ]
        for meta, page, expected in cases:
            with self.subTest(meta=meta):
                self.assertIs(MoyasarClient._has_next_page(meta, requested_page=page), expected)

    def test_unrecognised_shapes_assume_more_pages(self):
        for meta in (None, {}, "nonsense", {"cursor": "abc"}):
            with self.subTest(meta=meta):
                self.assertTrue(MoyasarClient._has_next_page(meta, requested_page=1))

    @override_settings(
        MOYASAR_API_BASE_URL="https://api.moyasar.com/v1",
        MOYASAR_SECRET_KEY="sk_test_secret",
    )
    @patch("subscriptions.moyasar.MoyasarClient._get")
    def test_list_payments_uses_documented_created_filter_in_utc(self, get):
        get.return_value = {"payments": [], "meta": {"next_page": None}}
        created_after = datetime(
            2026,
            8,
            8,
            18,
            30,
            tzinfo=dt_timezone(timedelta(hours=3)),
        )

        MoyasarClient().list_payments(created_after=created_after, page=2)

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["created[gt]"], "2026-08-08 15:30:00")
        self.assertNotIn("created[gte]", params)
