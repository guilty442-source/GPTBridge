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
# A485: the learning module is owned by 星澄 (xingcheng package); its
# codex identity (learning-evidence-sync-sub-sovereign) is preserved as
# lineage while it operates as a commanded module, not a sovereign.
from .sovereigns.xingcheng.learning_sub_sovereign import (
    LearningEvidenceSyncSubSovereign,
)

__all__ = [
    "AutomationSovereign",
    "DecisionSovereign",
    "LearningEvidenceSyncSubSovereign",
    "PermissionSovereign",
    "SovereignBase",
    "SovereignIdentity",
    "SystemRuntimeSovereign",
    "XingchengSovereign",
]
