"""Platform employee roles and fine-grained access policy.

This module intentionally has no database imports.  It is the single source of
truth shared by models, forms, decorators, middleware and templates.
"""

from __future__ import annotations

from collections.abc import Iterable


ROLE_SUPPORT = "support"
ROLE_FINANCE = "finance"
ROLE_CUSTOMER_SUCCESS = "customer_success"
ROLE_OPERATIONS = "operations"
ROLE_CUSTOM = "custom"

ROLE_CHOICES = (
    (ROLE_SUPPORT, "الدعم الفني"),
    (ROLE_FINANCE, "الإدارة المالية"),
    (ROLE_CUSTOMER_SUCCESS, "نجاح العملاء"),
    (ROLE_OPERATIONS, "إدارة العمليات"),
    (ROLE_CUSTOM, "دور مخصص"),
)


PERMISSION_DEFINITIONS = (
    {
        "key": "dashboard.view",
        "category": "overview",
        "category_label": "نظرة عامة",
        "label": "عرض مركز العمليات",
        "description": "الإحصائيات العامة ومؤشرات أداء المنصة.",
        "level": "view",
    },
    {
        "key": "schools.view",
        "category": "customers",
        "category_label": "المدارس والحسابات",
        "label": "عرض المدارس",
        "description": "استعراض المدارس وحالتها وخطتها وشاشاتها.",
        "level": "view",
    },
    {
        "key": "schools.manage",
        "category": "customers",
        "category_label": "المدارس والحسابات",
        "label": "إدارة المدارس",
        "description": "إضافة المدارس وتعديلها وحذفها.",
        "level": "manage",
        "implies": "schools.view",
    },
    {
        "key": "users.view",
        "category": "customers",
        "category_label": "المدارس والحسابات",
        "label": "عرض مدراء المدارس",
        "description": "استعراض حسابات العملاء وارتباطها بالمدارس.",
        "level": "view",
    },
    {
        "key": "users.manage",
        "category": "customers",
        "category_label": "المدارس والحسابات",
        "label": "إدارة مدراء المدارس",
        "description": "إنشاء حسابات العملاء وتعديلها وحذفها.",
        "level": "manage",
        "implies": "users.view",
    },
    {
        "key": "subscriptions.view",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "عرض الاشتراكات والفواتير",
        "description": "استعراض الاشتراكات والفواتير وحالات السداد.",
        "level": "view",
    },
    {
        "key": "subscriptions.manage",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "إدارة الاشتراكات",
        "description": "إنشاء الاشتراكات وتعديلها وإلغاؤها.",
        "level": "manage",
        "implies": "subscriptions.view",
    },
    {
        "key": "subscription_requests.view",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "عرض طلبات الاشتراك",
        "description": "متابعة طلبات الاشتراك والتجديد الواردة.",
        "level": "view",
    },
    {
        "key": "subscription_requests.manage",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "معالجة طلبات الاشتراك",
        "description": "اعتماد الطلبات أو رفضها وإرفاق الفواتير.",
        "level": "manage",
        "implies": "subscription_requests.view",
    },
    {
        "key": "plans.view",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "عرض الباقات والأسعار",
        "description": "استعراض الباقات والأسعار والحدود.",
        "level": "view",
    },
    {
        "key": "plans.manage",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "إدارة الباقات والأسعار",
        "description": "إضافة الباقات وتعديل أسعارها والخصائص المتاحة للاستخدام.",
        "level": "manage",
        "implies": "plans.view",
    },
    {
        "key": "discounts.view",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "عرض أكواد الخصم",
        "description": "استعراض أكواد الخصم واستخداماتها المتبقية.",
        "level": "view",
    },
    {
        "key": "discounts.manage",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "إدارة أكواد الخصم",
        "description": "إنشاء أكواد الخصم وتحديد مدتها وعددها وإيقافها.",
        "level": "manage",
        "implies": "discounts.view",
    },
    {
        "key": "screen_addons.view",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "عرض طلبات الشاشات الإضافية",
        "description": "استعراض إضافات الشاشات وتكلفتها.",
        "level": "view",
    },
    {
        "key": "screen_addons.manage",
        "category": "billing",
        "category_label": "الاشتراكات والمالية",
        "label": "إدارة الشاشات الإضافية",
        "description": "إنشاء إضافات الشاشات وتعديلها وحذفها.",
        "level": "manage",
        "implies": "screen_addons.view",
    },
    {
        "key": "reports.view",
        "category": "insights",
        "category_label": "التقارير والرقابة",
        "label": "عرض التقارير والإيرادات",
        "description": "الوصول إلى تقارير الأداء والنمو والإيرادات.",
        "level": "view",
    },
    {
        "key": "support.view",
        "category": "operations",
        "category_label": "الدعم والتشغيل",
        "label": "عرض تذاكر الدعم",
        "description": "قراءة تذاكر المدارس ومتابعة حالتها.",
        "level": "view",
    },
    {
        "key": "support.manage",
        "category": "operations",
        "category_label": "الدعم والتشغيل",
        "label": "معالجة تذاكر الدعم",
        "description": "الرد على التذاكر وتغيير حالتها وإنشاء تذكرة.",
        "level": "manage",
        "implies": "support.view",
    },
    {
        "key": "mail.view",
        "category": "operations",
        "category_label": "الدعم والتشغيل",
        "label": "عرض مركز البريد",
        "description": "قراءة الوارد والصادر وحالات تسليم رسائل العملاء.",
        "level": "view",
    },
    {
        "key": "mail.manage",
        "category": "operations",
        "category_label": "الدعم والتشغيل",
        "label": "إدارة البريد",
        "description": "إرسال الردود وإعادة محاولة إشعارات العملاء.",
        "level": "manage",
        "implies": "mail.view",
    },
    {
        "key": "emergency_alerts.view",
        "category": "operations",
        "category_label": "الدعم والتشغيل",
        "label": "عرض التنبيهات الطارئة",
        "description": "استعراض التنبيهات المرسلة لجميع المدارس.",
        "level": "view",
    },
    {
        "key": "emergency_alerts.manage",
        "category": "operations",
        "category_label": "الدعم والتشغيل",
        "label": "إدارة التنبيهات الطارئة",
        "description": "إرسال التنبيهات الشاملة وإلغاؤها.",
        "level": "manage",
        "implies": "emergency_alerts.view",
    },
)

PERMISSION_KEYS = frozenset(item["key"] for item in PERMISSION_DEFINITIONS)
PERMISSION_LABELS = {item["key"]: item["label"] for item in PERMISSION_DEFINITIONS}
IMPLIED_PERMISSIONS = {
    item["key"]: item["implies"]
    for item in PERMISSION_DEFINITIONS
    if item.get("implies")
}


ROLE_PRESETS = {
    ROLE_SUPPORT: {
        "label": "الدعم الفني",
        "description": "خدمة المدارس، متابعة الحسابات، ومعالجة تذاكر الدعم دون صلاحيات مالية حساسة.",
        "permissions": {
            "dashboard.view",
            "schools.view",
            "users.view",
            "subscriptions.view",
            "support.view",
            "support.manage",
            "mail.view",
            "mail.manage",
        },
    },
    ROLE_FINANCE: {
        "label": "الإدارة المالية",
        "description": "الاشتراكات والفواتير وطلبات السداد والتقارير المالية دون تعديل المدارس.",
        "permissions": {
            "dashboard.view",
            "schools.view",
            "subscriptions.view",
            "subscriptions.manage",
            "subscription_requests.view",
            "subscription_requests.manage",
            "plans.view",
            "discounts.view",
            "discounts.manage",
            "screen_addons.view",
            "reports.view",
            "mail.view",
        },
    },
    ROLE_CUSTOMER_SUCCESS: {
        "label": "نجاح العملاء",
        "description": "تهيئة المدارس ومدرائها ومتابعة طلباتها ودعمها بعد الاشتراك.",
        "permissions": {
            "dashboard.view",
            "schools.view",
            "schools.manage",
            "users.view",
            "users.manage",
            "subscriptions.view",
            "subscription_requests.view",
            "reports.view",
            "support.view",
            "support.manage",
            "mail.view",
            "mail.manage",
        },
    },
    ROLE_OPERATIONS: {
        "label": "إدارة العمليات",
        "description": "تشغيل المنصة يوميًا بصلاحيات واسعة، مع بقاء إدارة الموظفين لمالك النظام فقط.",
        "permissions": set(PERMISSION_KEYS),
    },
    ROLE_CUSTOM: {
        "label": "دور مخصص",
        "description": "ابدأ بصلاحية مركز العمليات ثم اختر بدقة ما يحتاجه الموظف.",
        "permissions": {"dashboard.view"},
    },
}


NAV_PERMISSION_MAP = {
    "home": "dashboard.view",
    "schools": "schools.view",
    "users": "users.view",
    "employees": "employees.owner_only",
    "subscriptions": "subscriptions.view",
    "payments": "subscriptions.view",
    "subscription_requests": "subscription_requests.view",
    "plans": "plans.view",
    "discounts": "discounts.view",
    "screen_addons": "screen_addons.view",
    "reports": "reports.view",
    "emergency_alerts": "emergency_alerts.view",
    "support": "support.view",
    "mail": "mail.view",
}


def normalize_permission_keys(values: Iterable[str] | None) -> list[str]:
    """Return valid, ordered permissions and include implied view access."""

    selected = {str(value).strip() for value in (values or ()) if str(value).strip()}
    selected.intersection_update(PERMISSION_KEYS)
    for manage_key, view_key in IMPLIED_PERMISSIONS.items():
        if manage_key in selected:
            selected.add(view_key)
    if selected:
        selected.add("dashboard.view")
    order = {item["key"]: index for index, item in enumerate(PERMISSION_DEFINITIONS)}
    return sorted(selected, key=lambda key: order[key])


def role_permissions(role: str) -> list[str]:
    preset = ROLE_PRESETS.get(str(role), ROLE_PRESETS[ROLE_CUSTOM])
    return normalize_permission_keys(preset["permissions"])


def role_label(role: str) -> str:
    return str(ROLE_PRESETS.get(str(role), ROLE_PRESETS[ROLE_CUSTOM])["label"])


def grouped_permission_definitions() -> list[dict]:
    groups: list[dict] = []
    by_key: dict[str, dict] = {}
    for item in PERMISSION_DEFINITIONS:
        category = item["category"]
        group = by_key.get(category)
        if group is None:
            group = {"key": category, "label": item["category_label"], "permissions": []}
            by_key[category] = group
            groups.append(group)
        group["permissions"].append(dict(item))
    return groups
