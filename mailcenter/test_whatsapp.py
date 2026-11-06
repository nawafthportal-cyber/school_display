from django.contrib.auth import get_user_model
from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import SystemEmployeeProfile, UserProfile

from .models import MailMessage
from .test_subscription_recipients import SubscriptionRecipientFixtures
from .whatsapp import whatsapp_phone_number


class WhatsAppPhoneTests(SimpleTestCase):
    def test_saudi_and_explicit_international_formats(self):
        for raw in ("0501234567", "501234567", "966501234567", "+966 50 123 4567", "00966501234567", "٠٥٠١٢٣٤٥٦٧", "۰۵۰۱۲۳۴۵۶۷"):
            with self.subTest(raw=raw):
                self.assertEqual(whatsapp_phone_number(raw), "966501234567")
        self.assertEqual(whatsapp_phone_number("+1 (202) 555-0123"), "12025550123")
        self.assertEqual(whatsapp_phone_number("0012025550123"), "12025550123")

    def test_invalid_and_ambiguous_numbers_are_not_linked(self):
        for raw in (None, "", "1234", "050123456", "05012345678", "abc0501234567", "+0501234567", "+966112345678", "2025550123", "javascript:alert(1)", "+1+2025550123", "+1234567890123456"):
            with self.subTest(raw=raw):
                self.assertIsNone(whatsapp_phone_number(raw))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WhatsAppPageTests(SubscriptionRecipientFixtures, TestCase):
    def setUp(self):
        self.create_recipients()
        self.owner = get_user_model().objects.create_superuser(username="wa_owner", email="owner@example.com", password="StrongPass123!")
        self.client.force_login(self.owner)
        UserProfile.objects.filter(user__username="manager_active").update(mobile="٠٥٠١٢٣٤٥٦٧")

    def test_page_has_categories_message_and_normalized_manager_number(self):
        response = self.client.get(reverse("dashboard:system_school_whatsapp"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-wa-phone="966501234567"')
        self.assertContains(response, "المدارس التي توقفت بعد التجربة المجانية")
        self.assertContains(response, 'id="whatsappMessage"')
        self.assertContains(response, "الإرسال يدويًا في واتساب")
        self.assertContains(response, "لا يوجد رقم جوال صالح")
        self.assertEqual(len(response.context["school_cards"]), len(self.schools))
        self.assertFalse(MailMessage.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_only_active_school_managers_are_listed_and_shared_numbers_are_deduplicated(self):
        for username, phone, flags in (("duplicate", "+966501234567", {}), ("inactive", "0509999999", {"is_active": False}),
                                       ("staff", "0508888888", {"is_staff": True}), ("invalid", "123", {}),
                                       ("unlinked", "0507777777", {})):
            user = get_user_model().objects.create_user(username=username, **flags)
            profile = UserProfile.objects.create(user=user, mobile=phone)
            if username != "unlinked":
                profile.schools.add(self.schools["active"])
        response = self.client.get(reverse("dashboard:system_school_whatsapp"))
        active = next(card for card in response.context["school_cards"] if card["school"].pk == self.schools["active"].pk)
        self.assertEqual([contact["phone"] for contact in active["contacts"]], ["966501234567"])
        self.assertNotContains(response, 'data-wa-phone="966509999999"')
        self.assertNotContains(response, 'data-wa-phone="966508888888"')
        self.assertNotContains(response, 'data-wa-phone="966507777777"')

    def test_school_managers_and_unprivileged_staff_cannot_access_contacts(self):
        self.client.force_login(get_user_model().objects.get(username="manager_active"))
        response = self.client.get(reverse("dashboard:system_school_whatsapp"))
        self.assertIn(response.status_code, (302, 403))
        self.assertNotContains(response, "966501234567", status_code=response.status_code)
        for permission, expected_status in (("mail.view", 403), ("mail.manage", 200)):
            user = get_user_model().objects.create_user(username=f"staff_{expected_status}", is_staff=True)
            SystemEmployeeProfile.objects.create(user=user, role="custom", permission_keys=[permission])
            self.client.force_login(user)
            response = self.client.get(reverse("dashboard:system_school_whatsapp"))
            self.assertEqual(response.status_code, expected_status)
            if expected_status == 403:
                self.assertNotContains(response, "966501234567", status_code=403)

    def test_post_is_not_a_send_endpoint(self):
        response = self.client.post(reverse("dashboard:system_school_whatsapp"), {"message": "نص تجريبي"})
        self.assertEqual(response.status_code, 405)
        self.assertFalse(MailMessage.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_mail_pages_link_to_manual_whatsapp_page(self):
        for name in ("system_mail_list", "system_mail_compose"):
            response = self.client.get(reverse(f"dashboard:{name}"))
            self.assertContains(response, reverse("dashboard:system_school_whatsapp"))
