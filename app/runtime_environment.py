from __future__ import annotations

import site
import sys


def isolate_user_site_packages() -> None:
    user_site = str(site.USER_SITE or "")
    if not user_site:
        return
    sys.path[:] = [path for path in sys.path if path != user_site]


__all__ = ["isolate_user_site_packages"]
