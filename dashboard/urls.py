from django.urls import path

from . import auth_views, support_views, two_factor_views, views, views_pairing, views_screens, views_themes
from mailcenter import dashboard_views as mail_views
from .receipt_views import subscription_receipt
from .views_document_import import school_document_import
 
app_name = "dashboard"

urlpatterns = [
    path("subscription-receipts/<path:asset>/", subscription_receipt, name="subscription_receipt"),
    # ==================
    # Auth / Account
    # ==================
    path("login/", auth_views.login_view, name="login"),
    path("password-reset/", auth_views.DashboardPasswordResetView.as_view(), name="password_reset"),
    path("password-reset/done/", auth_views.DashboardPasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.DashboardPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.DashboardPasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path("demo-login/", views.demo_login, name="demo_login"),
    path("logout/", auth_views.logout_view, name="logout"),
    path("2fa/verify/", two_factor_views.two_factor_verify, name="two_factor_verify"),
    path("2fa/setup/", two_factor_views.two_factor_setup, name="two_factor_setup"),
    path("2fa/disable/", two_factor_views.two_factor_disable, name="two_factor_disable"),
    path("subscription/invoices/<int:pk>/", views.subscription_invoice_view, name="subscription_invoice_view"),
    path("password/", auth_views.change_password, name="change_password"),
    path("switch-school/", views.switch_school, name="switch_school"),
    path("switch-school/<int:school_id>/", views.switch_school, name="switch_school"),
    path("", views.index, name="index"),
    path("select-school/", views.select_school, name="select_school"),
    path("schools/", views.schools_overview, name="schools_overview"),
    path("schools/add/", views.add_school, name="add_school"),
    path("help/getting-started/", views.help_getting_started, name="help_getting_started"),

    # ==================
    # Settings
    # ==================
    path("settings/", views.school_settings, name="settings"),
    path("themes/", views_themes.theme_list, name="theme_list"),
    path("themes/new/", views_themes.theme_edit, name="theme_create"),
    path("themes/<int:pk>/edit/", views_themes.theme_edit, name="theme_edit"),
    path("themes/<int:pk>/<str:action>/", views_themes.theme_action, name="theme_action"),

    # ==================
    # Lessons
    # ==================
    path("lessons/", views.lessons_list, name="lessons_list"),
    path("lessons/new/", views.lesson_create, name="add_lesson"),
    path("lessons/<int:pk>/edit/", views.lesson_edit, name="edit_lesson"),
    path("lessons/<int:pk>/delete/", views.lesson_delete, name="delete_lesson"),

    # ==================
    # Days & Day schedule
    # ==================
    path("days/", views.days_list, name="days_list"),
    # تقويم الإجازات: البُعد الذي يجهله الجدول الأسبوعي — التاريخ.
    path("closures/", views.closures_list, name="closures_list"),
    path("closures/<int:pk>/delete/", views.closure_delete, name="closure_delete"),
    path("days/<int:weekday>/", views.day_edit, name="day_edit"),
    path("days/<int:weekday>/toggle/", views.day_toggle, name="day_toggle"),
    path("days/<int:weekday>/autofill/", views.day_autofill, name="day_autofill"),
    path("days/<int:weekday>/clear/", views.day_clear, name="day_clear"),
    path("days/<int:weekday>/reindex/", views.day_reindex, name="day_reindex"),

    # ==================
    # Announcements
    # ==================
    path("announcements/", views.ann_list, name="ann_list"),
    path("announcements/new/", views.ann_create, name="ann_create"),
    path("announcements/<int:pk>/edit/", views.ann_edit, name="ann_edit"),
    path("announcements/<int:pk>/delete/", views.ann_delete, name="ann_delete"),
    path("emergency-alerts/", views.emergency_alert_list, name="emergency_alert_list"),
    path("emergency-alerts/new/", views.emergency_alert_create, name="emergency_alert_create"),
    path("emergency-alerts/<int:pk>/cancel/", views.emergency_alert_cancel, name="emergency_alert_cancel"),
    path("emergency-alerts/<int:pk>/delete/", views.emergency_alert_delete, name="emergency_alert_delete"),
    path("occasion-templates/", views.occasion_templates, name="occasion_templates"),
    path("occasion-templates/<slug:key>/edit/", views_themes.occasion_theme_edit, name="occasion_theme_edit"),

    # ==================
    # Excellence
    # ==================
    path("excellence/", views.exc_list, name="exc_list"),
    path("excellence/new/", views.exc_create, name="exc_create"),
    path("excellence/<int:pk>/edit/", views.exc_edit, name="exc_edit"),
    path("excellence/<int:pk>/delete/", views.exc_delete, name="exc_delete"),

    # ==================
    # Standby
    # ==================
    path("standby/", views.standby_list, name="standby_list"),
    path("standby/new/", views.standby_create, name="standby_create"),
    path("standby/<int:pk>/delete/", views.standby_delete, name="standby_delete"),

    # ==================
    # Duty / Supervision
    # ==================
    path("duty/", views.duty_list, name="duty_list"),
    path("duty/new/", views.duty_create, name="duty_create"),
    path("duty/<int:pk>/edit/", views.duty_edit, name="duty_edit"),
    path("duty/<int:pk>/delete/", views.duty_delete, name="duty_delete"),

    # ==================
    # Duty API
    # ==================
    path("api/duty/teachers/search/", views.duty_teacher_search, name="duty_teacher_search"),

    # ==================
    # Timetable
    # ==================
    path("timetable/day/", views.timetable_day_view, name="timetable_day"),
    path("timetable/week/", views.timetable_week_view, name="timetable_week"),
    path("timetable/export/", views.timetable_export_csv, name="timetable_export_csv"),

    # ==================
    # Screens
    # ==================
    path("screens/", views_screens.screen_list, name="screen_list"),
    path("screens/pair/", views_pairing.screen_pairing, name="screen_pairing"),
    path(
        "screens/pair/<uuid:pairing_id>/",
        views_pairing.screen_pairing_confirm,
        name="screen_pairing_confirm",
    ),
    path("screens/customize-all/", views_screens.screens_customize_all, name="screens_customize_all"),
    path("screens/new/", views_screens.screen_create, name="screen_create"),
    path("screens/<int:pk>/edit/", views_screens.screen_edit, name="screen_edit"),
    path("screens/<int:pk>/refresh/", views_screens.screen_refresh_now, name="screen_refresh_now"),
    path("screens/<int:pk>/reload/", views_screens.screen_reload_now, name="screen_reload_now"),
    path("screens/<int:pk>/unbind/", views_screens.screen_unbind_device, name="screen_unbind_device"),
    path("screens/<int:pk>/delete/", views_screens.screen_delete, name="screen_delete"),
    path("screens/request-addon/", views_screens.request_screen_addon, name="request_screen_addon"),

    # ==================
    # School Data (Classes / Subjects / Teachers)
    # ==================
    path("school-data/", views.school_data, name="school_data"),
    path("school-data/import/", views.school_data_import, name="school_data_import"),
    path("school-data/import/document/", school_document_import, name="school_document_import"),
    path("school-data/import/template/", views.school_data_import_template, name="school_data_import_template"),

    path("school-data/classes/add/", views.add_class, name="add_class"),
    path("school-data/classes/<int:pk>/delete/", views.delete_class, name="delete_class"),

    path("school-data/subjects/add/", views.add_subject, name="add_subject"),
    path("school-data/subjects/<int:pk>/delete/", views.delete_subject, name="delete_subject"),

    path("school-data/teachers/add/", views.add_teacher, name="add_teacher"),
    path("school-data/teachers/<int:pk>/delete/", views.delete_teacher, name="delete_teacher"),

    # ==================
    # Display API
    # ==================
 
    # ==================
    # SaaS Admin Panel
    # ==================
    path("admin-panel/", views.system_admin_dashboard, name="system_admin_dashboard"),

    # Schools
    path("admin-panel/schools/", views.system_schools_list, name="system_schools_list"),
    path("admin-panel/schools/add/", views.system_school_create, name="system_school_create"),
    path("admin-panel/schools/<int:pk>/edit/", views.system_school_edit, name="system_school_edit"),
    path("admin-panel/schools/<int:pk>/delete/", views.system_school_delete, name="system_school_delete"),

    # Users
    path("admin-panel/users/", views.system_users_list, name="system_users_list"),
    path("admin-panel/employees/", views.system_employees_list, name="system_employees_list"),
    path("admin-panel/employees/add/", views.system_employee_create, name="system_employee_create"),
    path("admin-panel/employees/<int:pk>/edit/", views.system_employee_edit, name="system_employee_edit"),
    path("admin-panel/users/add/", views.system_user_create, name="system_user_create"),
    path("admin-panel/users/<int:pk>/edit/", views.system_user_edit, name="system_user_edit"),
    path("admin-panel/users/<int:pk>/delete/", views.system_user_delete, name="system_user_delete"),

    # Subscriptions
    path("admin-panel/subscriptions/", views.system_subscriptions_list, name="system_subscriptions_list"),
    path("admin-panel/subscriptions/add/", views.system_subscription_create, name="system_subscription_create"),
    path("admin-panel/subscriptions/<int:pk>/edit/", views.system_subscription_edit, name="system_subscription_edit"),
    path("admin-panel/subscriptions/<int:pk>/delete/", views.system_subscription_delete, name="system_subscription_delete"),
    path("admin-panel/subscriptions/invoices/<int:pk>/", views.system_subscription_invoice_view, name="system_subscription_invoice_view"),

    # Payments and invoices ledger
    path("admin-panel/payments/", views.system_payments_list, name="system_payments_list"),
    path("admin-panel/payments/<int:pk>/", views.system_payment_detail, name="system_payment_detail"),

    # Subscription Requests (Renewal / New)
    path("admin-panel/subscription-requests/", views.system_subscription_requests_list, name="system_subscription_requests_list"),
    path("admin-panel/subscription-requests/<int:pk>/", views.system_subscription_request_detail, name="system_subscription_request_detail"),

    # Screen Add-ons
    path("admin-panel/screen-addons/", views.system_screen_addons_list, name="system_screen_addons_list"),
    path("admin-panel/screen-addons/add/", views.system_screen_addon_create, name="system_screen_addon_create"),
    path("admin-panel/screen-addons/<int:pk>/edit/", views.system_screen_addon_edit, name="system_screen_addon_edit"),
    path("admin-panel/screen-addons/<int:pk>/delete/", views.system_screen_addon_delete, name="system_screen_addon_delete"),

    # Plans
    path("admin-panel/plans/", views.system_plans_list, name="system_plans_list"),
    path("admin-panel/plans/add/", views.system_plan_create, name="system_plan_create"),
    path("admin-panel/plans/<int:pk>/edit/", views.system_plan_edit, name="system_plan_edit"),
    path("admin-panel/plans/<int:pk>/toggle/", views.system_plan_toggle, name="system_plan_toggle"),
    path("admin-panel/plans/<int:pk>/delete/", views.system_plan_delete, name="system_plan_delete"),

    # Discount codes
    path("admin-panel/discounts/", views.system_discounts_list, name="system_discounts_list"),
    path("admin-panel/discounts/add/", views.system_discount_create, name="system_discount_create"),
    path("admin-panel/discounts/<int:pk>/edit/", views.system_discount_edit, name="system_discount_edit"),
    path("admin-panel/discounts/<int:pk>/toggle/", views.system_discount_toggle, name="system_discount_toggle"),
    path("admin-panel/discounts/<int:pk>/delete/", views.system_discount_delete, name="system_discount_delete"),

    # Reports
    path("admin-panel/reports/", views.system_reports, name="system_reports"),

    # Platform mail center
    path("admin-panel/mail/", mail_views.system_mail_list, name="system_mail_list"),
    path("admin-panel/mail/compose/", mail_views.system_mail_compose, name="system_mail_compose"),
    path("admin-panel/mail/whatsapp/", mail_views.system_mail_whatsapp, name="system_mail_whatsapp"),
    path("admin-panel/mail/<int:pk>/", mail_views.system_mail_detail, name="system_mail_detail"),
    path("admin-panel/mail/<int:pk>/sync/", mail_views.system_mail_sync, name="system_mail_sync"),
    path("admin-panel/mail/queue/<int:pk>/retry/", mail_views.system_mail_queue_retry, name="system_mail_queue_retry"),

    # Support
    path("admin-panel/support/", support_views.system_support_tickets, name="system_support_tickets"),
    path("admin-panel/support/<int:pk>/", support_views.system_support_ticket_detail, name="system_support_ticket_detail"),
    path("admin-panel/support/add/", support_views.system_support_ticket_create, name="system_support_ticket_create"),

    # Customer Support
    path("support/", support_views.customer_support_tickets, name="customer_support_tickets"),
    path("support/new/", support_views.customer_support_ticket_create, name="customer_support_ticket_create"),
    path("support/<int:pk>/", support_views.customer_support_ticket_detail, name="customer_support_ticket_detail"),

    # My subscription
    path("my-subscription/", views.my_subscription, name="my_subscription"),
]
