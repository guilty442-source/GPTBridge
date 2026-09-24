"""GOVERNANCE_LAYER — 治理層套件 facade。

A592/A604: the sub-sovereign layer is eliminated.  The former
``sub-sovereigns`` implementation package is removed; the five peer
cores coordinate through registered single-purpose modules instead
(FORBID:sub-sovereign-routing).  The only remaining codex child identity
is the A485 星澄-owned learning module, re-exported here.
"""

from __future__ import annotations

from .sovereigns import (
    DecisionSovereign,
    PermissionSovereign,
    SovereignBase,
    SovereignIdentity,
    AutomationSovereign,
    SystemRuntimeSovereign,
    XingchengSovereign,
)
# A485/A604: learning is an internal capability of the 星澄 own-domain
# sovereign (no module/child concept); its retired codex identity is
# preserved as lineage only.
from .sovereigns.xingcheng.learning_engine import (
    XingchengLearningEngine,
)

__all__ = [
    "AutomationSovereign",
    "DecisionSovereign",
    "PermissionSovereign",
    "SovereignBase",
    "SovereignIdentity",
    "SystemRuntimeSovereign",
    "XingchengLearningEngine",
    "XingchengSovereign",
]
