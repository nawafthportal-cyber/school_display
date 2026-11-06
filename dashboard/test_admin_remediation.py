import io
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from core.models import DisplayScreen, School, SubscriptionPlan, UserProfile
from dashboard.forms import SystemEmployeeUpdateForm, SystemUserUpdateForm
from mailcenter.models import MailMessage
from mailcenter.reconciliation import delivery_review_queryset, reconcile_delivery
from mailcenter.services import sync_message_content
from schedule.models import SchoolSettings
from subscriptions.models import MoyasarCheckout, SchoolSubscription, SubscriptionInvoice, SubscriptionPaymentOperation, SubscriptionRequest
from subscriptions.receipt_storage import AuthenticatedCloudReceipts, PrivateReceiptStorage
from subscriptions.request_processing import process_request


@override_settings(TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False, TRANSACTIONAL_EMAIL_ENABLED=False,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AdminRemediationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_superuser(username="owner", email="owner@example.test")
        self.school = School.objects.create(name="School", slug="remediation-school")
        self.plan = SubscriptionPlan.objects.create(name="Plan", code="remediation-plan", price=100, duration_days=30, max_screens=2)
        self.client.force_login(self.owner)

    def user_payload(self, **changes):
        data = {"username": self.owner.username, "email": self.owner.email, "is_active": "on", "is_staff": "on", "is_superuser": "on"}
        data.update(changes)
        return data

    def request(self):
        return SubscriptionRequest.objects.create(school=self.school, plan=self.plan, created_by=self.owner,
                                                  request_type="new", amount=60, requested_starts_at=timezone.localdate())

    def test_last_owner_cannot_be_demoted_or_disabled_via_edit(self):
        for field in ("is_superuser", "is_active"):
            response = self.client.post(reverse("dashboard:system_user_edit", args=[self.owner.pk]), self.user_payload(**{field: ""}))
            self.assertEqual(response.status_code, 200)
            self.owner.refresh_from_db()
            self.assertTrue(self.owner.is_active and self.owner.is_superuser)

    def test_another_active_owner_allows_authorized_demotion(self):
        get_user_model().objects.create_superuser(username="second-owner")
        form = SystemUserUpdateForm(self.user_payload(is_superuser=""), instance=self.owner)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_superuser)

    def test_password_policy_applies_to_user_and_employee_updates(self):
        form = SystemUserUpdateForm(self.user_payload(new_password1="12345678", new_password2="12345678"), instance=self.owner)
        self.assertFalse(form.is_valid())
        employee = get_user_model().objects.create_user(username="employee", is_staff=True)
        form = SystemEmployeeUpdateForm({"username": employee.username, "role": "support", "permissions": ["dashboard.view"],
                                         "new_password1": "12345678", "new_password2": "12345678", "is_active": "on"}, instance=employee)
        self.assertFalse(form.is_valid())

    def test_password_policy_checks_updated_username(self):
        new_username = "updated-identity-928471"
        form = SystemUserUpdateForm(self.user_payload(username=new_username, new_password1=new_username, new_password2=new_username), instance=self.owner)
        self.assertFalse(form.is_valid())

    @override_settings(USE_CLOUD_STORAGE=True, CLOUDINARY_STORAGE={})
    def test_legacy_receipt_conversion_preserves_identity_and_content(self):
        import cloudinary.api
        req = self.request()
        req.receipt_image = "receipts/existing"
        req.save(update_fields=["receipt_image"])
        original = {"asset_id": "original-asset", "etag": "original-hash", "bytes": 1024, "secure_url": "https://res.cloudinary.com/test/image/upload/receipt.png"}
        with patch("cloudinary.api.resource", side_effect=[cloudinary.api.NotFound("missing"), original, original, cloudinary.api.NotFound("missing")]), patch("cloudinary.uploader.rename", return_value=original) as rename, patch("subscriptions.management.commands.secure_subscription_receipts.requests.get", return_value=Mock(content=b"x"*1024)), patch("subscriptions.receipt_storage.AuthenticatedCloudReceipts._open", return_value=ContentFile(b"x"*1024)):
            output = io.StringIO()
            call_command("secure_subscription_receipts", apply=True, stdout=output)
        self.assertIn('"secured": 1', output.getvalue())
        self.assertEqual(rename.call_args.kwargs["to_type"], "authenticated")
        self.assertTrue(rename.call_args.kwargs["invalidate"])
        req.refresh_from_db()
        self.assertEqual(req.receipt_image.name, "receipts/existing")

    @override_settings(USE_CLOUD_STORAGE=True, CLOUDINARY_STORAGE={})
    def test_legacy_receipt_conversion_rejects_content_mismatch(self):
        import cloudinary.api
        req = self.request()
        req.receipt_image = "receipts/existing"
        req.save(update_fields=["receipt_image"])
        original = {"asset_id": "original-asset", "etag": "original-hash", "bytes": 1024, "secure_url": "https://res.cloudinary.com/test/image/upload/receipt.png"}
        with patch("cloudinary.api.resource", side_effect=[cloudinary.api.NotFound("missing"), original, dict(original, etag="changed")]), patch("cloudinary.uploader.rename", return_value=original), patch("subscriptions.management.commands.secure_subscription_receipts.requests.get", return_value=Mock(content=b"x"*1024)):
            with self.assertRaises(CommandError):
                call_command("secure_subscription_receipts", apply=True, stdout=io.StringIO())

    @override_settings(DEBUG=False, TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False)
    def test_deployment_check_rejects_disabled_privileged_policy(self):
        from core.deployment_checks import privileged_two_factor_policy
        self.assertEqual([error.id for error in privileged_two_factor_policy(None)], ["core.E001"])

    def test_approval_creates_one_payment_and_invoice_on_repeated_submission(self):
        req = self.request()
        for _ in range(2):
            process_request(request_id=req.pk, action="approve", actor=self.owner)
        self.assertEqual(SubscriptionPaymentOperation.objects.count(), 1)
        op = SubscriptionPaymentOperation.objects.get(subscription_request=req)
        self.assertEqual(op.amount, Decimal("60"))
        self.assertTrue(SubscriptionInvoice.objects.get(operation=op).html_snapshot)

    def test_cancelled_subscription_conflict_preserves_all_existing_records(self):
        sub = SchoolSubscription.objects.create(school=self.school, plan=self.plan, starts_at=timezone.localdate(), status="cancelled")
        req = self.request()
        with self.assertRaises(ValidationError):
            process_request(request_id=req.pk, action="approve", actor=self.owner)
        sub.refresh_from_db(); req.refresh_from_db()
        self.assertEqual(sub.status, "cancelled")
        self.assertEqual(req.status, "submitted")
        self.assertFalse(SubscriptionPaymentOperation.objects.exists())

    def test_payment_failure_rolls_back_approval_and_new_subscription(self):
        req = self.request()
        with patch("subscriptions.models.SubscriptionPaymentOperation.objects.get_or_create", side_effect=RuntimeError("failure")):
            response = self.client.post(reverse("dashboard:system_subscription_request_detail", args=[req.pk]), {"action": "approve"})
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, "submitted")
        self.assertFalse(SchoolSubscription.objects.exists())
        self.assertFalse(SubscriptionPaymentOperation.objects.exists())

    def test_invoice_failure_rolls_back_the_payment(self):
        req = self.request()
        with patch("subscriptions.invoicing.build_invoice_from_operation", side_effect=RuntimeError("invoice failure")), patch("subscriptions.request_processing.build_invoice_from_operation", side_effect=RuntimeError("invoice failure")):
            with self.assertRaises(RuntimeError):
                process_request(request_id=req.pk, action="approve", actor=self.owner)
        req.refresh_from_db()
        self.assertEqual(req.status, "submitted")
        self.assertFalse(SchoolSubscription.objects.exists())
        self.assertFalse(SubscriptionPaymentOperation.objects.exists())

    def test_stale_reject_cannot_overwrite_approved_request(self):
        req = self.request()
        stale = SubscriptionRequest.objects.get(pk=req.pk)
        process_request(request_id=req.pk, action="approve", actor=self.owner)
        with patch("dashboard.views_billing.get_object_or_404", return_value=stale):
            self.client.post(reverse("dashboard:system_subscription_request_detail", args=[req.pk]), {"action": "reject"})
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")
        self.assertEqual(SubscriptionPaymentOperation.objects.count(), 1)

    def test_edit_note_preserves_discounted_payment_invoice_and_gateway_amount(self):
        sub = SchoolSubscription.objects.create(school=self.school, plan=self.plan, starts_at=timezone.localdate(), status="active")
        op = SubscriptionPaymentOperation.objects.create(school=self.school, subscription=sub, plan=self.plan, amount=60,
                                                         method="moyasar", source="request", created_by=self.owner)
        checkout = MoyasarCheckout.objects.create(school=self.school, plan=self.plan, request_type="new", amount=60,
                                                   status="captured", payment_operation=op, subscription=sub)
        original_invoice = op.invoice.html_snapshot
        response = self.client.post(reverse("dashboard:system_subscription_edit", args=[sub.pk]), {
            "school": self.school.pk, "plan": self.plan.pk, "starts_at": sub.starts_at.isoformat(),
            "status": "active", "payment_method": "bank_transfer", "notes": "updated note",
        })
        self.assertEqual(response.status_code, 302)
        sub.refresh_from_db(); op.refresh_from_db(); checkout.refresh_from_db()
        self.assertEqual(sub.notes, "updated note")
        self.assertEqual((op.amount, op.method, op.invoice.amount, checkout.amount), (Decimal("60"), "moyasar", Decimal("60"), Decimal("60")))
        self.assertEqual(op.invoice.html_snapshot, original_invoice)

    def test_future_subscription_is_excluded_from_active_plan_count(self):
        SchoolSubscription.objects.create(school=self.school, plan=self.plan, starts_at=timezone.localdate()+timedelta(days=7), status="active")
        response = self.client.get(reverse("dashboard:system_plans_list"))
        self.assertEqual(next(p for p in response.context["plans"] if p.pk == self.plan.pk).active_subscriptions, 0)

    def test_administrative_suspend_blocks_tv_and_api_and_can_be_reversed(self):
        SchoolSubscription.objects.create(school=self.school, plan=self.plan, starts_at=timezone.localdate(), status="active")
        SchoolSettings.objects.create(school=self.school, name=self.school.name)
        profile = UserProfile.objects.create(user=self.owner, active_school=self.school)
        profile.schools.add(self.school)
        screen = DisplayScreen.objects.create(school=self.school, name="Test TV")
        url = f"/s/{screen.short_code}/?preview=1"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.school.display_suspended = True; self.school.save(update_fields=["display_suspended"])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(f"/api/display/snapshot/{screen.token}/").status_code, 403)
        status = self.client.get(f"/api/display/status/{screen.token}/")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json(), {"reload": True, "fetch_required": False, "school_suspended": True})
        self.school.display_suspended = False; self.school.save(update_fields=["display_suspended"])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(SchoolSubscription.objects.get(school=self.school).is_active)

    @override_settings(USE_CLOUD_STORAGE=False)
    def test_private_receipt_requires_school_or_platform_permission(self):
        image = io.BytesIO(); Image.new("RGB", (3, 3), "white").save(image, format="PNG")
        req = self.request()
        with tempfile.TemporaryDirectory() as root, override_settings(PRIVATE_RECEIPT_ROOT=root):
            req.receipt_image.save("receipt.png", SimpleUploadedFile("receipt.png", image.getvalue(), "image/png"))
            url = req.receipt_image.url
            self.assertTrue(url.startswith("/dashboard/subscription-receipts/"))
            owner_response = self.client.get(url)
            self.assertEqual(owner_response.status_code, 200)
            self.assertEqual(owner_response["Content-Type"], "image/png")
            self.assertIn("no-store", owner_response["Cache-Control"])
            owner_response.close()
            self.client.logout()
            self.assertEqual(self.client.get(url).status_code, 302)
            stranger = get_user_model().objects.create_user(username="stranger")
            self.client.force_login(stranger)
            self.assertEqual(self.client.get(url).status_code, 404)
            profile = UserProfile.objects.create(user=stranger, active_school=self.school)
            profile.schools.add(self.school)
            authorized_response = self.client.get(url)
            self.assertEqual(authorized_response.status_code, 200)
            authorized_response.close()

    @override_settings(USE_CLOUD_STORAGE=True, CLOUDINARY_STORAGE={})
    def test_cloud_upload_is_authenticated_and_field_url_never_contains_signature(self):
        with patch("cloudinary.uploader.upload", return_value={"public_id": "private/receipt"}) as upload:
            self.assertEqual(AuthenticatedCloudReceipts()._save("folder/receipt.png", io.BytesIO(b"image")), "private/receipt")
        self.assertEqual(upload.call_args.kwargs["type"], "authenticated")
        self.assertNotIn("cloudinary", PrivateReceiptStorage().url("private/receipt"))

    def test_provider_reconciliation_updates_status_without_sending_mail(self):
        msg = MailMessage.objects.create(direction="outbound", status="sent", provider_id="test-id")
        with patch("mailcenter.services.retrieve_email", return_value={"last_event": "delivered", "subject": "test"}):
            self.assertTrue(sync_message_content(msg))
        msg.refresh_from_db()
        self.assertEqual(msg.status, "delivered")
        self.assertIsNotNone(msg.delivery_checked_at)

    def test_provider_sent_cannot_downgrade_a_terminal_status(self):
        msg = MailMessage.objects.create(direction="outbound", status="bounced", provider_id="test-id")
        with patch("mailcenter.services.retrieve_email", return_value={"last_event": "sent"}):
            sync_message_content(msg)
        msg.refresh_from_db(); self.assertEqual(msg.status, "bounced")

    def test_unknown_provider_result_is_not_treated_as_delivered(self):
        msg = MailMessage.objects.create(direction="outbound", status="sent", provider_id="old-id")
        MailMessage.objects.filter(pk=msg.pk).update(created_at=timezone.now()-timedelta(days=2))
        with patch("mailcenter.reconciliation.sync_message_content", return_value=False), patch("mailcenter.reconciliation.time.sleep"):
            result = reconcile_delivery(limit=10, apply=True)
        self.assertEqual(result["unavailable"], 1)
        msg.refresh_from_db(); self.assertEqual(msg.status, "sent")
        self.assertTrue(delivery_review_queryset().filter(pk=msg.pk).exists())

    def test_provider_failure_stops_batch_before_worker_heartbeat_is_delayed(self):
        for number in range(2):
            msg = MailMessage.objects.create(direction="outbound", status="sent", provider_id=f"old-{number}")
            MailMessage.objects.filter(pk=msg.pk).update(created_at=timezone.now()-timedelta(days=2))
        with patch("mailcenter.reconciliation.sync_message_content", return_value=False) as sync:
            result = reconcile_delivery(limit=100, apply=True)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(result["checked"], 1)
