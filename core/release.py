"""Non-sensitive revision identifier baked into every service image."""

import re
from functools import lru_cache

from django.conf import settings


@lru_cache(maxsize=1)
def release_label():
    try:
        revision = (settings.BASE_DIR / "BUILD_REVISION").read_text().strip()
    except OSError:
        revision = ""
    return revision[:12] if re.fullmatch(r"[a-f0-9]{40}", revision) else "تطوير"
