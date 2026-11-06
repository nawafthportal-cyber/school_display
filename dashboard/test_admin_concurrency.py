"""These tests exercise real row locks; SQLite cannot validate this contract."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings

from core.models import School, SubscriptionPlan
from core.owner_safety import protect_owner_change
from subscriptions.models import SubscriptionInvoice, SubscriptionPaymentOperation, SubscriptionRequest
from subscriptions.request_processing import process_request


@skipUnless(connection.vendor == "postgresql", "Real PostgreSQL row locking is required")
@override_settings(TRANSACTIONAL_EMAIL_ENABLED=False)
class AdminConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(username="concurrent-owner")
        self.school = School.objects.create(name="Concurrent school", slug="concurrent-school")
        self.plan = SubscriptionPlan.objects.create(code="concurrent-plan", name="Plan", price=100, duration_days=30, max_screens=1)
        self.request = SubscriptionRequest.objects.create(school=self.school, plan=self.plan, created_by=self.owner, request_type="new", amount=100)

    def run_pair(self, actions, operation):
        barrier = Barrier(2)

        def worker(action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation(action)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, action) for action in actions]
            return [future.result(timeout=30) for future in futures]

    def transition(self, action):
        try:
            _obj, changed = process_request(request_id=self.request.pk, action=action, actor=self.owner)
            return "changed" if changed else "unchanged"
        except ValidationError:
            return "rejected"

    def test_two_approvals_commit_exactly_one_payment_and_invoice(self):
        results = self.run_pair(["approve", "approve"], self.transition)
        self.assertCountEqual(results, ["changed", "unchanged"])
        self.assertEqual(SubscriptionPaymentOperation.objects.count(), 1)
        self.assertEqual(SubscriptionInvoice.objects.count(), 1)

    def test_approval_and_rejection_cannot_commit_conflicting_states(self):
        self.run_pair(["approve", "reject"], self.transition)
        self.request.refresh_from_db()
        approved = self.request.status == "approved"
        self.assertIn(self.request.status, {"approved", "rejected"})
        self.assertEqual(SubscriptionPaymentOperation.objects.count(), int(approved))
        self.assertEqual(SubscriptionInvoice.objects.count(), int(approved))
        self.assertEqual(self.request.approved_subscription_id is not None, approved)

    def test_two_owner_demotions_cannot_remove_every_active_owner(self):
        second = get_user_model().objects.create_superuser(username="second-concurrent-owner")

        def demote(pk):
            try:
                with transaction.atomic():
                    protect_owner_change(pk, active=True, superuser=False)
                    get_user_model().objects.filter(pk=pk).update(is_superuser=False)
                return "demoted"
            except ValidationError:
                return "protected"

        results = self.run_pair([self.owner.pk, second.pk], demote)
        self.assertCountEqual(results, ["demoted", "protected"])
        self.assertEqual(get_user_model().objects.filter(is_active=True, is_superuser=True).count(), 1)
