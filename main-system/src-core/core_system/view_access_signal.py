"""View access signal functions — A186/E161.

View access status reporting for audit and observability (A186: AUDIT).
"""

from __future__ import annotations

from typing import Any

from core_system.view_access_types import (
    VIEW_LEVEL_DEFAULT,
    ViewGrant,
)


def view_access_status(
    grant: ViewGrant | None,
    *,
    effective_level: str = VIEW_LEVEL_DEFAULT,
) -> dict[str, Any]:
    """Return the view access status for audit and observability (A186: AUDIT).

    Per A186: ``AUDIT:requester+actor+level+scope+purpose+fields-category+
    decision+grant-id+time+correlation-id without-protected-content``.
    """
    return {
        "has_grant": grant is not None,
        "grant_level": grant.level if grant else "",
        "effective_level": effective_level,
        "actor_class": grant.actor_class if grant else "",
        "owner_scope": grant.owner_scope if grant else "",
        "purpose": grant.purpose if grant else "",
        "issued_at": grant.issued_at if grant else "",
        "expiry": grant.expiry if grant else "",
        "is_expired": grant.is_expired if grant else False,
        "authority": "permission-sovereign",
        "basis": "A186/E161",
        "default": VIEW_LEVEL_DEFAULT,
    }
