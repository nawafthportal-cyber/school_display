from __future__ import annotations

import re
from pathlib import Path
from django.conf import settings
from datetime import date, time, timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.client import RequestFactory
from django.urls import resolve, reverse
from django.utils import timezone
import pyotp

from core.models import (
    DisplayScreen,
    School,
    ScreenOutage,
    SubscriptionPlan,
    SupportTicket,
    SystemEmployeeProfile,
    UserProfile,
    UserTwoFactorAuth,
)
from core import occasions
from dashboard import views
from notices.models import Announcement
from core.system_access import ROLE_FINANCE, ROLE_SUPPORT, role_permissions
from core.display_presence import latest_display_presence, touch_display_presence
from display.services.device_binding import bind_device_atomic
from display.ws_groups import school_group_name
from core.error_views import permission_denied
from core.tenant_access import authorized_active_school
from core.two_factor import (
    consume_second_factor,
    decrypt_secret,
    enable_two_factor,
    ensure_setup_config,
)
from dashboard.access import get_active_school_or_redirect
from dashboard.decorators import manager_required, superuser_required, system_staff_required
from dashboard.forms import ScreenDisplayCustomizationForm, SubscriptionNewRequestForm
from subscriptions.models import (
    MoyasarCheckout,
    SubscriptionInvoice,
    SubscriptionPaymentOperation,
    SchoolSubscription,
    SubscriptionRequest,
    SubscriptionScreenAddon,
)
from schedule.models import ClassLesson, DaySchedule, Period, SchoolClass, SchoolSettings, Subject, Teacher
from notices.models import Announcement, EmergencyAlert
from dashboard.excel_import import apply_import, build_template_bytes, parse_workbook, undo_import
from schedule.api_views import _merge_real_data_into_snapshot


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "dashboard-security-tests",
    }
}


@override_settings(
    CACHES=TEST_CACHES,
    LOGIN_RATE_LIMIT_WINDOW_SECONDS=120,
    LOGIN_RATE_LIMIT_ACCOUNT_ATTEMPTS=2,
    LOGIN_RATE_LIMIT_IP_ATTEMPTS=20,
)
class LoginRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="school_manager",
            password="CorrectPass123!",
        )

    def test_repeated_failed_logins_are_temporarily_blocked(self):
        url = reverse("dashboard:login")
        for _ in range(2):
            response = self.client.post(
                url,
                {"username": self.user.username, "password": "wrong"},
                REMOTE_ADDR="203.0.113.10",
            )
            self.assertEqual(response.status_code, 200)

        blocked = self.client.post(
            url,
            {"username": self.user.username, "password": "wrong"},
            REMOTE_ADDR="203.0.113.10",
        )

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked["Retry-After"], "120")
        self.assertContains(blocked, "تم إيقاف محاولات الدخول مؤقتًا", status_code=429)

    def test_successful_login_clears_the_account_failure_counter(self):
        url = reverse("dashboard:login")
        self.client.post(
            url,
            {"username": self.user.username, "password": "wrong"},
            REMOTE_ADDR="203.0.113.11",
        )
        success = self.client.post(
            url,
            {"username": self.user.username, "password": "CorrectPass123!"},
            REMOTE_ADDR="203.0.113.11",
        )

        self.assertEqual(success.status_code, 302)

        self.client.logout()
        after_success = self.client.post(
            url,
            {"username": self.user.username, "password": "wrong"},
            REMOTE_ADDR="203.0.113.11",
        )
        self.assertEqual(after_success.status_code, 200)

    def test_login_template_keeps_desktop_tablet_mobile_layout_contract(self):
        response = self.client.get(reverse("dashboard:login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "width: min(640px, 100%)")
        self.assertContains(response, "@media (min-width: 768px) and (max-width: 1179px)")
        self.assertContains(response, "@media (max-width: 767px)")
        self.assertContains(response, 'for="loginUsername"')
        self.assertContains(response, 'for="loginPassword"')
        self.assertNotContains(response, "Smart School Control")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="no-reply@example.com",
    PASSWORD_RESET_TIMEOUT=3600,
)
class PasswordResetTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reset_manager",
            email="reset@example.com",
            password="OldStrongPass123!",
        )

    def test_login_page_links_to_password_reset(self):
        response = self.client.get(reverse("dashboard:login"))

        self.assertContains(response, reverse("dashboard:password_reset"))
        self.assertContains(response, "نسيت كلمة المرور؟")

    def test_password_reset_email_changes_password(self):
        response = self.client.post(
            reverse("dashboard:password_reset"),
            {"email": "reset@example.com"},
        )

        self.assertRedirects(
            response,
            reverse("dashboard:password_reset_done"),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["reset@example.com"])
        self.assertIn("استعادة كلمة المرور", mail.outbox[0].subject)

        reset_url = next(
            line.strip()
            for line in mail.outbox[0].body.splitlines()
            if "/dashboard/reset/" in line
        )
        token_response = self.client.get(reset_url)
        self.assertEqual(token_response.status_code, 302)

        set_password_response = self.client.post(
            token_response.url,
            {
                "new_password1": "NewStrongPass456!",
                "new_password2": "NewStrongPass456!",
            },
        )
        self.assertRedirects(
            set_password_response,
            reverse("dashboard:password_reset_complete"),
            fetch_redirect_response=False,
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewStrongPass456!"))

    def test_unknown_email_uses_same_success_page_without_sending(self):
        response = self.client.post(
            reverse("dashboard:password_reset"),
            {"email": "unknown@example.com"},
        )

        self.assertRedirects(
            response,
            reverse("dashboard:password_reset_done"),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_inactive_account_does_not_receive_password_reset_email(self):
        self.user.is_active = False
        self.user.save(update_fields=("is_active",))

        response = self.client.post(
            reverse("dashboard:password_reset"),
            {"email": "reset@example.com"},
        )

        self.assertRedirects(
            response,
            reverse("dashboard:password_reset_done"),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 0)


class TenantIsolationTests(TestCase):
    def setUp(self):
        self.allowed_school = School.objects.create(name="المدرسة المصرح بها", slug="allowed-school")
        self.other_school = School.objects.create(name="مدرسة أخرى", slug="other-school")
        self.user = get_user_model().objects.create_user(
            username="tenant_manager",
            password="StrongPass123!",
        )
        self.profile = UserProfile.objects.create(user=self.user, active_school=self.other_school)
        self.profile.schools.add(self.allowed_school)

    def test_invalid_active_school_is_cleared(self):
        school = authorized_active_school(self.profile, user=self.user)

        self.assertIsNone(school)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.active_school)

    def test_dashboard_never_uses_a_school_outside_profile_membership(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard:index"))

        self.assertRedirects(response, reverse("dashboard:select_school"), fetch_redirect_response=False)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.active_school)


@override_settings(
    CACHES=TEST_CACHES,
    TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=True,
    TWO_FACTOR_ENCRYPTION_KEY="",
)
class PrivilegedTwoFactorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_superuser(
            username="system_admin",
            email="admin@example.com",
            password="StrongPass123!",
        )

    def _enable(self):
        config = ensure_setup_config(self.user)
        codes = enable_two_factor(config)
        return config, codes

    def test_privileged_login_requires_initial_enrollment(self):
        response = self.client.post(
            reverse("dashboard:login"),
            {"username": self.user.username, "password": "StrongPass123!"},
        )

        self.assertRedirects(response, reverse("dashboard:two_factor_setup"), fetch_redirect_response=False)

    def test_setup_enables_totp_and_displays_one_time_recovery_codes(self):
        self.client.force_login(self.user)
        config = ensure_setup_config(self.user)
        token = pyotp.TOTP(decrypt_secret(config)).now()

        response = self.client.post(reverse("dashboard:two_factor_setup"), {"token": token})

        self.assertEqual(response.status_code, 200)
        config.refresh_from_db()
        self.assertTrue(config.is_enabled)
        self.assertEqual(len(config.recovery_code_hashes), 10)
        self.assertContains(response, "لن تظهر مرة أخرى")

    def test_enabled_totp_is_required_before_creating_authenticated_session(self):
        config, _codes = self._enable()
        original_session = self.client.session
        original_session.save()
        original_session_key = original_session.session_key

        password_step = self.client.post(
            reverse("dashboard:login"),
            {"username": self.user.username, "password": "StrongPass123!"},
        )

        self.assertRedirects(password_step, reverse("dashboard:two_factor_verify"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotEqual(self.client.session.session_key, original_session_key)

        token = pyotp.TOTP(decrypt_secret(config)).now()
        verify_step = self.client.post(reverse("dashboard:two_factor_verify"), {"token": token})

        self.assertEqual(verify_step.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_totp_cannot_be_replayed_within_the_same_time_window(self):
        config, _codes = self._enable()
        token = pyotp.TOTP(decrypt_secret(config)).now()

        self.assertTrue(consume_second_factor(self.user, token))
        self.assertFalse(consume_second_factor(self.user, token))

    def test_recovery_code_is_single_use(self):
        _config, codes = self._enable()

        self.assertTrue(consume_second_factor(self.user, codes[0]))
        self.assertFalse(consume_second_factor(self.user, codes[0]))

    def test_mandatory_privileged_two_factor_cannot_be_disabled(self):
        config, codes = self._enable()
        self.client.force_login(self.user)

        setup_response = self.client.get(reverse("dashboard:two_factor_setup"))
        response = self.client.post(
            reverse("dashboard:two_factor_disable"),
            {"password": "StrongPass123!", "token": codes[0]},
        )

        self.assertContains(setup_response, "المصادقة الثنائية إلزامية")
        self.assertNotContains(setup_response, "إلغاء المصادقة الثنائية")
        self.assertRedirects(response, reverse("dashboard:two_factor_setup"), fetch_redirect_response=False)
        config.refresh_from_db()
        self.assertTrue(config.is_enabled)


@override_settings(
    CACHES=TEST_CACHES,
    TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=True,
    TWO_FACTOR_ENCRYPTION_KEY="",
)
class SchoolManagerTwoFactorTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة المدير", slug="manager-2fa-school")
        self.user = get_user_model().objects.create_user(
            username="manager_2fa",
            password="StrongPass123!",
        )
        profile = UserProfile.objects.create(user=self.user, active_school=self.school)
        profile.schools.add(self.school)
        plan = SubscriptionPlan.objects.create(
            code="manager-2fa-plan",
            name="خطة المدير",
            price=100,
            duration_days=30,
            max_screens=1,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate(),
            status="active",
        )

    def test_manager_can_open_two_factor_setup_from_security_settings(self):
        self.client.force_login(self.user)

        setup_response = self.client.get(reverse("dashboard:two_factor_setup"))
        settings_response = self.client.get(reverse("dashboard:settings"))

        self.assertEqual(setup_response.status_code, 200)
        self.assertContains(settings_response, "تفعيل المصادقة الثنائية")

    def test_manager_with_enabled_two_factor_is_challenged_at_login(self):
        config = ensure_setup_config(self.user)
        enable_two_factor(config)

        response = self.client.post(
            reverse("dashboard:login"),
            {"username": self.user.username, "password": "StrongPass123!"},
        )

        self.assertRedirects(response, reverse("dashboard:two_factor_verify"), fetch_redirect_response=False)

    def test_manager_can_disable_two_factor_with_password_and_current_token(self):
        config = ensure_setup_config(self.user)
        enable_two_factor(config)
        token = pyotp.TOTP(decrypt_secret(config)).now()
        self.client.force_login(self.user)

        setup_response = self.client.get(reverse("dashboard:two_factor_setup"))
        response = self.client.post(
            reverse("dashboard:two_factor_disable"),
            {"password": "StrongPass123!", "token": token},
        )

        self.assertContains(setup_response, "إلغاء المصادقة الثنائية")
        self.assertRedirects(response, reverse("dashboard:login"), fetch_redirect_response=False)
        self.assertFalse(UserTwoFactorAuth.objects.filter(user=self.user).exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_manager_cannot_disable_two_factor_with_wrong_password(self):
        config = ensure_setup_config(self.user)
        enable_two_factor(config)
        token = pyotp.TOTP(decrypt_secret(config)).now()
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("dashboard:two_factor_disable"),
            {"password": "wrong", "token": token},
        )

        self.assertEqual(response.status_code, 200)
        config.refresh_from_db()
        self.assertTrue(config.is_enabled)


class CustomerExperienceRegressionTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة التجربة", slug="customer-experience-school")
        self.user = get_user_model().objects.create_user(username="customer_manager", password="StrongPass123!")
        self.profile = UserProfile.objects.create(user=self.user, active_school=self.school)
        self.profile.schools.add(self.school)
        self.settings = SchoolSettings.objects.create(name=self.school.name, school=self.school)
        plan = SubscriptionPlan.objects.create(
            code="customer-plan",
            name="الخطة السنوية",
            description="الخيار المناسب لتشغيل شاشات المدرسة",
            price=100,
            duration_days=365,
            max_screens=3,
            max_users=4,
            is_featured=True,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate(),
            status="active",
            notes="internal mobile: 0500000000",
        )
        self.client.force_login(self.user)

    def test_subscription_uses_real_screen_connection_state_and_hides_internal_notes(self):
        DisplayScreen.objects.create(name="الشاشة الرئيسية", school=self.school, is_active=True)

        response = self.client.get(reverse("dashboard:my_subscription"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "بانتظار أول اتصال")
        self.assertNotContains(response, "جميع الشاشات تعمل")
        self.assertNotContains(response, "internal mobile")
        self.assertNotContains(response, "مدرسة مدرسة التجربة")
        self.assertContains(response, "data-plan-option")
        self.assertContains(response, 'id="plan-filter-search"')
        self.assertContains(response, 'id="plan-filter-duration"')
        self.assertContains(response, 'id="plan-filter-screens"')
        self.assertContains(response, 'data-payment-hub="new"')
        self.assertContains(response, 'data-payment-hub="renewal"')
        self.assertContains(response, 'data-payment-choice="new"', count=3)
        self.assertContains(response, 'data-payment-choice="renewal"', count=3)
        self.assertContains(response, "خطوة واحدة واضحة")
        self.assertContains(response, "ربط شاشة الآن")
        self.assertContains(response, reverse("dashboard:screen_list"))
        self.assertContains(response, "dashboard-form-shell col-span-full")
        self.assertNotContains(response, "lg:col-span-12")
        self.assertContains(response, "الخيار المناسب لتشغيل شاشات المدرسة")
        self.assertContains(response, "الأكثر طلباً")
        self.assertNotContains(response, "المدارس:")

    def test_inactive_legacy_plan_keeps_access_but_requires_new_plan_for_renewal(self):
        subscription = SchoolSubscription.objects.select_related("plan").get(school=self.school)
        subscription.plan.is_active = False
        subscription.plan.save(update_fields=["is_active"])

        response = self.client.get(reverse("dashboard:my_subscription"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_request_tab"], "new")
        self.assertTrue(response.context["renewal_requires_new_plan"])
        self.assertContains(response, "يبقى اشتراكك الحالي فعالًا حتى نهايته")
        self.assertContains(response, "اختيار باقة التجديد")
        self.assertNotContains(response, 'data-payment-hub="renewal"')

    def test_display_customization_lives_with_screens_not_school_settings(self):
        screen = DisplayScreen.objects.create(
            name="الشاشة الرئيسية",
            school=self.school,
            is_active=True,
        )

        settings_response = self.client.get(reverse("dashboard:settings"))

        self.assertEqual(settings_response.status_code, 200)
        self.assertNotContains(settings_response, "الهوية والمظهر")
        self.assertNotContains(settings_response, "رسائل الشاشة")
        self.assertNotContains(settings_response, "سرعة العرض")
        self.assertContains(settings_response, "التشغيل والتنبيهات")
        self.assertContains(settings_response, "بيانات التواصل")
        self.assertContains(settings_response, "الأمان")

        list_response = self.client.get(reverse("dashboard:screen_list"))
        self.assertContains(list_response, "تخصيص جميع الشاشات")
        self.assertContains(list_response, "تخصيص الشاشة")

        all_response = self.client.get(reverse("dashboard:screens_customize_all"))
        self.assertEqual(all_response.status_code, 200)
        self.assertContains(all_response, "الهوية والمظهر")
        self.assertContains(all_response, "رسائل الشاشة")
        self.assertContains(all_response, "سرعة العرض")
        self.assertContains(all_response, "تخصيص جميع الشاشات")
        self.assertContains(all_response, f"/s/{screen.short_code}/")

        screen_response = self.client.get(reverse("dashboard:screen_edit", args=[screen.pk]))
        self.assertEqual(screen_response.status_code, 200)
        self.assertContains(screen_response, f"تخصيص شاشة: {screen.name}")
        self.assertContains(screen_response, "المحتوى الظاهر في هذه الشاشة")
        self.assertContains(screen_response, "استخدام الإعداد العام")
        self.assertContains(screen_response, "كل التغييرات محفوظة")

    def test_free_plan_needs_no_receipt_but_paid_bank_transfer_does(self):
        free_plan = SubscriptionPlan.objects.create(
            code="customer-free-plan",
            name="تجربة مجانية",
            price=0,
            duration_days=14,
            max_screens=1,
        )
        paid_plan = SchoolSubscription.objects.get(school=self.school).plan

        free_form = SubscriptionNewRequestForm(
            data={"new-plan": free_plan.pk},
            prefix="new",
        )
        paid_form = SubscriptionNewRequestForm(
            data={"new-plan": paid_plan.pk},
            prefix="new",
        )

        self.assertTrue(free_form.is_valid(), free_form.errors)
        self.assertFalse(paid_form.is_valid())
        self.assertIn("receipt_image", paid_form.errors)

        response = self.client.post(
            reverse("dashboard:my_subscription"),
            {"action": "new", "new-plan": free_plan.pk},
        )
        self.assertRedirects(
            response,
            reverse("dashboard:my_subscription"),
            fetch_redirect_response=False,
        )
        request_obj = SubscriptionRequest.objects.get(plan=free_plan)
        self.assertFalse(bool(request_obj.receipt_image))

    def test_connected_screen_is_reported_live_in_screen_list(self):
        screen = DisplayScreen.objects.create(
            name="الشاشة الرئيسية",
            school=self.school,
            is_active=True,
            bound_device_id="tv-device-1",
        )
        touch_display_presence(screen.pk, token=screen.token)

        response = self.client.get(reverse("dashboard:screen_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "متصلة الآن")
        self.assertContains(response, "قبل أقل من دقيقة")
        self.assertNotContains(response, "لم تتصل بعد")

    def test_screen_monitoring_center_reports_outage_health_and_history(self):
        screen = DisplayScreen.objects.create(
            name="شاشة الساحة",
            school=self.school,
            is_active=True,
            bound_device_id="tv-device-outage",
            last_seen=timezone.now() - timedelta(minutes=35),
        )
        ScreenOutage.objects.create(
            screen=screen,
            detected_at=timezone.now() - timedelta(minutes=30),
            last_seen_at=timezone.now() - timedelta(minutes=35),
            cause="network_drop",
            cause_detail="انقطاع اتصال الجهاز بالإنترنت",
        )
        cache.set("screen-monitor:heartbeat", timezone.now().isoformat(), timeout=180)

        response = self.client.get(reverse("dashboard:screen_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "منقطعة الآن")
        self.assertContains(response, "متوسط التشغيل خلال 7 أيام")
        self.assertContains(response, "سجل الانقطاعات الأخير")
        self.assertContains(response, "انقطاع اتصال الجهاز بالإنترنت")
        self.assertContains(response, "تعمل بصورة طبيعية")

    def test_screen_list_opens_a_read_only_live_preview_inside_the_dashboard(self):
        screen = DisplayScreen.objects.create(
            name="شاشة المدخل",
            school=self.school,
            is_active=True,
            bound_device_id="tv-device-1",
        )

        response = self.client.get(reverse("dashboard:screen_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "معاينة حية")
        self.assertContains(response, "data-live-preview-open")
        self.assertContains(response, 'id="livePreviewDialog"')
        self.assertContains(response, f'/s/{screen.short_code}?preview=1')
        self.assertContains(response, "لا تُحتسب كشاشة متصلة")
        self.assertContains(response, "screen-live-preview.css")
        self.assertContains(response, "screen-live-preview.js")

    def test_manager_can_customize_each_screen_independently(self):
        first = DisplayScreen.objects.create(name="شاشة المدخل", school=self.school)
        second = DisplayScreen.objects.create(name="شاشة المعلمين", school=self.school)

        form_response = self.client.get(reverse("dashboard:screen_edit", args=[first.pk]))
        self.assertEqual(form_response.status_code, 200)
        self.assertContains(form_response, "تخصيص شاشة: شاشة المدخل")
        self.assertContains(form_response, "لون الثيم")
        self.assertContains(form_response, "رسائل الشاشة")
        self.assertContains(form_response, "سرعة العرض")
        self.assertContains(form_response, "استخدام الإعداد العام")

        settings_obj = SchoolSettings.objects.get(school=self.school)

        response = self.client.post(
            reverse("dashboard:screen_edit", args=[first.pk]),
            {
                "name": first.name,
                "is_active": "on",
                "theme": "rose",
                "display_accent_color": "#EC4899",
                "occasion_theme": "graduation",
                "featured_panel": "duty",
                "standby_scroll_speed": "1.1",
                "periods_scroll_speed": "0.9",
                "display_before_title": settings_obj.get_display_before_title(),
                "display_before_badge": settings_obj.get_display_before_badge(),
                "display_after_title": settings_obj.get_display_after_title(),
                "display_after_badge": settings_obj.get_display_after_badge(),
                "display_after_holiday_title": settings_obj.get_display_after_holiday_title(),
                "display_after_holiday_badge": settings_obj.get_display_after_holiday_badge(),
                "display_holiday_title": settings_obj.get_display_holiday_title(),
                "display_holiday_badge": settings_obj.get_display_holiday_badge(),
                "show_announcements": "on",
                "show_period_classes": "on",
                "show_duty": "on",
            },
        )

        self.assertRedirects(
            response,
            reverse("dashboard:screen_list"),
            fetch_redirect_response=False,
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.theme_override, "rose")
        self.assertEqual(first.display_accent_color_override, "#EC4899")
        self.assertEqual(first.standby_scroll_speed_override, 1.1)
        self.assertEqual(first.periods_scroll_speed_override, 0.9)
        self.assertEqual(first.occasion_theme, "graduation")
        self.assertEqual(first.featured_panel_override, "duty")
        self.assertTrue(first.show_announcements)
        self.assertTrue(first.show_period_classes)
        self.assertFalse(first.show_standby)
        self.assertTrue(first.show_duty)
        self.assertFalse(first.show_excellence)
        self.assertEqual(second.theme_override, "")
        self.assertEqual(second.occasion_theme, "auto")
        self.assertTrue(second.show_standby)
        self.assertTrue(second.show_excellence)

        reset_response = self.client.post(
            reverse("dashboard:screen_edit", args=[first.pk]),
            {"action": "reset_display_customization"},
        )
        self.assertRedirects(
            reset_response,
            reverse("dashboard:screen_edit", args=[first.pk]),
            fetch_redirect_response=False,
        )
        first.refresh_from_db()
        self.assertEqual(first.theme_override, "")
        self.assertEqual(first.display_accent_color_override, "")
        self.assertIsNone(first.standby_scroll_speed_override)
        self.assertIsNone(first.periods_scroll_speed_override)
        self.assertEqual(first.occasion_theme, "auto")
        self.assertTrue(first.show_standby)
        self.assertTrue(first.show_excellence)

    def test_screen_customization_is_scoped_to_active_school(self):
        other_school = School.objects.create(name="مدرسة أخرى", slug="other-screen-school")
        other_screen = DisplayScreen.objects.create(name="شاشة أخرى", school=other_school)

        response = self.client.get(reverse("dashboard:screen_edit", args=[other_screen.pk]))

        self.assertEqual(response.status_code, 404)

    @override_settings(DISPLAY_WS_ENABLED=True)
    def test_manual_screen_commands_are_pushed_to_required_group_immediately(self):
        screen = DisplayScreen.objects.create(
            name="الشاشة الرئيسية",
            school=self.school,
            is_active=True,
        )

        for url_name, event_type in (
            ("dashboard:screen_refresh_now", "broadcast_invalidate"),
            ("dashboard:screen_reload_now", "broadcast_reload"),
        ):
            channel_layer = SimpleNamespace(group_send=AsyncMock())
            with patch("channels.layers.get_channel_layer", return_value=channel_layer):
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(reverse(url_name, args=[screen.pk]))

            self.assertRedirects(
                response,
                reverse("dashboard:screen_list"),
                fetch_redirect_response=False,
            )
            channel_layer.group_send.assert_awaited_once()
            group, event = channel_layer.group_send.await_args.args
            self.assertEqual(group, school_group_name(self.school.pk))
            self.assertEqual(event["type"], event_type)
            self.assertEqual(event["school_id"], self.school.pk)
            self.assertEqual(event["target_screen_id"], screen.pk)

    def test_bound_screen_without_legacy_presence_is_not_called_offline(self):
        DisplayScreen.objects.create(
            name="الشاشة الرئيسية",
            school=self.school,
            is_active=True,
            bound_device_id="tv-device-1",
            bound_at=timezone.now(),
        )

        response = self.client.get(reverse("dashboard:screen_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مرتبطة وجاهزة")
        self.assertContains(response, "قبل أقل من دقيقة")
        self.assertNotContains(response, "مرتبطة بلا اتصال")
        self.assertNotContains(response, "لم تتصل بعد")

    def test_successful_display_binding_lookup_records_presence(self):
        screen = DisplayScreen.objects.create(
            name="الشاشة الرئيسية",
            school=self.school,
            is_active=True,
            bound_device_id="tv-device-1",
        )

        bound_screen = bind_device_atomic(token=screen.token, device_id="tv-device-1")

        self.assertEqual(bound_screen.pk, screen.pk)
        self.assertIsNotNone(latest_display_presence(screen))

    def _complete_guide_data(self):
        screen = DisplayScreen.objects.create(name="الشاشة الرئيسية", school=self.school, is_active=True)
        school_class = SchoolClass.objects.create(settings=self.settings, name="1/أ")
        subject = Subject.objects.create(school=self.school, name="الرياضيات")
        teacher = Teacher.objects.create(school=self.school, name="أحمد")
        day = DaySchedule.objects.create(settings=self.settings, weekday=1, periods_count=1)
        Period.objects.create(
            day=day,
            school_class=school_class,
            subject=subject,
            teacher=teacher,
            index=1,
            starts_at=time(8, 0),
            ends_at=time(8, 45),
        )
        return screen

    def test_guide_is_not_complete_while_no_screen_is_playing(self):
        """Entering the data is not the goal — a school day on the wall is."""
        self._complete_guide_data()

        response = self.client.get(reverse("dashboard:help_getting_started"))

        summary = response.context["setup_summary"]
        self.assertEqual(summary["completed_required"], 3)
        self.assertEqual(summary["total_required"], 4)
        self.assertEqual(summary["next_step_title"], "تشغيل الشاشة على التلفاز")
        self.assertContains(response, "لم تُشغَّل بعد")

    def test_completed_guide_opens_the_school_display(self):
        screen = self._complete_guide_data()
        screen.bound_device_id = "tv-device-1"
        screen.bound_at = timezone.now()
        screen.save(update_fields=["bound_device_id", "bound_at"])
        touch_display_presence(screen.pk, token=screen.token)

        response = self.client.get(reverse("dashboard:help_getting_started"))

        self.assertContains(response, "100%")
        self.assertContains(response, "افتح شاشة مدرستك الآن")
        self.assertContains(response, reverse("website:short_display", args=[screen.short_code]))

    def test_guide_launch_link_previews_instead_of_claiming_the_tv(self):
        """The dashboard's own link must not win the screen's single device slot."""
        screen = self._complete_guide_data()
        screen.bound_device_id = "tv-device-1"
        screen.bound_at = timezone.now()
        screen.save(update_fields=["bound_device_id", "bound_at"])
        touch_display_presence(screen.pk, token=screen.token)

        response = self.client.get(reverse("dashboard:help_getting_started"))

        self.assertEqual(
            response.context["setup_summary"]["launch_screen_url"],
            reverse("website:short_display", args=[screen.short_code]) + "?preview=1",
        )

    def test_dashboard_prioritizes_screen_operation_and_first_launch_state(self):
        screen = DisplayScreen.objects.create(name="شاشة المدخل", school=self.school, is_active=True)

        response = self.client.get(reverse("dashboard:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مركز تشغيل المدرسة")
        self.assertContains(response, "بانتظار أول تشغيل")
        self.assertContains(response, "فتح شاشة المدرسة")
        self.assertContains(response, "إدارة الشاشات")
        self.assertContains(response, reverse("website:short_display", args=[screen.short_code]))
        self.assertContains(response, 'aria-label="تقدم إعداد المنصة"')

    def test_dashboard_reports_a_connected_primary_screen(self):
        screen = DisplayScreen.objects.create(
            name="شاشة المدخل",
            school=self.school,
            is_active=True,
            bound_device_id="customer-tv-1",
        )
        touch_display_presence(screen.pk, token=screen.token)

        response = self.client.get(reverse("dashboard:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "متصلة الآن")
        self.assertContains(response, "الشاشة تعمل وتستقبل تحديثات الجدول والتنبيهات تلقائيًا")

    def test_school_manager_is_returned_from_internal_admin_panel(self):
        response = self.client.get(reverse("dashboard:system_admin_dashboard"), follow=True)

        self.assertRedirects(response, reverse("dashboard:index"))
        self.assertContains(response, "هذه الصفحة مخصصة لفريق إدارة النظام")
        self.assertNotContains(response, "403 Forbidden")

    def test_branded_permission_page_replaces_django_default(self):
        request = RequestFactory().get("/protected/")
        request.user = self.user

        response = permission_denied(request, PermissionDenied("blocked"))

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "هذه الصفحة ليست ضمن صلاحيات حسابك", status_code=403)
        self.assertContains(response, "العودة إلى لوحة المدرسة", status_code=403)
        self.assertNotContains(response, "403 Forbidden", status_code=403)


class RefactoredRoutingAndPermissionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.school_a = School.objects.create(name="مدرسة أ", slug="refactor-school-a")
        self.school_b = School.objects.create(name="مدرسة ب", slug="refactor-school-b")
        self.manager = get_user_model().objects.create_user(
            username="refactor_manager",
            password="StrongPass123!",
        )
        self.profile = UserProfile.objects.create(user=self.manager, active_school=self.school_a)
        self.profile.schools.add(self.school_a)
        plan = SubscriptionPlan.objects.create(
            code="refactor-active-plan",
            name="خطة الاختبار",
            price=100,
            duration_days=30,
            max_screens=3,
        )
        SchoolSubscription.objects.create(
            school=self.school_a,
            plan=plan,
            starts_at=timezone.localdate(),
            ends_at=timezone.localdate(),
            status="active",
        )

    def test_auth_and_screen_urls_resolve_to_extracted_modules(self):
        self.assertEqual(resolve(reverse("dashboard:login")).func.__module__, "dashboard.auth_views")
        self.assertEqual(resolve(reverse("dashboard:screen_list")).func.__module__, "dashboard.views_screens")
        self.assertEqual(
            resolve(reverse("dashboard:customer_support_tickets")).func.__module__,
            "dashboard.support_views",
        )

        from dashboard import auth_views, support_views, views, views_screens

        self.assertIs(views.login_view, auth_views.login_view)
        self.assertIs(views.screen_list, views_screens.screen_list)
        self.assertIs(views.customer_support_tickets, support_views.customer_support_tickets)

    def test_active_school_service_sets_request_tenant(self):
        request = self.factory.get("/dashboard/")
        request.user = self.manager
        request.school = None

        school, response = get_active_school_or_redirect(request)

        self.assertIsNone(response)
        self.assertEqual(school, self.school_a)
        self.assertEqual(request.school, self.school_a)

    def test_support_identity_cannot_use_school_manager_decorator(self):
        support = get_user_model().objects.create_user(username="support_refactor", password="StrongPass123!")
        support.groups.add(Group.objects.create(name="Support"))
        support_profile = UserProfile.objects.create(user=support, active_school=self.school_a)
        support_profile.schools.add(self.school_a)
        request = self.factory.get("/dashboard/school-data/")
        request.user = support
        protected_view = manager_required(lambda _request: HttpResponse("ok"))

        with self.assertRaises(PermissionDenied):
            protected_view(request)

    def test_regular_manager_cannot_use_system_staff_or_superuser_decorators(self):
        request = self.factory.get("/dashboard/admin-panel/")
        request.user = self.manager

        with self.assertRaises(PermissionDenied):
            system_staff_required(lambda _request: HttpResponse("ok"))(request)
        with self.assertRaises(PermissionDenied):
            superuser_required(lambda _request: HttpResponse("ok"))(request)

    def test_manager_cannot_delete_another_schools_screen(self):
        other_screen = DisplayScreen.objects.create(school=self.school_b, name="شاشة المدرسة ب")
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("dashboard:screen_delete", kwargs={"pk": other_screen.pk})
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(DisplayScreen.objects.filter(pk=other_screen.pk).exists())

    def test_customer_cannot_open_another_users_support_ticket(self):
        other_user = get_user_model().objects.create_user(username="other_ticket_owner", password="StrongPass123!")
        ticket = SupportTicket.objects.create(
            user=other_user,
            school=self.school_b,
            subject="تذكرة خاصة",
            message="لا يجب أن تظهر لمستخدم آخر",
        )
        self.client.force_login(self.manager)

        response = self.client.get(
            reverse("dashboard:customer_support_ticket_detail", kwargs={"pk": ticket.pk})
        )

        self.assertEqual(response.status_code, 404)

    def test_ticket_creation_never_uses_an_unauthorized_active_school(self):
        self.profile.active_school = self.school_b
        self.profile.save(update_fields=["active_school"])
        self.client.force_login(self.manager)

        response = self.client.post(
            reverse("dashboard:customer_support_ticket_create"),
            {"subject": "طلب مساعدة", "message": "تفاصيل الطلب"},
        )

        self.assertRedirects(
            response,
            reverse("dashboard:customer_support_tickets"),
            fetch_redirect_response=False,
        )
        ticket = SupportTicket.objects.get(user=self.manager, subject="طلب مساعدة")
        self.assertEqual(ticket.school, self.school_a)


@override_settings(
    CACHES=TEST_CACHES,
    TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False,
)
class SystemAdminExperienceTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="admin_experience",
            email="admin-experience@example.com",
            password="StrongPass123!",
        )
        self.school = School.objects.create(name="مدرسة الإدارة", slug="admin-school")
        self.manager = get_user_model().objects.create_user(
            username="school_director",
            email="director@example.com",
            password="StrongPass123!",
        )
        self.profile = UserProfile.objects.create(user=self.manager, active_school=self.school)
        self.profile.schools.add(self.school)
        self.plan = SubscriptionPlan.objects.create(
            code="admin-pro",
            name="الباقة الاحترافية",
            price=990,
            duration_days=365,
            max_users=7,
            max_screens=2,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
            starts_at=timezone.localdate(),
            ends_at=timezone.localdate() + timedelta(days=12),
            status="active",
        )
        self.client.force_login(self.admin)

    def test_admin_dashboard_prioritizes_daily_operations(self):
        response = self.client.get(reverse("dashboard:system_admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "مركز إدارة المنصة")
        self.assertContains(response, "تحتاج إلى انتباهك")
        self.assertContains(response, "اشتراكات تنتهي قريباً")
        self.assertContains(response, "الباقات والأسعار")
        self.assertContains(response, "إحصائيات مسار البيع")
        self.assertContains(response, "متوسط الشاشات/مدرسة")

    def test_admin_payments_ledger_and_detail_expose_recorded_payment(self):
        subscription = SchoolSubscription.objects.get(school=self.school, plan=self.plan)
        operation = SubscriptionPaymentOperation.objects.create(
            school=self.school,
            subscription=subscription,
            plan=self.plan,
            amount=Decimal("990.00"),
            method="moyasar",
            source="request",
            created_by=self.admin,
            note="دفعة إلكترونية موثقة للاختبار",
        )
        invoice = SubscriptionInvoice.objects.get(operation=operation)
        checkout = MoyasarCheckout.objects.create(
            school=self.school,
            created_by=self.admin,
            plan=self.plan,
            request_type="new",
            amount=operation.amount,
            status="captured",
            live_mode=True,
            payment_id="pay_admin_test_001",
            payment_operation=operation,
            subscription=subscription,
            last_event="payment_captured",
            processed_at=timezone.now(),
        )

        list_response = self.client.get(
            reverse("dashboard:system_payments_list"),
            {"q": checkout.payment_id, "method": "moyasar"},
        )
        detail_response = self.client.get(
            reverse("dashboard:system_payment_detail", args=[operation.pk])
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, self.school.name)
        self.assertContains(list_response, invoice.invoice_number)
        self.assertContains(list_response, "990,00")
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "دفعة إلكترونية موثقة للاختبار")
        self.assertContains(detail_response, "pay_admin_test_001")
        self.assertContains(detail_response, "payment_captured")
        self.assertContains(detail_response, invoice.invoice_number)

    def test_admin_lists_expose_school_manager_and_subscription_filters(self):
        admin_profile = UserProfile.objects.create(user=self.admin, active_school=self.school)
        admin_profile.schools.add(self.school)

        schools_response = self.client.get(
            reverse("dashboard:system_schools_list"),
            {"plan": self.plan.pk},
        )
        managers_response = self.client.get(
            reverse("dashboard:system_users_list"),
            {"school": self.school.pk, "role": "manager"},
        )
        subscriptions_response = self.client.get(
            reverse("dashboard:system_subscriptions_list"),
            {"status": "expiring"},
        )

        self.assertContains(schools_response, self.school.name)
        self.assertContains(schools_response, self.plan.name)
        school_row = next(
            school for school in schools_response.context["schools"] if school.pk == self.school.pk
        )
        self.assertEqual(school_row.managers_count, 1)
        self.assertContains(managers_response, self.manager.username)
        self.assertContains(subscriptions_response, "ينتهي قريباً")
        self.assertContains(subscriptions_response, self.school.name)

        dashboard_response = self.client.get(reverse("dashboard:system_admin_dashboard"))
        recent_school = next(
            school for school in dashboard_response.context["recent_schools"] if school.pk == self.school.pk
        )
        self.assertEqual(recent_school.managers_total, 1)

    def test_protected_subscription_delete_is_cancelled_instead_of_500(self):
        subscription = SchoolSubscription.objects.get(school=self.school, plan=self.plan)
        operation = SubscriptionPaymentOperation.objects.create(
            school=self.school,
            subscription=subscription,
            plan=self.plan,
            amount=subscription.plan.price,
            method="bank_transfer",
            source="admin_manual",
        )
        self.assertTrue(SubscriptionInvoice.objects.filter(operation=operation).exists())

        response = self.client.post(
            reverse("dashboard:system_subscription_delete", args=[subscription.pk])
        )

        self.assertRedirects(response, reverse("dashboard:system_subscriptions_list"))
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "cancelled")

    def test_plan_cards_show_limits_and_usage(self):
        response = self.client.get(reverse("dashboard:system_plans_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.plan.name)
        self.assertContains(response, "اشتراكات سارية")
        self.assertContains(response, "إضافة باقة جديدة")
        self.assertContains(response, "تعديل")
        self.assertContains(response, "حذف")
        self.assertContains(response, "2")
        self.assertContains(response, "7")

    def test_admin_plan_update_syncs_landing_and_school_manager_catalog(self):
        response = self.client.post(
            reverse("dashboard:system_plan_edit", args=[self.plan.pk]),
            {
                "name": "باقة متزامنة",
                "description": "وصف موحد من لوحة إدارة المنصة",
                "code": self.plan.code,
                "price": "1777.00",
                "duration_days": "365",
                "max_screens": "2",
                "card_badge_text": "الأوفر للمدارس",
                "card_duration_text": "عام دراسي — صلاحية 12 شهرًا",
                "card_price_caption": "ريال سعودي / عام دراسي",
                "card_monthly_text": "سعر موحد في جميع الواجهات",
                "card_features": "ميزة موحدة أولى\nميزة موحدة ثانية",
                "card_screen_text": "تشغيل شاشتين",
                "card_cta_text": "اختر الباقة المتزامنة",
                "show_card_badge": "on",
                "show_card_duration": "on",
                "show_monthly_equivalent": "on",
                "show_screen_limit": "on",
                "sort_order": "10",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("dashboard:system_plans_list"))

        landing_response = self.client.get(reverse("website:home"))
        self.client.force_login(self.manager)
        manager_response = self.client.get(reverse("dashboard:my_subscription"))

        for catalog_response in (landing_response, manager_response):
            self.assertContains(catalog_response, "باقة متزامنة")
            self.assertContains(catalog_response, "1777")
            self.assertContains(catalog_response, "عام دراسي — صلاحية 12 شهرًا")
            self.assertContains(catalog_response, "سعر موحد في جميع الواجهات")


    def test_superuser_in_support_group_keeps_plan_management_navigation(self):
        self.admin.groups.add(Group.objects.get_or_create(name="Support")[0])

        response = self.client.get(reverse("dashboard:system_plans_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "إدارة الباقات والأسعار")
        self.assertContains(response, reverse("dashboard:system_plan_create"))

    def test_plan_crud_and_activation_controls(self):
        form_response = self.client.get(reverse("dashboard:system_plan_create"))
        self.assertContains(form_response, "وصف مختصر")
        self.assertContains(form_response, "الباقة الموصى بها")
        self.assertContains(form_response, "data-duration-days=\"365\"")
        self.assertContains(form_response, "planPreviewName")
        self.assertContains(form_response, "محتوى بطاقة الباقة")
        self.assertContains(form_response, "مزايا الباقة")
        self.assertNotContains(form_response, "الحد الأقصى للمستخدمين")

        create_response = self.client.post(
            reverse("dashboard:system_plan_create"),
            {
                "name": "الباقة الجديدة",
                "description": "وصف الباقة الجديدة",
                "code": "NEW-PLAN",
                "price": "1499.00",
                "duration_days": "365",
                "max_screens": "3",
                "card_badge_text": "باقة المدارس النشطة",
                "card_duration_text": "مدة خاصة للعميل",
                "card_price_caption": "ريال سعودي / عرض سنوي",
                "card_monthly_text": "قيمة شهرية مخصصة",
                "card_features": "الميزة الأولى\nالميزة الثانية",
                "card_screen_text": "3 شاشات متزامنة",
                "card_cta_text": "اطلب العرض الآن",
                "show_card_badge": "on",
                "show_card_duration": "on",
                "show_monthly_equivalent": "on",
                "show_screen_limit": "on",
                "sort_order": "2",
                "is_featured": "on",
                "is_active": "on",
            },
        )
        self.assertRedirects(create_response, reverse("dashboard:system_plans_list"))
        created_plan = SubscriptionPlan.objects.get(code="new-plan")
        self.assertTrue(created_plan.is_featured)
        self.assertEqual(created_plan.description, "وصف الباقة الجديدة")
        self.assertEqual(created_plan.card_badge_text, "باقة المدارس النشطة")
        self.assertEqual(created_plan.card_features, "الميزة الأولى\nالميزة الثانية")
        self.assertEqual(created_plan.card_cta_text, "اطلب العرض الآن")

        edit_response = self.client.post(
            reverse("dashboard:system_plan_edit", args=[created_plan.pk]),
            {
                "name": "الباقة الجديدة المطورة",
                "description": "وصف محدث",
                "code": "new-plan",
                "price": "1799.00",
                "duration_days": "365",
                "max_screens": "4",
                "card_badge_text": "باقة مطورة",
                "card_duration_text": "مدة محدثة",
                "card_price_caption": "سعر محدث",
                "card_monthly_text": "معادل محدث",
                "card_features": "ميزة محدثة",
                "card_screen_text": "4 شاشات متزامنة",
                "card_cta_text": "اطلب النسخة المطورة",
                "show_card_badge": "on",
                "show_card_duration": "on",
                "show_monthly_equivalent": "on",
                "show_screen_limit": "on",
                "sort_order": "2",
                "is_active": "on",
            },
        )
        self.assertRedirects(edit_response, reverse("dashboard:system_plans_list"))
        created_plan.refresh_from_db()
        self.assertEqual(created_plan.name, "الباقة الجديدة المطورة")
        self.assertEqual(created_plan.max_screens, 4)

        toggle_response = self.client.post(
            reverse("dashboard:system_plan_toggle", args=[created_plan.pk])
        )
        self.assertRedirects(toggle_response, reverse("dashboard:system_plans_list"))
        created_plan.refresh_from_db()
        self.assertFalse(created_plan.is_active)

        delete_response = self.client.post(
            reverse("dashboard:system_plan_delete", args=[created_plan.pk])
        )
        self.assertRedirects(delete_response, reverse("dashboard:system_plans_list"))
        self.assertFalse(SubscriptionPlan.objects.filter(pk=created_plan.pk).exists())

    def test_used_plan_is_archived_instead_of_breaking_subscription_history(self):
        response = self.client.get(
            reverse("dashboard:system_plan_delete", args=[self.plan.pk])
        )
        self.assertContains(response, "هذه الباقة مستخدمة")
        self.assertContains(response, "إيقاف الباقة بأمان")

        response = self.client.post(
            reverse("dashboard:system_plan_delete", args=[self.plan.pk])
        )

        self.assertRedirects(response, reverse("dashboard:system_plans_list"))
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.is_active)
        self.assertTrue(SchoolSubscription.objects.filter(plan=self.plan).exists())


@override_settings(
    CACHES=TEST_CACHES,
    TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False,
)
class SystemEmployeePermissionTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="platform_owner",
            email="owner@example.com",
            password="StrongPass123!",
        )
        self.plan = SubscriptionPlan.objects.create(
            code="employee-access-plan",
            name="باقة اختبار الصلاحيات",
            price=500,
            duration_days=365,
            max_screens=2,
        )
        self.school = School.objects.create(
            name="مدرسة صلاحيات الموظفين",
            slug="employee-permissions-school",
        )

    def _create_employee(self, *, username="delegated_employee", permissions=None, role="custom"):
        employee = get_user_model().objects.create_user(
            username=username,
            password="StrongPass123!",
            is_staff=True,
        )
        SystemEmployeeProfile.objects.create(
            user=employee,
            role=role,
            permission_keys=permissions or ["dashboard.view"],
            created_by=self.owner,
        )
        return employee

    def test_owner_can_create_finance_employee_with_granular_permissions(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("dashboard:system_employee_create"),
            {
                "username": "finance_agent",
                "email": "finance@example.com",
                "first_name": "موظف",
                "last_name": "المالية",
                "mobile": "0500000000",
                "is_active": "on",
                "role": ROLE_FINANCE,
                "permissions": [
                    "subscriptions.manage",
                    "subscription_requests.manage",
                    "reports.view",
                ],
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        self.assertRedirects(response, reverse("dashboard:system_employees_list"))
        employee = get_user_model().objects.get(username="finance_agent")
        profile = employee.system_employee_profile
        self.assertTrue(employee.is_staff)
        self.assertFalse(employee.is_superuser)
        self.assertEqual(profile.role, ROLE_FINANCE)
        self.assertEqual(
            set(profile.permission_keys),
            {
                "dashboard.view",
                "subscriptions.view",
                "subscriptions.manage",
                "subscription_requests.view",
                "subscription_requests.manage",
                "reports.view",
            },
        )
        self.assertFalse(employee.profile.schools.exists())

    def test_view_only_permission_hides_actions_and_blocks_direct_mutation(self):
        employee = self._create_employee(permissions=["dashboard.view", "plans.view"])
        self.client.force_login(employee)

        list_response = self.client.get(reverse("dashboard:system_plans_list"))
        create_response = self.client.get(reverse("dashboard:system_plan_create"))
        toggle_response = self.client.post(
            reverse("dashboard:system_plan_toggle", args=[self.plan.pk])
        )
        schools_response = self.client.get(reverse("dashboard:system_schools_list"))
        employees_response = self.client.get(reverse("dashboard:system_employees_list"))

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, self.plan.name)
        self.assertNotContains(list_response, reverse("dashboard:system_plan_create"))
        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(toggle_response.status_code, 403)
        self.assertEqual(schools_response.status_code, 403)
        self.assertEqual(employees_response.status_code, 403)
        self.plan.refresh_from_db()
        self.assertTrue(self.plan.is_active)

    def test_navigation_contains_only_authorized_platform_sections(self):
        employee = self._create_employee(
            permissions=["dashboard.view", "subscriptions.view", "support.view"]
        )
        self.client.force_login(employee)

        response = self.client.get(reverse("dashboard:system_admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        nav_keys = {item["key"] for item in response.context["admin_nav_links"]}
        self.assertEqual(nav_keys, {"home", "subscriptions", "payments", "support"})
        self.assertNotContains(response, reverse("dashboard:system_employees_list"))
        self.assertNotContains(response, reverse("dashboard:system_schools_list"))

    def test_owner_edit_applies_permissions_on_the_next_request(self):
        employee = self._create_employee(
            username="support_agent",
            permissions=role_permissions(ROLE_SUPPORT),
            role=ROLE_SUPPORT,
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:system_employee_edit", args=[employee.pk]),
            {
                "username": employee.username,
                "email": "support@example.com",
                "first_name": "الدعم",
                "last_name": "الفني",
                "mobile": "",
                "is_active": "on",
                "role": "custom",
                "permissions": ["dashboard.view", "plans.view"],
                "new_password1": "",
                "new_password2": "",
            },
        )
        self.assertRedirects(response, reverse("dashboard:system_employees_list"))

        self.client.force_login(employee)
        self.assertEqual(
            self.client.get(reverse("dashboard:system_plans_list")).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("dashboard:system_support_tickets")).status_code,
            403,
        )

    def test_delegated_user_manager_cannot_promote_customer_or_list_employees(self):
        employee = self._create_employee(
            permissions=["dashboard.view", "users.view", "users.manage"]
        )
        self.client.force_login(employee)

        create_response = self.client.post(
            reverse("dashboard:system_user_create"),
            {
                "username": "new_school_manager",
                "email": "manager@example.com",
                "first_name": "مدير",
                "last_name": "مدرسة",
                "mobile": "",
                "is_active": "on",
                "is_staff": "on",
                "is_superuser": "on",
                "schools": [self.school.pk],
                "active_school": self.school.pk,
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )
        self.assertRedirects(create_response, reverse("dashboard:system_users_list"))
        customer = get_user_model().objects.get(username="new_school_manager")
        self.assertFalse(customer.is_staff)
        self.assertFalse(customer.is_superuser)

        list_response = self.client.get(
            reverse("dashboard:system_users_list"),
            {"role": "system"},
        )
        self.assertEqual(list_response.status_code, 200)
        listed_usernames = {
            user.username for user in list_response.context["page_obj"].object_list
        }
        self.assertNotIn(self.owner.username, listed_usernames)
        self.assertNotIn(employee.username, listed_usernames)
        self.assertIn(customer.username, listed_usernames)
        self.assertNotContains(list_response, "حسابات النظام")
        self.assertContains(list_response, customer.username)

    def test_view_only_support_and_emergency_access_hides_mutating_controls(self):
        employee = self._create_employee(
            permissions=[
                "dashboard.view",
                "support.view",
                "emergency_alerts.view",
            ]
        )
        ticket = SupportTicket.objects.create(
            user=self.owner,
            school=self.school,
            subject="تذكرة للقراءة فقط",
            message="لا ينبغي أن تظهر أدوات المعالجة.",
        )
        old_alert = EmergencyAlert.objects.create(
            kind=EmergencyAlert.KIND_URGENT,
            title="تنبيه قديم للقراءة فقط",
            message="لا ينبغي أن تظهر أداة حذفه.",
            created_by=self.owner,
            is_active=False,
        )
        old_alert.schools.add(self.school)
        self.client.force_login(employee)

        ticket_response = self.client.get(
            reverse("dashboard:system_support_ticket_detail", args=[ticket.pk])
        )
        emergency_response = self.client.get(reverse("dashboard:emergency_alert_list"))

        self.assertEqual(ticket_response.status_code, 200)
        self.assertContains(ticket_response, ticket.subject)
        self.assertNotContains(ticket_response, "إضافة رد")
        self.assertNotContains(ticket_response, "إرسال الرد")
        self.assertEqual(
            self.client.post(
                reverse("dashboard:system_support_ticket_detail", args=[ticket.pk]),
                {"status": "closed"},
            ).status_code,
            403,
        )
        self.assertEqual(emergency_response.status_code, 200)
        self.assertNotContains(emergency_response, "تنبيه طارئ جديد")
        self.assertNotContains(
            emergency_response,
            reverse("dashboard:emergency_alert_delete", kwargs={"pk": old_alert.pk}),
        )
        self.assertEqual(
            self.client.get(reverse("dashboard:emergency_alert_create")).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("dashboard:emergency_alert_delete", kwargs={"pk": old_alert.pk})
            ).status_code,
            403,
        )
        self.assertTrue(EmergencyAlert.objects.filter(pk=old_alert.pk).exists())


@override_settings(CACHES=TEST_CACHES)
class EmergencyAlertsAndExcelImportTests(TestCase):
    def setUp(self):
        cache.clear()
        self.school = School.objects.create(name="مدرسة الطوارئ", slug="emergency-school")
        self.other_school = School.objects.create(name="مدرسة أخرى للطوارئ", slug="other-emergency-school")
        self.settings = SchoolSettings.objects.create(name=self.school.name, school=self.school)
        self.manager = get_user_model().objects.create_user(
            username="emergency_manager",
            password="StrongPass123!",
        )
        self.profile = UserProfile.objects.create(user=self.manager, active_school=self.school)
        self.profile.schools.add(self.school)
        plan = SubscriptionPlan.objects.create(
            code="emergency-test-plan",
            name="باقة اختبار الطوارئ",
            price=100,
            duration_days=30,
            max_screens=5,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate(),
            ends_at=timezone.localdate() + timedelta(days=30),
            status="active",
        )
        self.screen = DisplayScreen.objects.create(name="الشاشة الرئيسية", school=self.school)
        self.other_screen = DisplayScreen.objects.create(name="شاشة أخرى", school=self.school)
        self.client.force_login(self.manager)

    def test_manager_can_target_one_screen_and_cancel_with_audit_log(self):
        response = self.client.post(
            reverse("dashboard:emergency_alert_create"),
            {
                "kind": "fire",
                "title": "تنبيه حريق",
                "message": "تنفيذ الإخلاء فورًا",
                "schools": [self.school.pk],
                "scope": "screens",
                "screens": [self.screen.pk],
            },
        )
        self.assertRedirects(
            response,
            reverse("dashboard:emergency_alert_list"),
            fetch_redirect_response=False,
        )
        alert = EmergencyAlert.objects.get()
        self.assertEqual(alert.created_by, self.manager)
        self.assertEqual(list(alert.screens.all()), [self.screen])

        snap = {}
        _merge_real_data_into_snapshot(RequestFactory().get("/api/display/snapshot/"), snap, self.settings)
        self.assertEqual(snap["emergency_alerts"][0]["screen_ids"], [self.screen.pk])

        cancel = self.client.post(
            reverse("dashboard:emergency_alert_cancel", kwargs={"pk": alert.pk})
        )
        self.assertRedirects(
            cancel,
            reverse("dashboard:emergency_alert_list"),
            fetch_redirect_response=False,
        )
        alert.refresh_from_db()
        self.assertFalse(alert.is_active)
        self.assertEqual(alert.cancelled_by, self.manager)
        self.assertIsNotNone(alert.cancelled_at)

    def test_manager_can_delete_an_expired_emergency_alert(self):
        alert = EmergencyAlert.objects.create(
            kind=EmergencyAlert.KIND_URGENT,
            title="تنبيه قديم",
            message="انتهى هذا التنبيه",
            created_by=self.manager,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        alert.schools.add(self.school)

        list_response = self.client.get(reverse("dashboard:emergency_alert_list"))
        self.assertContains(
            list_response,
            reverse("dashboard:emergency_alert_delete", kwargs={"pk": alert.pk}),
        )

        response = self.client.post(
            reverse("dashboard:emergency_alert_delete", kwargs={"pk": alert.pk})
        )

        self.assertRedirects(
            response,
            reverse("dashboard:emergency_alert_list"),
            fetch_redirect_response=False,
        )
        self.assertFalse(EmergencyAlert.objects.filter(pk=alert.pk).exists())

    def test_manager_must_cancel_an_active_emergency_alert_before_deleting_it(self):
        alert = EmergencyAlert.objects.create(
            kind=EmergencyAlert.KIND_FIRE,
            title="تنبيه نشط",
            message="لا يمكن حذفه مباشرة",
            created_by=self.manager,
        )
        alert.schools.add(self.school)

        response = self.client.post(
            reverse("dashboard:emergency_alert_delete", kwargs={"pk": alert.pk})
        )

        self.assertRedirects(
            response,
            reverse("dashboard:emergency_alert_list"),
            fetch_redirect_response=False,
        )
        self.assertTrue(EmergencyAlert.objects.filter(pk=alert.pk).exists())

    def test_manager_cannot_delete_another_schools_emergency_alert(self):
        alert = EmergencyAlert.objects.create(
            kind=EmergencyAlert.KIND_WEATHER,
            title="تنبيه مدرسة أخرى",
            message="خارج نطاق المدير",
            created_by=self.manager,
            is_active=False,
        )
        alert.schools.add(self.other_school)

        response = self.client.post(
            reverse("dashboard:emergency_alert_delete", kwargs={"pk": alert.pk})
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(EmergencyAlert.objects.filter(pk=alert.pk).exists())

    def test_emergency_screen_selection_wins_over_stale_all_scope(self):
        response = self.client.post(
            reverse("dashboard:emergency_alert_create"),
            {
                "kind": "urgent",
                "title": "رسالة لشاشة واحدة",
                "message": "لا تعمم هذه الرسالة",
                "schools": [self.school.pk],
                "scope": "all",
                "screens": [self.screen.pk],
            },
        )

        self.assertRedirects(
            response,
            reverse("dashboard:emergency_alert_list"),
            fetch_redirect_response=False,
        )
        self.assertEqual(list(EmergencyAlert.objects.get().screens.all()), [self.screen])

    def test_manager_cannot_send_emergency_to_another_school(self):
        response = self.client.post(
            reverse("dashboard:emergency_alert_create"),
            {
                "kind": "urgent",
                "title": "رسالة",
                "message": "لا يجب إرسالها",
                "schools": [self.other_school.pk],
                "scope": "all",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(EmergencyAlert.objects.exists())
        self.assertTrue(response.context["form"].errors["schools"])

    def test_occasion_template_prefills_theme_and_bounded_display_window(self):
        response = self.client.get(
            reverse("dashboard:ann_create"),
            {"template": "national_day"},
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.initial["occasion_theme"], "national_day")
        self.assertEqual(
            form.initial["expires_at"] - form.initial["starts_at"],
            timedelta(hours=24),
        )
        self.assertContains(response, "الثيم يغيّر ألوان وخلفية وزخارف شاشة العرض")

    def test_occasion_theme_is_saved_and_exposed_to_display_snapshot(self):
        starts_at = timezone.now().replace(second=0, microsecond=0)
        response = self.client.post(
            reverse("dashboard:ann_create"),
            {
                "title": "دام عزك يا وطن",
                "body": "احتفاء باليوم الوطني",
                "level": "success",
                "occasion_theme": "national_day",
                "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M"),
                "expires_at": (starts_at + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M"),
                "is_active": "on",
            },
        )

        self.assertRedirects(
            response,
            reverse("dashboard:ann_list"),
            fetch_redirect_response=False,
        )
        announcement = Announcement.objects.get()
        self.assertEqual(announcement.occasion_theme, "national_day")
        snap = {}
        _merge_real_data_into_snapshot(
            RequestFactory().get("/api/display/snapshot/"),
            snap,
            self.settings,
        )
        self.assertEqual(snap["announcements"][0]["occasion_theme"], "national_day")
        self.assertEqual(
            snap["announcements"][0]["occasion_theme_label"],
            "اليوم الوطني السعودي",
        )

    def test_occasion_and_announcement_can_target_one_screen(self):
        starts_at = timezone.now().replace(second=0, microsecond=0)
        response = self.client.post(
            reverse("dashboard:ann_create"),
            {
                "title": "مناسبة في شاشة المدخل",
                "body": "هذه المناسبة ليست لجميع الشاشات",
                "level": "success",
                "occasion_theme": "national_day",
                "scope": "screens",
                "screens": [self.screen.pk],
                "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M"),
                "expires_at": (starts_at + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M"),
                "is_active": "on",
            },
        )

        self.assertRedirects(
            response,
            reverse("dashboard:ann_list"),
            fetch_redirect_response=False,
        )
        announcement = Announcement.objects.get()
        self.assertEqual(list(announcement.screens.all()), [self.screen])
        snap = {}
        _merge_real_data_into_snapshot(
            RequestFactory().get("/api/display/snapshot/"),
            snap,
            self.settings,
        )
        self.assertEqual(snap["announcements"][0]["screen_ids"], [self.screen.pk])

    def test_announcement_target_cannot_use_another_school_screen(self):
        foreign_screen = DisplayScreen.objects.create(
            name="شاشة مدرسة أخرى",
            school=self.other_school,
        )
        starts_at = timezone.now().replace(second=0, microsecond=0)
        response = self.client.post(
            reverse("dashboard:ann_create"),
            {
                "title": "تنبيه غير مسموح",
                "body": "اختبار العزل",
                "level": "info",
                "scope": "screens",
                "screens": [foreign_screen.pk],
                "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M"),
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Announcement.objects.exists())
        self.assertTrue(response.context["form"].errors["screens"])

    def test_excel_template_preview_and_atomic_import_create_full_timetable(self):
        parsed = parse_workbook(BytesIO(build_template_bytes()))
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(len(parsed["rows"]), 1)
        result = apply_import(school=self.school, parsed=parsed)
        self.assertEqual(result["lessons"], 1)
        self.assertTrue(Teacher.objects.filter(school=self.school, name="أحمد محمد").exists())
        self.assertTrue(Subject.objects.filter(school=self.school, name="الرياضيات").exists())
        self.assertTrue(SchoolClass.objects.filter(settings=self.settings, name="الأول أ").exists())
        self.assertTrue(DaySchedule.objects.filter(settings=self.settings, weekday=7).exists())
        self.assertTrue(
            ClassLesson.objects.filter(settings=self.settings, weekday=7, period_index=1).exists()
        )

    def test_excel_import_rejects_invalid_rows_before_writing(self):
        from openpyxl import load_workbook

        wb = load_workbook(BytesIO(build_template_bytes()))
        wb["الجدول"]["A2"] = "يوم غير صحيح"
        broken = BytesIO()
        wb.save(broken)
        broken.seek(0)
        parsed = parse_workbook(broken)
        self.assertTrue(parsed["errors"])
        with self.assertRaises(ValueError):
            apply_import(school=self.school, parsed=parsed)
        self.assertFalse(ClassLesson.objects.filter(settings=self.settings).exists())

    def test_excel_preview_identifies_header_and_teacher_conflicts_by_column(self):
        from openpyxl import load_workbook

        workbook = load_workbook(BytesIO(build_template_bytes()))
        workbook["الجدول"].append(
            ["الأحد", 1, "07:00", "07:45", "الثاني أ", "العلوم", "أحمد محمد"]
        )
        conflicted = BytesIO()
        workbook.save(conflicted)
        conflicted.seek(0)

        parsed = parse_workbook(conflicted)

        self.assertTrue(parsed["errors"])
        teacher_issue = next(
            issue for issue in parsed["issues"] if "مرتبط في الوقت نفسه" in issue["message"]
        )
        self.assertEqual(teacher_issue["row"], 3)
        self.assertEqual(teacher_issue["column"], "المعلم")

        workbook = load_workbook(BytesIO(build_template_bytes()))
        workbook["الجدول"]["A1"] = "اسم اليوم"
        bad_headers = BytesIO()
        workbook.save(bad_headers)
        bad_headers.seek(0)
        parsed_headers = parse_workbook(bad_headers)
        self.assertTrue(any(issue["row"] == 1 for issue in parsed_headers["issues"]))

    def test_excel_import_can_restore_the_previous_lesson(self):
        school_class = SchoolClass.objects.create(settings=self.settings, name="الأول أ")
        old_subject = Subject.objects.create(school=self.school, name="المادة السابقة")
        old_teacher = Teacher.objects.create(school=self.school, name="المعلم السابق")
        lesson = ClassLesson.objects.create(
            settings=self.settings,
            weekday=7,
            period_index=1,
            school_class=school_class,
            subject=old_subject,
            teacher=old_teacher,
        )
        parsed = parse_workbook(BytesIO(build_template_bytes()))

        result = apply_import(school=self.school, parsed=parsed)
        lesson.refresh_from_db()
        self.assertEqual(lesson.subject.name, "الرياضيات")
        self.assertEqual(lesson.teacher.name, "أحمد محمد")

        undo_import(school=self.school, undo=result["undo"])
        lesson.refresh_from_db()
        self.assertEqual(lesson.subject, old_subject)
        self.assertEqual(lesson.teacher, old_teacher)
        self.assertFalse(Subject.objects.filter(school=self.school, name="الرياضيات").exists())
        self.assertFalse(Teacher.objects.filter(school=self.school, name="أحمد محمد").exists())

    def test_excel_import_view_exposes_one_click_undo_for_latest_import(self):
        preview = self.client.post(
            reverse("dashboard:school_data_import"),
            {
                "action": "preview",
                "excel_file": SimpleUploadedFile(
                    "school-data.xlsx",
                    build_template_bytes(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            },
        )
        self.assertEqual(preview.status_code, 200)
        token = preview.context["import_token"]
        self.assertContains(preview, "الأثر المتوقع قبل الحفظ")

        imported = self.client.post(
            reverse("dashboard:school_data_import"),
            {"action": "import", "import_token": token},
        )
        self.assertEqual(imported.status_code, 302)
        completed_token = imported.url.split("completed=", 1)[1]
        completed = self.client.get(imported.url)
        self.assertContains(completed, "التراجع عن آخر استيراد")

        undone = self.client.post(
            reverse("dashboard:school_data_import"),
            {"action": "undo", "undo_token": completed_token},
        )
        self.assertRedirects(
            undone,
            reverse("dashboard:school_data_import"),
            fetch_redirect_response=False,
        )
        self.assertFalse(ClassLesson.objects.filter(settings=self.settings).exists())


class ConfirmationDialogAssetTests(SimpleTestCase):
    def test_data_confirm_uses_accessible_dialog_instead_of_browser_confirm(self):
        source = (Path(settings.BASE_DIR) / "static" / "js" / "csp-actions.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("window.confirm", source)
        self.assertIn('document.createElement("dialog")', source)
        self.assertIn('setAttribute("aria-labelledby"', source)
        self.assertIn("requestSubmit", source)


class SelfServiceSchoolCreationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="multi_school_manager",
            password="StrongPass123!",
        )
        self.current_school = School.objects.create(
            name="المدرسة الحالية",
            slug="current-self-service-school",
        )
        self.profile = UserProfile.objects.create(
            user=self.user,
            active_school=self.current_school,
        )
        self.profile.schools.add(self.current_school)
        self.one_screen_plan = SubscriptionPlan.objects.create(
            code="self-service-one-screen",
            name="سنوية - شاشة واحدة",
            price=500,
            duration_days=365,
            max_screens=1,
            is_active=True,
            sort_order=1,
        )
        self.three_screen_plan = SubscriptionPlan.objects.create(
            code="self-service-three-screens",
            name="سنوية - ثلاث شاشات",
            price=900,
            duration_days=365,
            max_screens=3,
            is_active=True,
            sort_order=2,
        )
        self.client.force_login(self.user)

    def test_manager_can_open_add_school_without_an_active_subscription(self):
        response = self.client.get(reverse("dashboard:add_school"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "إضافة مدرسة جديدة")
        self.assertContains(response, self.one_screen_plan.name)
        self.assertContains(response, self.three_screen_plan.name)

    def test_manager_creates_linked_inactive_school_then_continues_to_checkout(self):
        response = self.client.post(
            reverse("dashboard:add_school"),
            {
                "name": "مدرسة المستقبل",
                "school_type": "girls",
                "screen_count": "3",
                "plan": self.three_screen_plan.pk,
            },
        )

        school = School.objects.get(name="مدرسة المستقبل")
        self.profile.refresh_from_db()
        self.assertFalse(school.is_active)
        self.assertEqual(school.school_type, "girls")
        self.assertTrue(self.profile.schools.filter(pk=school.pk).exists())
        self.assertEqual(self.profile.active_school_id, school.pk)
        self.assertTrue(
            SchoolSettings.objects.filter(
                school=school,
                name=school.name,
                theme="rose",
            ).exists()
        )
        self.assertFalse(SchoolSubscription.objects.filter(school=school).exists())
        expected = (
            reverse("dashboard:my_subscription")
            + "?plan=self-service-three-screens&source=new_school#renewal-section"
        )
        self.assertRedirects(response, expected, fetch_redirect_response=False)

        checkout_response = self.client.get(expected.split("#", 1)[0])
        self.assertEqual(checkout_response.status_code, 200)
        self.assertEqual(checkout_response.context["school"], school)
        self.assertEqual(
            checkout_response.context["requested_plan"],
            self.three_screen_plan,
        )
        self.assertEqual(checkout_response.context["active_request_tab"], "new")

    def test_plan_must_match_selected_screen_count(self):
        response = self.client.post(
            reverse("dashboard:add_school"),
            {
                "name": "مدرسة باقة غير مطابقة",
                "school_type": "boys",
                "screen_count": "1",
                "plan": self.three_screen_plan.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "الباقة المختارة لا تطابق عدد الشاشات المحدد")
        self.assertFalse(School.objects.filter(name="مدرسة باقة غير مطابقة").exists())

    def test_free_plan_cannot_be_used_for_paid_school_creation(self):
        free_plan = SubscriptionPlan.objects.create(
            code="self-service-free",
            name="مجانية",
            price=0,
            duration_days=14,
            max_screens=1,
            is_active=True,
        )

        response = self.client.post(
            reverse("dashboard:add_school"),
            {
                "name": "مدرسة مجانية غير مسموحة",
                "school_type": "boys",
                "screen_count": "1",
                "plan": free_plan.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(School.objects.filter(name="مدرسة مجانية غير مسموحة").exists())


@override_settings(
    CACHES=TEST_CACHES,
    TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False,
)
class SystemAdminConsoleRegressionTests(TestCase):
    """Regressions for defects found while auditing the platform admin console."""

    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="console_owner",
            email="console-owner@example.com",
            password="StrongPass123!",
        )
        self.plan = SubscriptionPlan.objects.create(
            code="console-plan",
            name="باقة لوحة الإدارة",
            price=900,
            duration_days=365,
            max_screens=3,
        )
        self.subscribed_school = School.objects.create(
            name="مدرسة مشتركة", slug="console-subscribed"
        )
        self.bare_school = School.objects.create(
            name="مدرسة بلا اشتراك", slug="console-bare"
        )
        SchoolSubscription.objects.create(
            school=self.subscribed_school,
            plan=self.plan,
            starts_at=timezone.localdate(),
            ends_at=timezone.localdate() + timedelta(days=200),
            status="active",
        )

    def _create_employee(self, *, username="console_employee", permissions):
        employee = get_user_model().objects.create_user(
            username=username,
            password="StrongPass123!",
            is_staff=True,
        )
        SystemEmployeeProfile.objects.create(
            user=employee,
            role="custom",
            permission_keys=permissions,
            created_by=self.owner,
        )
        return employee

    def test_schools_list_filters_schools_without_a_live_subscription(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("dashboard:system_schools_list"), {"subscription": "none"}
        )

        self.assertEqual(response.status_code, 200)
        listed = {school.pk for school in response.context["schools"]}
        self.assertEqual(listed, {self.bare_school.pk})
        self.assertEqual(response.context["schools_without_subscription_count"], 1)

    def test_schools_list_filters_schools_with_a_live_subscription(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("dashboard:system_schools_list"), {"subscription": "active"}
        )

        listed = {school.pk for school in response.context["schools"]}
        self.assertEqual(listed, {self.subscribed_school.pk})

    def test_overview_alert_links_to_the_matching_schools_filter(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("dashboard:system_admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["schools_without_subscription_count"], 1)
        self.assertContains(
            response,
            reverse("dashboard:system_schools_list") + "?subscription=none",
        )

    def test_support_ticket_status_can_be_updated_from_the_detail_page(self):
        ticket = SupportTicket.objects.create(
            user=self.owner,
            school=self.subscribed_school,
            subject="شاشة لا تعمل",
            message="الشاشة الرئيسية متوقفة.",
        )
        self.client.force_login(self.owner)

        detail = self.client.get(
            reverse("dashboard:system_support_ticket_detail", args=[ticket.pk])
        )
        self.assertEqual(detail.status_code, 200)
        # The status form must carry a real submit control; it previously relied
        # on a data-autosubmit handler whose script was never loaded on the page.
        self.assertContains(detail, "تحديث الحالة")

        response = self.client.post(
            reverse("dashboard:system_support_ticket_detail", args=[ticket.pk]),
            {"status": "in_progress"},
        )

        self.assertRedirects(
            response,
            reverse("dashboard:system_support_ticket_detail", args=[ticket.pk]),
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "in_progress")

    def test_support_ticket_list_filters_by_status_and_paginates(self):
        for index in range(3):
            SupportTicket.objects.create(
                user=self.owner,
                school=self.subscribed_school,
                subject=f"تذكرة {index}",
                message="نص",
                status="closed" if index else "open",
            )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("dashboard:system_support_tickets"), {"status": "open"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["filtered_count"], 1)
        self.assertEqual(response.context["open_count"], 1)
        self.assertEqual(response.context["closed_count"], 2)
        self.assertIsNotNone(response.context["page_obj"])

    def test_delegated_employee_gets_the_console_shell_on_emergency_alerts(self):
        employee = self._create_employee(
            permissions=["dashboard.view", "emergency_alerts.view"]
        )
        self.client.force_login(employee)

        response = self.client.get(reverse("dashboard:emergency_alert_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["base_template"], "admin/admin_base.html")
        # The school shell offered manager-only links this identity cannot open.
        self.assertNotContains(response, reverse("dashboard:ann_list"))

    def test_owner_keeps_the_school_shell_on_emergency_alerts(self):
        self.client.force_login(self.owner)

        response = self.client.get(reverse("dashboard:emergency_alert_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["base_template"], "dashboard/_base.html")

    def test_admin_cannot_delete_their_own_account(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("dashboard:system_user_delete", args=[self.owner.pk])
        )

        self.assertRedirects(response, reverse("dashboard:system_users_list"))
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())

    def test_last_platform_owner_cannot_be_deleted(self):
        second_owner = get_user_model().objects.create_superuser(
            username="temporary_owner",
            email="temp-owner@example.com",
            password="StrongPass123!",
        )
        self.client.force_login(self.owner)

        # Two owners exist, so removing one is allowed.
        response = self.client.post(
            reverse("dashboard:system_user_delete", args=[second_owner.pk])
        )
        self.assertRedirects(response, reverse("dashboard:system_users_list"))
        self.assertFalse(get_user_model().objects.filter(pk=second_owner.pk).exists())

        # The remaining owner must survive an explicit delete attempt.
        deleter = self._create_employee(
            username="owner_deleter", permissions=["dashboard.view", "users.manage"]
        )
        self.client.force_login(deleter)
        response = self.client.post(
            reverse("dashboard:system_user_delete", args=[self.owner.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())

    def test_deleting_a_school_with_payment_records_deactivates_instead_of_crashing(self):
        MoyasarCheckout.objects.create(
            school=self.subscribed_school,
            plan=self.plan,
            request_type="new",
            amount=900,
        )
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("dashboard:system_school_delete", args=[self.subscribed_school.pk])
        )

        self.assertRedirects(response, reverse("dashboard:system_schools_list"))
        self.subscribed_school.refresh_from_db()
        self.assertTrue(self.subscribed_school.is_active)
        self.assertTrue(self.subscribed_school.display_suspended)

    def test_reports_hides_sections_outside_the_employee_permissions(self):
        employee = self._create_employee(
            username="reports_only_employee",
            permissions=["dashboard.view", "reports.view"],
        )
        self.client.force_login(employee)

        response = self.client.get(reverse("dashboard:system_reports"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("dashboard:system_support_tickets"))
        self.assertNotContains(
            response, reverse("dashboard:system_subscription_requests_list")
        )

    def test_screen_addon_form_preselects_the_requested_subscription(self):
        subscription = SchoolSubscription.objects.get(school=self.subscribed_school)
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("dashboard:system_screen_addon_create"),
            {"subscription": subscription.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].initial.get("subscription"), subscription.pk
        )

    def test_screen_addon_list_shows_the_pricing_cycle_label(self):
        subscription = SchoolSubscription.objects.get(school=self.subscribed_school)
        SubscriptionScreenAddon.objects.create(
            subscription=subscription,
            screens_added=2,
            pricing_cycle="semiannual",
            starts_at=timezone.localdate(),
            status="pending",
        )
        self.client.force_login(self.owner)

        response = self.client.get(reverse("dashboard:system_screen_addons_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["rows"][0]["pricing_cycle_label"], "نصف سنوي"
        )
        self.assertNotContains(response, "semiannual")


@override_settings(
    CACHES=TEST_CACHES,
    TWO_FACTOR_REQUIRED_FOR_PRIVILEGED=False,
)
class SystemAdminConsoleSmokeTests(TestCase):
    """Every platform console page must render for the owner and an employee."""

    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="smoke_owner",
            email="smoke-owner@example.com",
            password="StrongPass123!",
        )
        self.plan = SubscriptionPlan.objects.create(
            code="smoke-plan",
            name="باقة الفحص",
            price=1200,
            duration_days=365,
            max_screens=4,
        )
        self.school = School.objects.create(name="مدرسة الفحص", slug="smoke-school")
        self.subscription = SchoolSubscription.objects.create(
            school=self.school,
            plan=self.plan,
            starts_at=timezone.localdate(),
            ends_at=timezone.localdate() + timedelta(days=20),
            status="active",
        )
        self.manager = get_user_model().objects.create_user(
            username="smoke_manager", password="StrongPass123!"
        )
        UserProfile.objects.create(user=self.manager, active_school=self.school)
        self.manager.profile.schools.add(self.school)
        self.ticket = SupportTicket.objects.create(
            user=self.manager,
            school=self.school,
            subject="مشكلة فحص",
            message="نص المشكلة",
        )
        self.request = SubscriptionRequest.objects.create(
            school=self.school,
            plan=self.plan,
            request_type="renewal",
            amount=1200,
            requested_starts_at=timezone.localdate(),
        )
        self.addon = SubscriptionScreenAddon.objects.create(
            subscription=self.subscription,
            screens_added=2,
            pricing_cycle="annual",
            starts_at=timezone.localdate(),
            status="pending",
        )

    def _console_urls(self):
        return [
            reverse("dashboard:system_admin_dashboard"),
            reverse("dashboard:system_schools_list"),
            reverse("dashboard:system_school_create"),
            reverse("dashboard:system_school_edit", args=[self.school.pk]),
            reverse("dashboard:system_school_delete", args=[self.school.pk]),
            reverse("dashboard:system_users_list"),
            reverse("dashboard:system_user_create"),
            reverse("dashboard:system_user_edit", args=[self.manager.pk]),
            reverse("dashboard:system_user_delete", args=[self.manager.pk]),
            reverse("dashboard:system_employees_list"),
            reverse("dashboard:system_employee_create"),
            reverse("dashboard:system_subscriptions_list"),
            reverse("dashboard:system_subscription_create"),
            reverse("dashboard:system_subscription_edit", args=[self.subscription.pk]),
            reverse("dashboard:system_subscription_requests_list"),
            reverse("dashboard:system_subscription_request_detail", args=[self.request.pk]),
            reverse("dashboard:system_plans_list"),
            reverse("dashboard:system_plan_create"),
            reverse("dashboard:system_plan_edit", args=[self.plan.pk]),
            reverse("dashboard:system_plan_delete", args=[self.plan.pk]),
            reverse("dashboard:system_screen_addons_list"),
            reverse("dashboard:system_screen_addon_create"),
            reverse("dashboard:system_screen_addon_edit", args=[self.addon.pk]),
            reverse("dashboard:system_screen_addon_delete", args=[self.addon.pk]),
            reverse("dashboard:system_reports"),
            reverse("dashboard:system_support_tickets"),
            reverse("dashboard:system_support_ticket_detail", args=[self.ticket.pk]),
            reverse("dashboard:system_support_ticket_create"),
            reverse("dashboard:emergency_alert_list"),
            reverse("dashboard:emergency_alert_create"),
        ]

    def test_every_console_page_renders_for_the_owner(self):
        self.client.force_login(self.owner)
        for url in self._console_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, msg=url)

    def test_every_console_page_renders_for_a_full_access_employee(self):
        employee = get_user_model().objects.create_user(
            username="smoke_employee", password="StrongPass123!", is_staff=True
        )
        SystemEmployeeProfile.objects.create(
            user=employee,
            role="operations",
            permission_keys=role_permissions("operations"),
            created_by=self.owner,
        )
        self.client.force_login(employee)

        owner_only = {
            reverse("dashboard:system_employees_list"),
            reverse("dashboard:system_employee_create"),
        }
        for url in self._console_urls():
            if url in owner_only:
                continue
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, msg=url)

    def test_console_pages_render_a_single_top_level_heading(self):
        self.client.force_login(self.owner)
        for url in self._console_urls():
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertEqual(body.count("<h1"), 1, msg=url)


class ScreenInheritanceTests(TestCase):
    """تخصيص شاشة يجب ألا يقطع وراثتها من إعداد جميع الشاشات بلا قصد."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="inheritance_manager",
            password="StrongPass123!",
        )
        self.school = School.objects.create(name="مدرسة الوراثة", slug="inheritance-school")
        profile = UserProfile.objects.create(user=self.user, active_school=self.school)
        profile.schools.add(self.school)
        self.settings_obj = SchoolSettings.objects.create(
            school=self.school,
            name=self.school.name,
            theme="indigo",
        )
        plan = SubscriptionPlan.objects.create(
            code="inheritance-plan",
            name="باقة الوراثة",
            price=100,
            duration_days=365,
            max_screens=5,
            is_active=True,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate() - timedelta(days=1),
            ends_at=timezone.localdate() + timedelta(days=200),
            status="active",
        )
        self.screen = DisplayScreen.objects.create(name="شاشة البهو", school=self.school)
        self.client.force_login(self.user)

    def _payload(self, **overrides):
        payload = {
            "name": self.screen.name,
            "is_active": "on",
            "theme": "rose",
            "display_accent_color": "#EC4899",
            "occasion_theme": "auto",
            "featured_panel": "duty",
            "standby_scroll_speed": "1.1",
            "periods_scroll_speed": "0.9",
            "display_before_title": "عنوان خاص",
            "display_before_badge": "شارة",
            "display_after_title": "عنوان",
            "display_after_badge": "شارة",
            "display_after_holiday_title": "عنوان",
            "display_after_holiday_badge": "شارة",
            "display_holiday_title": "عنوان",
            "display_holiday_badge": "شارة",
            "show_announcements": "on",
            "show_period_classes": "on",
            "show_standby": "on",
            "show_duty": "on",
            "show_excellence": "on",
        }
        payload.update(overrides)
        return payload

    def test_saving_a_screen_while_inheriting_leaves_overrides_empty(self):
        response = self.client.post(
            reverse("dashboard:screen_edit", args=[self.screen.pk]),
            self._payload(inherit_groups=["appearance", "messages", "motion"]),
        )

        self.assertRedirects(
            response,
            reverse("dashboard:screen_list"),
            fetch_redirect_response=False,
        )
        self.screen.refresh_from_db()
        self.assertEqual(self.screen.theme_override, "")
        self.assertEqual(self.screen.display_accent_color_override, "")
        self.assertEqual(self.screen.featured_panel_override, "")
        self.assertIsNone(self.screen.standby_scroll_speed_override)
        self.assertIsNone(self.screen.periods_scroll_speed_override)
        self.assertEqual(self.screen.display_before_title_override, "")

    def test_groups_can_be_customized_independently(self):
        self.client.post(
            reverse("dashboard:screen_edit", args=[self.screen.pk]),
            self._payload(inherit_groups=["messages", "motion"]),
        )

        self.screen.refresh_from_db()
        # المظهر مخصص...
        self.assertEqual(self.screen.theme_override, "rose")
        self.assertEqual(self.screen.featured_panel_override, "duty")
        # ...بينما الرسائل والسرعات ما زالت موروثة من إعداد المدرسة.
        self.assertEqual(self.screen.display_before_title_override, "")
        self.assertIsNone(self.screen.standby_scroll_speed_override)

    def test_inherited_group_does_not_require_its_fields(self):
        payload = self._payload(inherit_groups=["messages"])
        for field in (
            "display_before_title",
            "display_before_badge",
            "display_after_title",
            "display_after_badge",
            "display_after_holiday_title",
            "display_after_holiday_badge",
            "display_holiday_title",
            "display_holiday_badge",
        ):
            payload[field] = ""

        response = self.client.post(
            reverse("dashboard:screen_edit", args=[self.screen.pk]),
            payload,
        )

        self.assertRedirects(
            response,
            reverse("dashboard:screen_list"),
            fetch_redirect_response=False,
        )
        self.screen.refresh_from_db()
        self.assertEqual(self.screen.display_holiday_title_override, "")

    def test_customized_group_rejects_blank_fields(self):
        response = self.client.post(
            reverse("dashboard:screen_edit", args=[self.screen.pk]),
            self._payload(inherit_groups=["appearance", "motion"], display_before_title=""),
        )

        self.assertEqual(response.status_code, 200)
        self.screen.refresh_from_db()
        self.assertEqual(self.screen.display_before_title_override, "")

    def test_screen_list_reports_inheritance_state(self):
        response = self.client.get(reverse("dashboard:screen_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "تتبع إعداد جميع الشاشات")

        self.screen.theme_override = "rose"
        self.screen.save(update_fields=["theme_override"])

        response = self.client.get(reverse("dashboard:screen_list"))
        self.assertContains(response, "تخصيص مستقل")

    def test_new_screen_starts_with_appearance_inheritance_off(self):
        self.settings_obj.theme = "emerald"
        self.settings_obj.display_accent_color = "#22C55E"
        self.settings_obj.featured_panel = SchoolSettings.FEATURE_PANEL_DUTY
        self.settings_obj.save(update_fields=["theme", "display_accent_color", "featured_panel"])

        response = self.client.post(
            reverse("dashboard:screen_create"),
            {
                "name": "شاشة جديدة",
                "is_active": "on",
            },
        )

        self.assertRedirects(
            response,
            reverse("dashboard:screen_list"),
            fetch_redirect_response=False,
        )
        new_screen = DisplayScreen.objects.get(name="شاشة جديدة", school=self.school)
        self.assertEqual(new_screen.theme_override, "emerald")
        self.assertEqual(new_screen.display_accent_color_override, "#22C55E")
        self.assertEqual(new_screen.featured_panel_override, SchoolSettings.FEATURE_PANEL_DUTY)

        response = self.client.get(reverse("dashboard:screen_edit", args=[new_screen.pk]))
        inherit_selected = response.context["inherit_selected"]
        self.assertNotIn(ScreenDisplayCustomizationForm.INHERIT_APPEARANCE, inherit_selected)

    def test_screen_limit_is_rechecked_under_lock_before_creating(self):
        DisplayScreen.objects.filter(school=self.school).delete()
        for index in range(5):
            DisplayScreen.objects.create(name=f"شاشة {index}", school=self.school)

        response = self.client.post(
            reverse("dashboard:screen_create"),
            {"name": "شاشة زائدة", "is_active": "on"},
        )

        self.assertRedirects(
            response,
            reverse("dashboard:screen_list"),
            fetch_redirect_response=False,
        )
        self.assertEqual(DisplayScreen.objects.filter(school=self.school).count(), 5)


class MultiSchoolManagerTests(TestCase):
    """مدير عدة مدارس: نظرة موحدة، وعدم حبسه بسبب مدرسة متعثرة."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="fleet_manager",
            password="StrongPass123!",
        )
        self.paid_school = School.objects.create(name="مدرسة مدفوعة", slug="paid-fleet-school")
        self.lapsed_school = School.objects.create(name="مدرسة متعثرة", slug="lapsed-fleet-school")
        self.profile = UserProfile.objects.create(user=self.user, active_school=self.lapsed_school)
        self.profile.schools.add(self.paid_school, self.lapsed_school)

        for school in (self.paid_school, self.lapsed_school):
            SchoolSettings.objects.create(school=school, name=school.name)

        plan = SubscriptionPlan.objects.create(
            code="fleet-plan",
            name="باقة الأسطول",
            price=700,
            duration_days=365,
            max_screens=4,
            is_active=True,
        )
        SchoolSubscription.objects.create(
            school=self.paid_school,
            plan=plan,
            starts_at=timezone.localdate() - timedelta(days=10),
            ends_at=timezone.localdate() + timedelta(days=300),
            status="active",
        )
        DisplayScreen.objects.create(name="شاشة مدفوعة", school=self.paid_school, is_active=True)
        DisplayScreen.objects.create(name="شاشة متعثرة", school=self.lapsed_school, is_active=True)
        self.client.force_login(self.user)

    def test_overview_lists_every_school_with_its_status(self):
        response = self.client.get(reverse("dashboard:schools_overview"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.paid_school.name)
        self.assertContains(response, self.lapsed_school.name)
        self.assertContains(response, "اشتراك ساري")
        self.assertContains(response, "الاشتراك منتهي")

    def test_lapsed_school_still_allows_support_and_switching(self):
        for view_name in (
            "dashboard:customer_support_tickets",
            "dashboard:customer_support_ticket_create",
            "dashboard:select_school",
            "dashboard:schools_overview",
        ):
            response = self.client.get(reverse(view_name))
            self.assertNotEqual(
                response.status_code,
                302,
                msg=f"{view_name} must stay reachable while the active school is lapsed",
            )

    def test_lapsed_school_still_blocks_product_pages(self):
        response = self.client.get(reverse("dashboard:screen_list"))

        self.assertRedirects(
            response,
            reverse("dashboard:my_subscription"),
            fetch_redirect_response=False,
        )

    def test_switching_school_from_overview_uses_post(self):
        response = self.client.post(
            reverse("dashboard:switch_school", args=[self.paid_school.pk]),
            {"next": reverse("dashboard:index")},
        )

        self.assertRedirects(
            response,
            reverse("dashboard:index"),
            fetch_redirect_response=False,
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.active_school_id, self.paid_school.pk)

    def test_overview_excludes_schools_outside_the_managers_profile(self):
        School.objects.create(name="مدرسة غريبة", slug="stranger-fleet-school")

        response = self.client.get(reverse("dashboard:schools_overview"))

        self.assertNotContains(response, "مدرسة غريبة")

    def test_dashboard_hero_summarises_the_fleet_when_school_has_many_screens(self):
        self.profile.active_school = self.paid_school
        self.profile.save(update_fields=["active_school"])
        DisplayScreen.objects.create(name="شاشة ثانية", school=self.paid_school, is_active=True)

        response = self.client.get(reverse("dashboard:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2 شاشة في مدرسة مدفوعة")
        self.assertContains(response, "تخصيص جميع الشاشات")


class AnnouncementScreenTargetingTests(TestCase):
    """استهداف شاشة معينة يجب ألا يضيع لأن الشاشة عُطّلت مؤقتًا."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="targeting_manager",
            password="StrongPass123!",
        )
        self.school = School.objects.create(name="مدرسة الاستهداف", slug="targeting-school")
        profile = UserProfile.objects.create(user=self.user, active_school=self.school)
        profile.schools.add(self.school)
        SchoolSettings.objects.create(school=self.school, name=self.school.name)
        plan = SubscriptionPlan.objects.create(
            code="targeting-plan",
            name="باقة الاستهداف",
            price=300,
            duration_days=365,
            max_screens=3,
            is_active=True,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate() - timedelta(days=1),
            ends_at=timezone.localdate() + timedelta(days=100),
            status="active",
        )
        self.screen = DisplayScreen.objects.create(
            name="شاشة الممر",
            school=self.school,
            is_active=True,
        )
        self.client.force_login(self.user)

    def test_disabled_but_targeted_screen_survives_an_edit(self):
        from notices.models import Announcement

        create_response = self.client.post(
            reverse("dashboard:ann_create"),
            {
                "title": "تنبيه موجه",
                "body": "نص التنبيه",
                "level": "info",
                "occasion_theme": "",
                "starts_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "scope": "screens",
                "screens": [self.screen.pk],
                "is_active": "on",
            },
        )
        self.assertRedirects(
            create_response,
            reverse("dashboard:ann_list"),
            fetch_redirect_response=False,
        )
        announcement = Announcement.objects.get(title="تنبيه موجه")
        self.assertEqual(list(announcement.screens.values_list("id", flat=True)), [self.screen.pk])

        # الشاشة تعطلت (يدويا أو تلقائيا عند تجاوز حد الباقة).
        self.screen.is_active = False
        self.screen.save(update_fields=["is_active"])

        form_response = self.client.get(reverse("dashboard:ann_edit", args=[announcement.pk]))
        self.assertEqual(form_response.status_code, 200)
        self.assertContains(form_response, self.screen.name)

        self.client.post(
            reverse("dashboard:ann_edit", args=[announcement.pk]),
            {
                "title": "تنبيه موجه محدث",
                "body": "نص التنبيه",
                "level": "info",
                "occasion_theme": "",
                "starts_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "scope": "screens",
                "screens": [self.screen.pk],
                "is_active": "on",
            },
        )
        announcement.refresh_from_db()
        self.assertEqual(announcement.title, "تنبيه موجه محدث")
        self.assertEqual(list(announcement.screens.values_list("id", flat=True)), [self.screen.pk])


class OccasionRegistryTests(TestCase):
    """السجل هو مصدر الحقيقة الوحيد؛ أي انحراف عنه يعني معاينة تكذب."""

    def test_model_choices_are_derived_from_the_registry(self):
        registry_keys = [item.key for item in occasions.all_occasions()]

        announcement_keys = [key for key, _label in Announcement.OCCASION_THEME_CHOICES if key]
        screen_keys = [
            key
            for key, _label in DisplayScreen.OCCASION_THEME_CHOICES
            if key not in ("auto", "off")
        ]

        self.assertEqual(announcement_keys, registry_keys)
        self.assertEqual(screen_keys, registry_keys)

    def test_retired_weather_theme_is_gone_from_every_layer(self):
        # «حالة جوية» تنبيه لا مناسبة، ومكرر في EmergencyAlert.
        self.assertNotIn("weather", occasions.OCCASIONS)
        self.assertNotIn("weather", dict(Announcement.OCCASION_THEME_CHOICES))
        self.assertNotIn("weather", dict(DisplayScreen.OCCASION_THEME_CHOICES))

    def test_saudi_calendar_is_covered(self):
        for key in ("national_day", "founding_day", "flag_day", "ramadan", "eid_fitr", "eid_adha"):
            self.assertIn(key, occasions.OCCASIONS, msg=f"{key} مفقودة من التقويم")

    def test_every_occasion_carries_a_complete_visual_identity(self):
        # حقل ناقص يعني ثيمًا يظهر بلا لون أو رمز على شاشة مدرسة.
        for occasion in occasions.all_occasions():
            with self.subTest(occasion=occasion.key):
                self.assertTrue(occasion.name)
                self.assertTrue(occasion.title)
                self.assertTrue(occasion.mark)
                self.assertTrue(occasion.badge_icon)
                self.assertEqual(len(occasion.symbols), 2)
                self.assertTrue(occasion.tagline)
                for colour in (occasion.deep, occasion.accent, occasion.highlight, occasion.soft):
                    self.assertRegex(colour, r"^#[0-9a-fA-F]{6}$")
                self.assertTrue(occasion.pattern_css)
                self.assertGreater(occasion.duration_hours, 0)

    def test_theme_map_and_card_list_cover_the_same_occasions(self):
        self.assertEqual(set(occasions.theme_map()), set(occasions.OCCASIONS))
        self.assertEqual(
            [card["key"] for card in occasions.card_list()],
            [item.key for item in occasions.all_occasions()],
        )


class OccasionScheduleTests(TestCase):
    """التواريخ الثابتة يجب أن تُحسب بدقة — هي أساس الاقتراح."""

    def test_gregorian_occasion_resolves_to_its_fixed_date(self):
        national_day = occasions.OCCASIONS["national_day"]

        self.assertEqual(
            national_day.schedule.next_occurrence(date(2026, 1, 1)),
            date(2026, 9, 23),
        )

    def test_gregorian_occasion_rolls_over_to_next_year_once_passed(self):
        flag_day = occasions.OCCASIONS["flag_day"]

        self.assertEqual(
            flag_day.schedule.next_occurrence(date(2026, 6, 1)),
            date(2027, 3, 11),
        )

    def test_occasion_on_its_own_day_is_still_returned(self):
        national_day = occasions.OCCASIONS["national_day"]

        self.assertEqual(
            national_day.schedule.next_occurrence(date(2026, 9, 23)),
            date(2026, 9, 23),
        )

    def test_hijri_occasion_resolves_through_the_hijri_calendar(self):
        ramadan = occasions.OCCASIONS["ramadan"]

        occurs_on = ramadan.schedule.next_occurrence(date(2026, 1, 1))

        self.assertIsNotNone(occurs_on)
        from hijridate import Gregorian

        hijri = Gregorian(occurs_on.year, occurs_on.month, occurs_on.day).to_hijri()
        self.assertEqual((hijri.month, hijri.day), (9, 1))

    def test_occasions_without_a_fixed_date_are_never_suggested(self):
        # التخرج وبداية العام قرار مدرسي لا تاريخ ثابت.
        for key in ("graduation", "back_to_school"):
            self.assertIsNone(occasions.OCCASIONS[key].schedule)

        suggestions = occasions.upcoming(date(2026, 9, 20), lead_days=365)
        suggested_keys = {item.occasion.key for item in suggestions}

        self.assertNotIn("graduation", suggested_keys)
        self.assertNotIn("back_to_school", suggested_keys)

    def test_upcoming_respects_the_lead_window(self):
        five_days_before = date(2026, 9, 18)

        within = occasions.upcoming(five_days_before, lead_days=10)
        outside = occasions.upcoming(five_days_before, lead_days=3)

        self.assertIn("national_day", {item.occasion.key for item in within})
        self.assertNotIn("national_day", {item.occasion.key for item in outside})

    def test_upcoming_is_sorted_by_proximity(self):
        results = occasions.upcoming(date(2026, 9, 1), lead_days=120)

        self.assertEqual(
            [item.days_left for item in results],
            sorted(item.days_left for item in results),
        )

    def test_countdown_labels_read_naturally_in_arabic(self):
        national_day = occasions.OCCASIONS["national_day"]
        labels = {}
        for days_before, expected in ((0, "اليوم"), (1, "غدًا"), (2, "بعد يومين"), (5, "بعد 5 أيام")):
            item = occasions.UpcomingOccasion(
                national_day, date(2026, 9, 23), days_before
            )
            labels[expected] = item.countdown_label

        for expected, actual in labels.items():
            self.assertEqual(actual, expected)


class OccasionSuggestionViewTests(TestCase):
    """الاقتراح يذكّر بلا أن يفعّل — التفعيل التلقائي مخاطرة لا تُغتفر."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="occasion_manager",
            password="StrongPass123!",
        )
        self.school = School.objects.create(name="مدرسة المناسبات", slug="occasion-school")
        profile = UserProfile.objects.create(user=self.user, active_school=self.school)
        profile.schools.add(self.school)
        SchoolSettings.objects.create(school=self.school, name=self.school.name)
        plan = SubscriptionPlan.objects.create(
            code="occasion-plan",
            name="باقة المناسبات",
            price=200,
            duration_days=365,
            max_screens=2,
            is_active=True,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate() - timedelta(days=1),
            ends_at=timezone.localdate() + timedelta(days=120),
            status="active",
        )
        self.client.force_login(self.user)

    def test_templates_page_previews_the_real_screen_colours_and_marks(self):
        response = self.client.get(reverse("dashboard:occasion_templates"))

        self.assertEqual(response.status_code, 200)
        for occasion in occasions.all_occasions():
            self.assertContains(response, occasion.accent)
            self.assertContains(response, occasion.name)

    def test_creating_an_announcement_from_a_template_prefills_registry_copy(self):
        response = self.client.get(
            reverse("dashboard:ann_create") + "?template=flag_day"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, occasions.OCCASIONS["flag_day"].title)

    def test_unknown_template_key_is_ignored_without_error(self):
        response = self.client.get(reverse("dashboard:ann_create") + "?template=not-real")

        self.assertEqual(response.status_code, 200)

    def test_suggestion_appears_when_an_occasion_is_near(self):
        near = occasions.OCCASIONS["national_day"].schedule.next_occurrence(
            timezone.localdate()
        )
        suggestions = occasions.upcoming(near - timedelta(days=4))

        self.assertIn("national_day", {item.occasion.key for item in suggestions})

    def test_suggestion_disappears_once_the_school_prepared_that_occasion(self):
        Announcement.objects.create(
            school=self.school,
            title="دام عزك يا وطن",
            body="نص",
            occasion_theme="national_day",
            is_active=True,
            starts_at=timezone.now(),
        )
        near = occasions.OCCASIONS["national_day"].schedule.next_occurrence(
            timezone.localdate()
        )

        remaining = views._occasion_suggestions_for(self.school, today=near - timedelta(days=4))

        self.assertNotIn("national_day", {item.occasion.key for item in remaining})

    def test_expired_announcement_does_not_silence_next_years_suggestion(self):
        # تنبيه العام الماضي انتهى؛ يجب أن يُذكَّر المدير بالمناسبة من جديد.
        Announcement.objects.create(
            school=self.school,
            title="دام عزك يا وطن",
            body="نص",
            occasion_theme="national_day",
            is_active=True,
            starts_at=timezone.now() - timedelta(days=400),
            expires_at=timezone.now() - timedelta(days=399),
        )
        near = occasions.OCCASIONS["national_day"].schedule.next_occurrence(
            timezone.localdate()
        )

        remaining = views._occasion_suggestions_for(self.school, today=near - timedelta(days=4))

        self.assertIn("national_day", {item.occasion.key for item in remaining})

    def test_dashboard_home_renders_without_suggestions_out_of_season(self):
        response = self.client.get(reverse("dashboard:index"))

        self.assertEqual(response.status_code, 200)


class TemplateCommentSyntaxTests(SimpleTestCase):
    """`{# ... #}` في Django تعليق سطر واحد فقط.

    تعليق يمتد لسطرين لا يُحذف عند التصيير — يُطبع حرفيًا في الصفحة فيقرأه
    المستخدم. هذا الاختبار يمنع عودة الخطأ في أي قالب.
    """

    def test_no_template_uses_a_multiline_hash_comment(self):
        offenders = []
        for path in sorted(Path(settings.BASE_DIR).joinpath("templates").rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\{#(.*?)#\}", text, re.S):
                if "\n" in match.group(1):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(settings.BASE_DIR)}:{line}")

        self.assertEqual(
            offenders,
            [],
            msg=(
                "تعليقات متعددة الأسطر تُعرض كنص للمستخدم. "
                "استخدم {% comment %}...{% endcomment %} بدلًا منها: "
                + ", ".join(offenders)
            ),
        )

    def test_django_really_does_render_multiline_hash_comments(self):
        # يوثّق سبب الاختبار أعلاه بدل الاعتماد على الحفظ.
        from django.template import Context, Template

        rendered = Template("A{# سطر\nسطر آخر #}B").render(Context({}))

        self.assertIn("سطر", rendered)


class OccasionCardRenderTests(TestCase):
    """أخطاء تظهر فقط عند التصيير الفعلي لصفحة القوالب."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="card_render_manager",
            password="StrongPass123!",
        )
        self.school = School.objects.create(name="مدرسة العرض", slug="card-render-school")
        profile = UserProfile.objects.create(user=self.user, active_school=self.school)
        profile.schools.add(self.school)
        SchoolSettings.objects.create(school=self.school, name=self.school.name)
        plan = SubscriptionPlan.objects.create(
            code="card-render-plan",
            name="باقة العرض",
            price=100,
            duration_days=365,
            max_screens=2,
            is_active=True,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate() - timedelta(days=1),
            ends_at=timezone.localdate() + timedelta(days=120),
            status="active",
        )
        self.client.force_login(self.user)

    def _page(self):
        response = self.client.get(reverse("dashboard:occasion_templates"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_page_renders_no_template_syntax_to_the_user(self):
        html = self._page()

        self.assertNotIn("{#", html)
        self.assertNotIn("{%", html)

    def test_every_mark_renders_monochrome_so_the_grid_stays_coherent(self):
        # الإيموجي الملوّن يتجاهل لون الثيم ويظهر بثقل بصري مختلف، فتبدو
        # الشبكة مفكّكة رغم أن باقي الرموز أنيقة وأحادية.
        html = self._page()

        coloured = sorted({char for char in html if ord(char) >= 0x1F300})

        self.assertEqual(coloured, [], msg=f"رموز ملوّنة في الصفحة: {coloured}")

    def test_action_button_rule_is_declared_once_so_cards_align(self):
        # قاعدتان بنفس المحدِّد كانتا تلغيان margin-top:auto، فتتفاوت مواضع
        # الأزرار بحسب طول نص كل بطاقة.
        html = self._page()

        self.assertEqual(html.count(".occasion-card-body > a {"), 1)
        self.assertIn("margin-top: auto", html)

    def test_masked_pattern_carries_the_webkit_prefix(self):
        html = self._page()

        self.assertIn("-webkit-mask-image", html)

    def test_card_count_matches_the_registry(self):
        html = self._page()

        self.assertEqual(html.count('<article class="occasion-card"'), len(occasions.all_occasions()))


class ArabicCountdownLabelTests(SimpleTestCase):
    """تمييز العدد في العربية: ٣–١٠ جمع، و١١ فأكثر مفرد منصوب."""

    def _label(self, days_left):
        occasion = occasions.OCCASIONS["national_day"]
        return occasions.UpcomingOccasion(occasion, date(2026, 9, 23), days_left).countdown_label

    def test_small_counts_use_the_plural_form(self):
        self.assertEqual(self._label(0), "اليوم")
        self.assertEqual(self._label(1), "غدًا")
        self.assertEqual(self._label(2), "بعد يومين")
        self.assertEqual(self._label(3), "بعد 3 أيام")
        self.assertEqual(self._label(10), "بعد 10 أيام")

    def test_eleven_and_above_use_the_singular_accusative(self):
        self.assertEqual(self._label(11), "بعد 11 يومًا")
        self.assertEqual(self._label(49), "بعد 49 يومًا")

    def test_no_count_ever_produces_the_ungrammatical_plural(self):
        for days in range(11, 61):
            with self.subTest(days=days):
                self.assertNotIn("أيام", self._label(days))
