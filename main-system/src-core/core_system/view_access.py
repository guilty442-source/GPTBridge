"""Layered information system and tiered view access — A186/E161.

Per A186 (layered-information-system-and-tiered-view-access) and E161
(information-layer-view-access), the information layer is a 9-sublayer
pipeline, and view access is governed by tiered levels issued by the
Permission Sovereign.

This module is a re-export facade; the implementation lives in submodules:

  * :mod:`core_system.view_access_types` — constants and dataclasses.
  * :mod:`core_system.view_access_verify` — verification functions.
  * :mod:`core_system.view_access_signal` — signal/audit functions.
"""

from __future__ import annotations

from core_system.view_access_signal import view_access_status
from core_system.view_access_types import (
    ACTOR_CEILINGS,
    FieldPolicyResult,
    INFORMATION_LAYER_FLOW,
    INFORMATION_LAYER_SUBLAYERS,
    NON_VIEWABLE_SECRETS,
    VIEW_LEVELS,
    VIEW_LEVEL_DEFAULT,
    ViewGrant,
)
from core_system.view_access_verify import (
    apply_field_policy,
    effective_view_level,
    verify_sublayer_pipeline,
)

__all__ = [
    "ACTOR_CEILINGS",
    "FieldPolicyResult",
    "INFORMATION_LAYER_FLOW",
    "INFORMATION_LAYER_SUBLAYERS",
    "NON_VIEWABLE_SECRETS",
    "VIEW_LEVELS",
    "VIEW_LEVEL_DEFAULT",
    "ViewGrant",
    "apply_field_policy",
    "effective_view_level",
    "verify_sublayer_pipeline",
    "view_access_status",
]
