from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth import get_user
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import DisplayScreen, School, SubscriptionPlan, UserProfile
from schedule.models import SchoolSettings
from subscriptions.models import SchoolSubscription


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "website-trial-tests",
    }
}


@override_settings(CACHES=TEST_CACHES)
class TrialSignupTests(TestCase):
    def setUp(self):
        cache.clear()

    def _payload(self, **overrides):
        data = {
            "school_name": "مدرسة الاختبار",
            "school_type": "boys",
            "city": "الرياض",
            "contact_name": "أحمد محمد",
            "mobile": "0501234567",
            "email": "manager@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
        }
        data.update(overrides)
        return data

    def test_trial_signup_creates_ready_school_account(self):
        response = self.client.post(reverse("website:trial_signup"), data=self._payload())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(School.objects.count(), 1)
        self.assertEqual(UserProfile.objects.count(), 1)
        self.assertEqual(SchoolSubscription.objects.count(), 1)
        self.assertEqual(DisplayScreen.objects.count(), 1)
        self.assertEqual(SchoolSettings.objects.count(), 1)

        school = School.objects.get()
        profile = UserProfile.objects.select_related("active_school").get()
        subscription = SchoolSubscription.objects.select_related("plan", "school").get()

        self.assertEqual(profile.active_school, school)
        self.assertTrue(profile.schools.filter(pk=school.pk).exists())
        self.assertEqual(profile.mobile, "0501234567")
        self.assertEqual(profile.user.email, "manager@example.com")
        self.assertTrue(profile.needs_onboarding)
        self.assertEqual(subscription.school, school)
        self.assertEqual(subscription.status, "active")
        self.assertEqual(subscription.plan.code, "free-trial")
        self.assertEqual(subscription.plan.duration_days, 14)
        self.assertEqual(subscription.plan.max_screens, 1)
        self.assertTrue(get_user_model().objects.get().check_password("StrongPass123!"))
        self.assertEqual(response.json()["username"], get_user_model().objects.get().username)
        self.assertEqual(response.json()["mobile"], "0501234567")
        self.assertIn("login_url", response.json())
        self.assertEqual(response.json()["redirect_url"], reverse("dashboard:help_getting_started"))

    def test_trial_user_can_login_with_mobile_after_auto_signup(self):
        signup = self.client.post(reverse("website:trial_signup"), data=self._payload())
        self.assertEqual(signup.status_code, 200)
        self.client.logout()

        response = self.client.post(
            reverse("dashboard:login"),
            data={"username": "0501234567", "password": "StrongPass123!"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard:help_getting_started"))
        self.assertTrue(get_user(self.client).is_authenticated)

    def test_opening_getting_started_completes_first_login_redirect(self):
        self.client.post(reverse("website:trial_signup"), data=self._payload())
        profile = UserProfile.objects.get()
        self.assertTrue(profile.needs_onboarding)

        response = self.client.get(reverse("dashboard:help_getting_started"))

        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertFalse(profile.needs_onboarding)

    def test_signup_dialog_exposes_accessible_modal_and_mobile_contract(self):
        response = self.client.get(reverse("website:home"))

        self.assertContains(response, 'role="dialog"')
        self.assertContains(response, 'aria-modal="true"')
        self.assertContains(response, 'pattern="05[0-9]{8}"')
        self.assertContains(response, 'type="email"')
        self.assertContains(response, "يُستخدم لإرسال الفواتير واستعادة كلمة المرور")

    def test_landing_page_has_conversion_and_performance_contracts(self):
        response = self.client.get(reverse("website:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rel="canonical" href="https://school-display.com/"')
        self.assertContains(response, 'class="skip-link"')
        self.assertContains(response, 'id="heroTrialCta"')
        self.assertContains(response, 'id="heroVideoLink" href="#video"')
        self.assertContains(response, 'class="hero-price" href="#pricing"')
        self.assertContains(response, "screen-during-class.webp")
        self.assertContains(response, "screen-during-break.webp")
        self.assertContains(response, 'fetchpriority="high"')
        self.assertContains(response, 'id="shot-display" role="tabpanel"')
        self.assertContains(response, 'id="schoolOverviewVideo" controls playsinline preload="none"')
        self.assertContains(response, "school-display-overview.mp4")
        self.assertContains(response, "school-display-overview-ar.vtt")
        self.assertContains(response, "school-display-overview-poster.webp")
        self.assertContains(response, "school-dashboard.webp")
        self.assertNotContains(response, "dashboard-overview.png")
        self.assertNotContains(response, "display-screen.png")
        self.assertContains(response, "بلا بطاقة بنكية")
        self.assertContains(response, "كل المزايا مشمولة")
        self.assertContains(response, "تنبيهًا طارئًا لشاشة واحدة أو لكل الشاشات")
        self.assertContains(response, "شغّل شاشات مدرستك")
        self.assertContains(response, "كيف تستفيد المدرسة؟")
        self.assertContains(response, "هل يمكن تخصيص محتوى كل شاشة بشكل مستقل؟")
        self.assertContains(response, "استورد جدول الحصص من Excel")
        self.assertContains(response, "آخر محتوى محفوظ")
        self.assertContains(response, "اربط التلفاز بالجوال")
        self.assertContains(response, "إدارة المحتوى — 299 ر.س شهريًا")
        self.assertNotContains(response, "مجمع تعليمي: حتى 3 مدارس و15 شاشة")
        self.assertContains(response, "عام دراسي")
        self.assertContains(response, "الباقات والأسعار")
        self.assertContains(response, 'id="orderPlanSummary"')
        self.assertContains(response, 'data-form-step="1"')
        self.assertContains(response, 'data-form-step="2"')
        self.assertNotContains(response, "<iframe")

    def test_landing_pricing_uses_only_active_dashboard_plans(self):
        active_plan = SubscriptionPlan.objects.create(
            code="landing-annual",
            name="الباقة السنوية المتزامنة",
            description="وصف موحد يظهر في جميع واجهات الباقات",
            price=9876,
            duration_days=365,
            max_users=12,
            max_screens=4,
            sort_order=3,
            is_featured=True,
            is_active=True,
            card_badge_text="الخيار الأنسب للمدارس",
            card_duration_text="اشتراك مدرسي لمدة عام",
            card_price_caption="ريال سعودي شامل الضريبة / سنوي",
            card_monthly_text="أقل من 30 ريالاً لكل شاشة شهرياً",
            card_features="كل مزايا المنصة\nدعم فني مباشر\nتحديثات مستمرة",
            card_screen_text="تشغيل حتى 4 شاشات",
            card_cta_text="اشترك في الباقة الآن",
        )
        SubscriptionPlan.objects.create(
            code="landing-hidden",
            name="باقة مخفية من الهبوط",
            price=4321,
            duration_days=180,
            max_users=5,
            max_screens=1,
            is_active=False,
        )

        response = self.client.get(reverse("website:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, active_plan.name)
        self.assertContains(response, active_plan.description)
        self.assertContains(response, "الخيار الأنسب للمدارس")
        self.assertContains(response, "9876")
        self.assertContains(response, "اشتراك مدرسي لمدة عام")
        self.assertContains(response, "ريال سعودي شامل الضريبة / سنوي")
        self.assertContains(response, "أقل من 30 ريالاً لكل شاشة شهرياً")
        self.assertContains(response, "دعم فني مباشر")
        self.assertContains(response, "تشغيل حتى 4 شاشات")
        self.assertContains(response, "اشترك في الباقة الآن")
        self.assertContains(response, "data-plan-code=\"landing-annual\"")
        self.assertNotContains(response, "المستخدمون:")
        self.assertNotContains(response, "المدارس:")
        self.assertNotContains(response, "باقة مخفية من الهبوط")

    def test_landing_pricing_groups_all_terms_for_each_screen_count(self):
        monthly = SubscriptionPlan.objects.create(
            code="grouped-monthly",
            name="باقة مجمعة شهرية",
            price=240,
            duration_days=30,
            max_screens=3,
            is_active=True,
        )
        annual = SubscriptionPlan.objects.create(
            code="grouped-annual",
            name="باقة مجمعة سنوية",
            price=2200,
            duration_days=365,
            max_screens=3,
            is_active=True,
        )
        semiannual = SubscriptionPlan.objects.create(
            code="grouped-semiannual",
            name="باقة مجمعة نصف سنوية",
            price=1300,
            duration_days=182,
            max_screens=3,
            is_active=True,
        )

        response = self.client.get(reverse("website:home"))

        groups = dict(response.context["pricing_groups"])
        terms = {plan["code"]: plan["landing_duration"] for plan in groups[3]}
        self.assertEqual(terms[monthly.code], "شهر واحد")
        self.assertEqual(terms[semiannual.code], "6 أشهر")
        self.assertEqual(terms[annual.code], "سنة كاملة")
        for plan in (monthly, semiannual, annual):
            self.assertContains(response, f'data-plan-code="{plan.code}"')
        self.assertContains(response, 'name="landing-screen-count"')
        self.assertContains(response, 'data-pricing-choice="0" aria-pressed="true"')

    def test_landing_starting_price_updates_and_disappears_when_plan_is_inactive(self):
        monthly_plan = SubscriptionPlan.objects.get(code="school-screen-1-monthly")
        response = self.client.get(reverse("website:home"))
        self.assertContains(response, "<strong>119 ر.س</strong>", html=True)

        monthly_plan.price = Decimal("139.00")
        monthly_plan.save(update_fields=["price"])
        response = self.client.get(reverse("website:home"))
        self.assertContains(response, "<strong>139 ر.س</strong>", html=True)

        monthly_plan.is_active = False
        monthly_plan.save(update_fields=["is_active"])
        response = self.client.get(reverse("website:home"))
        self.assertNotContains(response, 'class="hero-price-amount"')
        self.assertContains(response, 'class="hero-price" href="#pricing"')

    def test_landing_pricing_reflects_dashboard_price_update_on_next_request(self):
        plan = SubscriptionPlan.objects.create(
            code="landing-live-price",
            name="باقة السعر اللحظي",
            price=8642,
            duration_days=90,
            max_users=6,
            max_screens=2,
            is_active=True,
        )
        first_response = self.client.get(reverse("website:home"))
        self.assertContains(first_response, "8642")

        plan.price = 7654
        plan.duration_days = 120
        plan.save(update_fields=["price", "duration_days"])

        second_response = self.client.get(reverse("website:home"))
        self.assertContains(second_response, "7654")
        self.assertContains(second_response, "120 يوماً")
        self.assertNotContains(second_response, "8642")
        self.assertIn("no-cache", second_response.headers.get("Cache-Control", ""))

    def test_trial_signup_rejects_duplicate_mobile(self):
        first = self.client.post(reverse("website:trial_signup"), data=self._payload())
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            reverse("website:trial_signup"),
            data=self._payload(school_name="مدرسة أخرى"),
        )

        self.assertEqual(second.status_code, 400)
        self.assertFalse(second.json()["ok"])
        self.assertIn("mobile", second.json()["errors"])
        self.assertEqual(School.objects.count(), 1)
        self.assertEqual(SubscriptionPlan.objects.filter(code="free-trial").count(), 1)

    def test_trial_signup_requires_a_valid_unique_email(self):
        missing = self.client.post(
            reverse("website:trial_signup"),
            data=self._payload(email=""),
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("email", missing.json()["errors"])

        first = self.client.post(reverse("website:trial_signup"), data=self._payload())
        self.assertEqual(first.status_code, 200)

        duplicate = self.client.post(
            reverse("website:trial_signup"),
            data=self._payload(
                school_name="مدرسة بريد أخرى",
                mobile="0512345678",
                email="MANAGER@example.com",
            ),
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("email", duplicate.json()["errors"])


@override_settings(CACHES=TEST_CACHES)
class PaidPlanJourneyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.plan = SubscriptionPlan.objects.create(
            code="paid-three-screens",
            name="باقة 3 شاشات",
            price=1190,
            duration_days=180,
            max_screens=3,
            is_active=True,
        )

    def _account(self, *, school_name="مدرسة العميل"):
        user = get_user_model().objects.create_user(
            username="customer",
            password="StrongPass123!",
        )
        school = School.objects.create(
            name=school_name,
            slug="customer-school",
            school_type="boys",
        )
        profile = UserProfile.objects.create(
            user=user,
            active_school=school,
            needs_onboarding=True,
        )
        profile.schools.add(school)
        return user, school

    def _order_url(self):
        return f'{reverse("website:plan_order")}?{urlencode({"plan": self.plan.code})}'

    def test_paid_plan_requires_login_and_preserves_the_selected_plan(self):
        response = self.client.get(self._order_url())

        self.assertEqual(response.status_code, 302)
        expected_next = urlencode({"next": self._order_url()})
        self.assertEqual(response.url, f'{reverse("dashboard:login")}?{expected_next}')

    def test_eligible_existing_account_gets_one_trial_then_reaches_selected_plan(self):
        user, school = self._account()
        self.client.force_login(user)

        response = self.client.get(self._order_url())

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"plan={self.plan.code}", response.url)
        self.assertIn("#renewal-section", response.url)
        trial = SchoolSubscription.objects.get(school=school)
        self.assertEqual(trial.plan.code, "free-trial")
        self.assertEqual(trial.status, "active")
        self.assertTrue(DisplayScreen.objects.filter(school=school).exists())
        self.assertTrue(SchoolSettings.objects.filter(school=school).exists())

        second = self.client.get(self._order_url())
        self.assertEqual(second.status_code, 302)
        self.assertEqual(SchoolSubscription.objects.filter(school=school).count(), 1)

    def test_any_previous_subscription_across_users_schools_blocks_a_new_trial(self):
        user, first_school = self._account(school_name="المدرسة الأولى")
        previous_plan = SubscriptionPlan.objects.create(
            code="previous-plan",
            name="اشتراك سابق",
            price=500,
            duration_days=30,
            is_active=False,
        )
        SchoolSubscription.objects.create(
            school=first_school,
            plan=previous_plan,
            status="expired",
        )
        second_school = School.objects.create(
            name="المدرسة الثانية",
            slug="second-customer-school",
            school_type="girls",
        )
        profile = user.profile
        profile.schools.add(second_school)
        profile.active_school = second_school
        profile.save(update_fields=["active_school"])
        self.client.force_login(user)

        response = self.client.get(self._order_url())

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            SchoolSubscription.objects.filter(
                school=second_school,
                plan__code="free-trial",
            ).exists()
        )

    def test_paid_plan_signup_redirects_to_plan_journey(self):
        response = self.client.post(
            reverse("website:trial_signup"),
            data={
                "school_name": "مدرسة الاشتراك الجديد",
                "school_type": "boys",
                "city": "الرياض",
                "contact_name": "أحمد محمد",
                "mobile": "0501234567",
                "email": "paid-signup@example.com",
                "password": "StrongPass123!",
                "password_confirm": "StrongPass123!",
                "plan_code": self.plan.code,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect_url"], self._order_url())
        self.assertEqual(SchoolSubscription.objects.count(), 1)

    def test_selected_paid_plan_is_preselected_on_subscription_page(self):
        user, _school = self._account()
        self.client.force_login(user)

        response = self.client.get(self._order_url(), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"تم اختيار {self.plan.name}")
        self.assertContains(response, f'value="{self.plan.pk}" selected')

    def test_login_honors_paid_plan_next_even_when_onboarding_is_pending(self):
        user, _school = self._account()

        response = self.client.post(
            reverse("dashboard:login"),
            data={
                "username": user.username,
                "password": "StrongPass123!",
                "next": self._order_url(),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self._order_url())


@override_settings(DISPLAY_WS_LIVE_STATUS_CHECK_SEC=60)
class DisplayRuntimeConfigTests(TestCase):
    @staticmethod
    def _activate(school):
        """Give the school a live subscription so the display gate lets it through."""
        plan = SubscriptionPlan.objects.create(
            code=f"display-plan-{school.slug}",
            name="خطة العرض",
            price=Decimal("100.00"),
            duration_days=365,
            max_screens=5,
        )
        return SchoolSubscription.objects.create(
            school=school,
            plan=plan,
            starts_at=timezone.localdate(),
            status="active",
        )

    def test_display_page_receives_the_configured_ws_safety_interval(self):
        school = School.objects.create(name="مدرسة العرض", slug="display-school")
        SchoolSettings.objects.create(name=school.name, school=school)
        screen = DisplayScreen.objects.create(name="الشاشة الرئيسية", school=school)
        self._activate(school)

        response = self.client.get(reverse("website:short_display", args=[screen.short_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-ws-live-status-check="60"')
        self.assertEqual(response["X-School-Display"], "board")

    def test_display_page_exposes_screen_specific_theme_and_visibility(self):
        school = School.objects.create(name="مدرسة التخصيص", slug="custom-display-school")
        SchoolSettings.objects.create(name=school.name, school=school, theme="emerald")
        screen = DisplayScreen.objects.create(
            name="شاشة المدخل",
            school=school,
            theme_override="violet",
            display_accent_color_override="#A855F7",
            standby_scroll_speed_override=1.1,
            periods_scroll_speed_override=0.9,
            display_before_title_override="مرحبًا بكم في شاشة المدخل",
            display_before_badge_override="أهلًا",
            occasion_theme="founding_day",
            featured_panel_override="duty",
            show_announcements=False,
            show_period_classes=True,
            show_standby=False,
            show_duty=True,
            show_excellence=False,
        )
        self._activate(school)

        response = self.client.get(reverse("website:short_display", args=[screen.short_code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-theme="violet"')
        self.assertContains(response, 'data-screen-theme="violet"')
        self.assertContains(response, 'data-screen-accent-color="#A855F7"')
        self.assertContains(response, 'data-standby="1.1"')
        self.assertContains(response, 'data-periods-speed="0.9"')
        self.assertContains(response, 'data-screen-display-before-title="مرحبًا بكم في شاشة المدخل"')
        self.assertContains(response, 'data-screen-display-before-badge="أهلًا"')
        self.assertContains(response, 'data-screen-occasion-theme="founding_day"')
        self.assertContains(response, 'data-screen-featured-panel="duty"')
        self.assertContains(response, 'data-screen-show-announcements="0"')
        self.assertContains(response, 'data-screen-show-period-classes="1"')
        self.assertContains(response, 'data-screen-show-standby="0"')
        self.assertContains(response, 'data-screen-show-duty="1"')
        self.assertContains(response, 'data-screen-show-excellence="0"')
