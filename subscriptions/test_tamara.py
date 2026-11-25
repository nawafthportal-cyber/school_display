import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import jwt
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import School, SubscriptionPlan, UserProfile
from subscriptions.models import (
    SchoolSubscription,
    SubscriptionInvoice,
    SubscriptionPaymentOperation,
    TamaraCheckout,
)
from subscriptions.tamara import TamaraAPIError
from subscriptions.tamara_processing import activate_checkout, reconcile_pending_checkouts


TAMARA_TEST_SETTINGS = {
    "TAMARA_ENABLED": True,
    "TAMARA_API_BASE_URL": "https://api-sandbox.tamara.co",
    "TAMARA_API_TOKEN": "test-api-token",
    "TAMARA_NOTIFICATION_TOKEN": "test-notification-secret-at-least-32-bytes",
    "TAMARA_CALLBACK_BASE_URL": "https://school-display.com",
    "TAMARA_HTTP_TIMEOUT_SECONDS": 5,
    "TAMARA_ELIGIBILITY_TIMEOUT_SECONDS": 0.2,
    "TAMARA_CAPTURE_DIGITAL_ORDERS": True,
    "TAMARA_RECONCILIATION_BATCH_SIZE": 20,
}


@override_settings(**TAMARA_TEST_SETTINGS)
class TamaraCheckoutTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة تمارا", slug="tamara-school")
        self.plan = SubscriptionPlan.objects.create(
            code="tamara-plan",
            name="باقة تمارا",
            price=Decimal("500.00"),
            duration_days=365,
            max_screens=2,
        )
        self.user = get_user_model().objects.create_user(
            username="tamara_manager",
            password="StrongPass123!",
            email="manager@example.com",
            first_name="منصور",
            last_name="الغامدي",
        )
        profile = UserProfile.objects.create(
            user=self.user,
            active_school=self.school,
            mobile="0501234567",
            email_verified_at=timezone.now(),
        )
        profile.schools.add(self.school)
        self.client.force_login(self.user)

    def _checkout(self, *, order_id: str, status: str = "new") -> TamaraCheckout:
        return TamaraCheckout.objects.create(
            school=self.school,
            created_by=self.user,
            plan=self.plan,
            request_type="new",
            starts_at=timezone.localdate(),
            amount=Decimal("500.00"),
            tamara_order_id=order_id,
            checkout_id=f"checkout-{order_id}",
            checkout_url=f"https://checkout.tamara.co/session/{order_id}",
            status=status,
        )

    def _order_details(self, checkout: TamaraCheckout, *, status: str) -> dict:
        return {
            "order_id": checkout.tamara_order_id,
            "order_reference_id": checkout.merchant_reference,
            "total_amount": {"amount": 500, "currency": "SAR"},
            "status": status,
        }

    @patch("subscriptions.tamara.TamaraClient.create_checkout")
    @patch("subscriptions.tamara.TamaraClient.is_eligible", return_value=True)
    def test_start_checkout_stores_remote_ids_and_redirects(self, _eligible, create_checkout):
        create_checkout.return_value = {
            "order_id": "order-123",
            "checkout_id": "checkout-123",
            "checkout_url": "https://checkout.tamara.co/session/123",
        }

        response = self.client.post(
            reverse("subscriptions:tamara_start"),
            {"request_type": "new", "plan_id": self.plan.pk},
        )

        self.assertRedirects(
            response,
            "https://checkout.tamara.co/session/123",
            fetch_redirect_response=False,
        )
        checkout = TamaraCheckout.objects.get()
        self.assertEqual(checkout.tamara_order_id, "order-123")
        self.assertEqual(checkout.checkout_id, "checkout-123")
        self.assertEqual(checkout.status, "new")

    @patch("subscriptions.tamara.TamaraClient.create_checkout")
    @patch("subscriptions.tamara.TamaraClient.is_eligible", return_value=True)
    def test_new_same_plan_checkout_quotes_and_grants_a_future_term(self, _eligible, create_checkout):
        today = timezone.localdate()
        current = SchoolSubscription.objects.create(
            school=self.school, plan=self.plan,
            starts_at=today - timedelta(days=30), ends_at=today + timedelta(days=10),
        )
        create_checkout.return_value = {
            "order_id": "order-future-term", "checkout_id": "checkout-future-term",
            "checkout_url": "https://checkout.tamara.co/session/future-term",
        }
        self.client.post(
            reverse("subscriptions:tamara_start"),
            {"request_type": "new", "plan_id": self.plan.pk},
        )
        checkout = TamaraCheckout.objects.get()
        expected = current.ends_at + timedelta(days=1)
        self.assertEqual(checkout.starts_at, expected)
        self.assertEqual(
            create_checkout.call_args.args[0]["risk_assessment"]["education"]["start_date"],
            expected.strftime("%d-%m-%Y"),
        )

        paid = activate_checkout(checkout.pk, status="authorised", event_type="webhook")
        captured = activate_checkout(checkout.pk, status="captured", event_type="return")
        self.assertEqual(paid.subscription.starts_at, expected)
        self.assertEqual(captured.subscription_id, paid.subscription_id)
        self.assertEqual(SchoolSubscription.objects.filter(school=self.school).count(), 2)

    @patch("subscriptions.tamara_processing.TamaraClient.capture_order")
    @patch("subscriptions.tamara_processing.TamaraClient.authorise_order")
    @patch("subscriptions.tamara_processing.TamaraClient.get_order")
    def test_approved_webhook_verifies_activates_and_captures(
        self,
        get_order,
        authorise_order,
        capture_order,
    ):
        checkout = TamaraCheckout.objects.create(
            school=self.school,
            created_by=self.user,
            plan=self.plan,
            request_type="new",
            starts_at=timezone.localdate(),
            amount=Decimal("500.00"),
            tamara_order_id="order-456",
            checkout_id="checkout-456",
            checkout_url="https://checkout.tamara.co/session/456",
            status="new",
        )
        get_order.side_effect = [
            {
                "order_id": "order-456",
                "order_reference_id": checkout.merchant_reference,
                "total_amount": {"amount": 500, "currency": "SAR"},
                "status": "approved",
            },
            {
                "order_id": "order-456",
                "order_reference_id": checkout.merchant_reference,
                "total_amount": {"amount": 500, "currency": "SAR"},
                "status": "authorised",
            },
            {
                "order_id": "order-456",
                "order_reference_id": checkout.merchant_reference,
                "total_amount": {"amount": 500, "currency": "SAR"},
                "status": "fully_captured",
            },
        ]
        authorise_order.return_value = {"status": "authorised"}
        capture_order.return_value = {"status": "fully_captured"}
        token = jwt.encode(
            {"iss": "Tamara", "iat": int(timezone.now().timestamp()), "exp": int(timezone.now().timestamp()) + 300},
            TAMARA_TEST_SETTINGS["TAMARA_NOTIFICATION_TOKEN"],
            algorithm="HS256",
        )

        response = self.client.post(
            reverse("subscriptions:tamara_webhook"),
            data=json.dumps(
                {
                    "order_id": "order-456",
                    "order_reference_id": checkout.merchant_reference,
                    "order_number": checkout.merchant_reference,
                    "event_type": "order_approved",
                    "data": [],
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "captured")
        self.assertIsNotNone(checkout.subscription_id)
        self.assertIsNotNone(checkout.payment_operation_id)
        self.assertTrue(
            SubscriptionPaymentOperation.objects.filter(
                pk=checkout.payment_operation_id,
                method="tamara",
            ).exists()
        )
        self.assertTrue(SubscriptionInvoice.objects.filter(operation=checkout.payment_operation).exists())

    @patch("subscriptions.tamara_processing.TamaraClient.capture_order")
    @patch("subscriptions.tamara_processing.TamaraClient.get_order")
    def test_capture_conflict_rechecks_remote_status_and_marks_captured(
        self,
        get_order,
        capture_order,
    ):
        checkout = self._checkout(order_id="order-already-captured", status="authorised")
        get_order.side_effect = [
            self._order_details(checkout, status="authorised"),
            self._order_details(checkout, status="fully_captured"),
        ]
        capture_order.side_effect = TamaraAPIError("conflict", status_code=409)

        result = reconcile_pending_checkouts()

        self.assertEqual(result.checked, 1)
        self.assertEqual(result.activated, 1)
        self.assertEqual(result.captured, 1)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "captured")
        self.assertEqual(SubscriptionPaymentOperation.objects.filter(method="tamara").count(), 1)

    @patch("subscriptions.tamara_processing.TamaraClient.get_order")
    def test_activation_reuses_existing_tamara_payment_operation(self, get_order):
        checkout = self._checkout(order_id="order-existing-operation")
        subscription = SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
            starts_at=checkout.starts_at,
            status="active",
            notes="Existing subscription",
        )
        operation = SubscriptionPaymentOperation.objects.create(
            school=self.school,
            subscription=subscription,
            plan=self.plan,
            amount=checkout.amount,
            method="tamara",
            source="request",
            created_by=self.user,
            note=f"Tamara {checkout.merchant_reference} / {checkout.tamara_order_id}",
        )
        get_order.return_value = self._order_details(checkout, status="fully_captured")

        result = reconcile_pending_checkouts()

        self.assertEqual(result.checked, 1)
        self.assertEqual(result.activated, 1)
        checkout.refresh_from_db()
        self.assertEqual(checkout.payment_operation_id, operation.pk)
        self.assertEqual(SubscriptionPaymentOperation.objects.filter(method="tamara").count(), 1)

    @patch("subscriptions.tamara_processing.TamaraClient.get_order")
    def test_amount_mismatch_never_activates(self, get_order):
        checkout = TamaraCheckout.objects.create(
            school=self.school,
            created_by=self.user,
            plan=self.plan,
            request_type="new",
            amount=Decimal("500.00"),
            tamara_order_id="order-mismatch",
            status="new",
        )
        get_order.return_value = {
            "order_id": "order-mismatch",
            "order_reference_id": checkout.merchant_reference,
            "total_amount": {"amount": 1, "currency": "SAR"},
            "status": "fully_captured",
        }

        response = self.client.get(
            reverse("subscriptions:tamara_success"),
            {"reference": checkout.merchant_reference},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SchoolSubscription.objects.filter(school=self.school).exists())
        self.assertFalse(SubscriptionPaymentOperation.objects.filter(method="tamara").exists())

    def test_subscription_page_offers_tamara(self):
        response = self.client.get(reverse("dashboard:my_subscription"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["tamara_available"])
        self.assertContains(response, reverse("subscriptions:tamara_start"), count=1)
        self.assertContains(response, 'id="tamara-plan-id"')
        self.assertContains(response, 'img/tamara-app-icon.png')

    @override_settings(TAMARA_ENABLED=False)
    def test_subscription_page_shows_tamara_pending_without_checkout(self):
        response = self.client.get(reverse("dashboard:my_subscription"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["tamara_available"])
        self.assertContains(response, 'data-payment-method="tamara"')
        self.assertContains(response, 'img/tamara-app-icon.png')
        self.assertNotContains(response, reverse("subscriptions:tamara_start"))

    def test_subscription_page_shows_tamara_checkout_status(self):
        checkout = self._checkout(order_id="order-visible")

        response = self.client.get(reverse("dashboard:my_subscription"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, checkout.merchant_reference[-8:])
        self.assertContains(response, "تمارا")
        self.assertContains(response, checkout.get_status_display())
        self.assertContains(response, checkout.checkout_url)

    def test_invalid_webhook_token_is_rejected(self):
        response = self.client.post(
            reverse("subscriptions:tamara_webhook"),
            data="{}",
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer invalid",
        )

        self.assertEqual(response.status_code, 403)

    def test_signed_webhook_for_other_platform_is_acknowledged_without_payment(self):
        token = jwt.encode(
            {"iss": "Tamara", "iat": int(timezone.now().timestamp()), "exp": int(timezone.now().timestamp()) + 300},
            TAMARA_TEST_SETTINGS["TAMARA_NOTIFICATION_TOKEN"],
            algorithm="HS256",
        )
        response = self.client.post(
            reverse("subscriptions:tamara_webhook"),
            data=json.dumps({
                "order_id": "11111111-1111-1111-1111-111111111111",
                "order_reference_id": "TWQ-OTHER-PLATFORM",
                "event_type": "order_approved",
            }),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ignored"], "other_platform")
        self.assertFalse(TamaraCheckout.objects.exists())

    @patch("subscriptions.tamara_processing.TamaraClient.get_order")
    def test_reconciliation_recovers_payment_older_than_three_days(self, get_order):
        checkout = self._checkout(order_id="order-old-paid")
        stale = timezone.now() - timedelta(days=10)
        TamaraCheckout.objects.filter(pk=checkout.pk).update(
            created_at=stale,
            updated_at=stale,
        )
        get_order.return_value = self._order_details(checkout, status="fully_captured")

        result = reconcile_pending_checkouts()

        self.assertEqual(result.checked, 1)
        self.assertEqual(result.activated, 1)
        self.assertEqual(result.captured, 1)
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, "captured")
        self.assertIsNotNone(checkout.payment_operation_id)

    @patch("subscriptions.tamara_processing.TamaraClient.get_order")
    def test_open_checkout_rotates_and_does_not_starve_later_orders(self, get_order):
        first = self._checkout(order_id="order-still-new-1")
        second = self._checkout(order_id="order-still-new-2")
        old = timezone.now() - timedelta(days=2)
        TamaraCheckout.objects.filter(pk=first.pk).update(updated_at=old)
        TamaraCheckout.objects.filter(pk=second.pk).update(updated_at=old + timedelta(seconds=1))
        details_by_id = {
            first.tamara_order_id: self._order_details(first, status="new"),
            second.tamara_order_id: self._order_details(second, status="new"),
        }
        get_order.side_effect = lambda order_id: details_by_id[order_id]

        reconcile_pending_checkouts(limit=1)
        reconcile_pending_checkouts(limit=1)

        self.assertEqual(
            [call.args[0] for call in get_order.call_args_list],
            [first.tamara_order_id, second.tamara_order_id],
        )

    @patch("subscriptions.tamara_processing.TamaraClient.get_order")
    def test_reconciliation_mismatch_does_not_block_next_paid_order(self, get_order):
        mismatched = self._checkout(order_id="order-mismatched")
        paid = self._checkout(order_id="order-valid-paid")
        mismatched_details = self._order_details(mismatched, status="fully_captured")
        mismatched_details.pop("order_reference_id")
        details_by_id = {
            mismatched.tamara_order_id: mismatched_details,
            paid.tamara_order_id: self._order_details(paid, status="fully_captured"),
        }
        get_order.side_effect = lambda order_id: details_by_id[order_id]

        result = reconcile_pending_checkouts(limit=2)

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.checked, 1)
        self.assertEqual(result.activated, 1)
        mismatched.refresh_from_db()
        paid.refresh_from_db()
        self.assertEqual(mismatched.status, "new")
        self.assertEqual(mismatched.last_event, "reconcile_mismatch")
        self.assertTrue(mismatched.error_message)
        self.assertEqual(paid.status, "captured")
        self.assertIsNotNone(paid.payment_operation_id)
