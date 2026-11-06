# dashboard/middleware.py
from __future__ import annotations

import logging

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import resolve, Resolver404
from django.utils import timezone
from django.conf import settings

from subscriptions.utils import school_has_active_subscription
from core.two_factor import get_enabled_config, two_factor_required_for
from .access import is_system_staff_user


logger = logging.getLogger(__name__)


class TwoFactorRequiredMiddleware:
    """Force privileged authenticated users to enroll before using the system."""

    EXEMPT_PREFIXES = (
        "/static/",
        "/media/",
        "/api/display/",
        "/dashboard/2fa/",
        "/dashboard/login/",
        "/dashboard/logout/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = str(getattr(request, "path", "") or "")
        if any(path.startswith(prefix) for prefix in self.EXEMPT_PREFIXES):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if (
            user
            and getattr(user, "is_authenticated", False)
            and two_factor_required_for(user)
            and get_enabled_config(user) is None
        ):
            return redirect("dashboard:two_factor_setup")
        return self.get_response(request)


class SubscriptionRequiredMiddleware:
    """
    يتحقق أن المدرسة النشطة (request.school) لديها اشتراك فعال قبل دخول /dashboard/

    - يعمل فقط على /dashboard/
    - لا يطبق على superuser ولا على موظفي المنصة ولا على غير المسجلين
    - يمنع تكرار رسالة الخطأ في نفس الجلسة

    البوابة تحجب *ميزات المنتج* فقط. أي مسار يحتاجه العميل ليعالج انقطاع
    اشتراكه — الدفع، الفواتير، الدعم، التنقل بين مدارسه، وأمان حسابه — يبقى
    مفتوحًا: حجبه يعاقب العميل في اللحظة التي يحتاج فيها الوصول أكثر من غيرها،
    ولا يضيف أي ضغط تحصيلي مفيد.
    """

    EXEMPT_VIEWS = {
        # الدخول والخروج
        "dashboard:login",
        "dashboard:logout",
        "dashboard:demo_login",
        # الدفع والفوترة: الطريق الوحيد لرفع الحجب
        "dashboard:my_subscription",
        "dashboard:subscription_invoice_view",
        "dashboard:subscription_receipt",
        "dashboard:add_school",
        # التنقل بين المدارس: مدرسة متعثرة يجب ألا تحبس بقية مدارس المدير
        "dashboard:switch_school",
        "dashboard:select_school",
        "dashboard:schools_overview",
        # الدعم: أكثر ما يحتاجه العميل عند تعثر الاشتراك
        "dashboard:customer_support_tickets",
        "dashboard:customer_support_ticket_create",
        "dashboard:customer_support_ticket_detail",
        # أمان الحساب
        "dashboard:change_password",
        "dashboard:two_factor_setup",
        "dashboard:two_factor_disable",
        "dashboard:two_factor_verify",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.process_request(request)
        if response is not None:
            return response
        return self.get_response(request)

    @staticmethod
    def _user_has_other_schools(user, school) -> bool:
        try:
            return user.profile.schools.exclude(pk=school.pk).exists()
        except Exception:
            return False

    def process_request(self, request):
        if getattr(settings, "MIDDLEWARE_DEBUG", False):
            logger.debug(
                "subscription_gate user=%s path=%s authenticated=%s superuser=%s",
                getattr(request, "user", None),
                getattr(request, "path", None),
                getattr(request.user, "is_authenticated", None),
                getattr(request.user, "is_superuser", None),
            )
        path = request.path or ""

        if path.startswith("/static/") or path.startswith("/media/"):
            return None

        if not path.startswith("/dashboard/"):
            return None

        if not request.user.is_authenticated:
            return None

        # السماح الفوري للمستخدم الخارق (superuser) بدون أي تحقق آخر
        if getattr(request.user, "is_superuser", False):
            return None

        # موظفو المنصة لا يتبعون اشتراك مدرسة بعينها.
        if is_system_staff_user(request.user):
            return None

        try:
            match = resolve(request.path_info)
            view_name = match.view_name or ""
        except Resolver404:
            view_name = ""

        if view_name in self.EXEMPT_VIEWS:
            return None

        school = getattr(request, "school", None)
        if not school:
            # لا تمنع هنا حتى لا تعمل لوب؛
            # خلي الفيوهات تتعامل مع عدم وجود مدرسة عند الحاجة.
            return None

        today = timezone.localdate()
        has_active = school_has_active_subscription(school_id=school.id, on_date=today)

        if has_active:
            request.session.pop("sub_blocked_once", None)
            return None

        if not request.session.get("sub_blocked_once"):
            # مدير متعدد المدارس يجب أن يعرف أن الحجب يخص هذه المدرسة وحدها،
            # وأن أمامه طريقًا واضحًا لبقية مدارسه.
            notice = f"⚠️ اشتراك «{school.name}» منتهي أو غير نشط. الرجاء مراجعة صفحة الاشتراك."
            if self._user_has_other_schools(request.user, school):
                notice += " يمكنك الانتقال إلى بقية مدارسك من صفحة «كل مدارسي»."
            messages.error(request, notice)
            request.session["sub_blocked_once"] = True

        return redirect("dashboard:my_subscription")


class SupportDashboardOnlyMiddleware:
    """Keep every platform employee inside authorized platform surfaces.

    Granular access is enforced by view decorators.  This middleware provides
    the tenant boundary: platform identities cannot drift into school-manager
    screens even if an old profile or bookmark exists.
    """

    ADMIN_PANEL_PREFIX = "/dashboard/admin-panel/"

    EXEMPT_VIEW_NAMES = {
        "dashboard:login",
        "dashboard:logout",
        "dashboard:change_password",
        "dashboard:switch_school",
        "dashboard:two_factor_setup",
        "dashboard:two_factor_disable",
        "dashboard:two_factor_verify",
    }

    PLATFORM_VIEWS_OUTSIDE_ADMIN_PREFIX = {
        "dashboard:emergency_alert_list",
        "dashboard:emergency_alert_create",
        "dashboard:emergency_alert_cancel",
        "dashboard:emergency_alert_delete",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = (getattr(request, "path", "") or "").strip()

        if not path:
            return self.get_response(request)

        # Always allow static/media and display API
        if path.startswith("/static/") or path.startswith("/media/") or path.startswith("/api/display/"):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return self.get_response(request)

        if getattr(user, "is_superuser", False):
            return self.get_response(request)

        is_platform_staff = is_system_staff_user(user)

        if not is_platform_staff:
            # A school manager may reach an internal admin URL from an old
            # bookmark or browser history. Send them back to their own panel
            # with a useful explanation instead of exposing Django's raw 403.
            if path.startswith(self.ADMIN_PANEL_PREFIX):
                try:
                    profile = user.profile
                    is_school_manager = profile.schools.exists()
                except Exception:
                    is_school_manager = False

                if is_school_manager:
                    messages.warning(
                        request,
                        "هذه الصفحة مخصصة لفريق إدارة النظام. تمت إعادتك إلى لوحة مدرستك.",
                    )
                    return redirect("dashboard:index")
            return self.get_response(request)

        # Resolve view name once.
        try:
            match = resolve(request.path_info)
            view_name = match.view_name or ""
        except Resolver404:
            view_name = ""

        # Always allow auth-related pages.
        if view_name in self.EXEMPT_VIEW_NAMES:
            return self.get_response(request)

        # Permission decorators provide the second, granular authorization
        # layer for every page accepted by this tenant boundary.
        if (
            path.startswith(self.ADMIN_PANEL_PREFIX)
            or view_name == "dashboard:system_admin_dashboard"
            or view_name in self.PLATFORM_VIEWS_OUTSIDE_ADMIN_PREFIX
        ):
            return self.get_response(request)

        # Anything else: force back to the system admin dashboard.
        return redirect("dashboard:system_admin_dashboard")
