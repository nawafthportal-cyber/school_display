from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def privileged_two_factor_policy(app_configs, **kwargs):
    if not settings.DEBUG and not settings.TWO_FACTOR_REQUIRED_FOR_PRIVILEGED:
        return [Error("Privileged accounts must require two-factor authentication in production.", id="core.E001")]
    return []
