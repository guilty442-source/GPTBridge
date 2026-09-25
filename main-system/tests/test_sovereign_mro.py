"""Sovereign mixin MRO regression tests — no dead mixins, no broken init chain.

The sovereign split puts real behaviour in mixins; a base class or an
earlier mixin must not shadow them, and every mixin ``__init__`` must chain
through ``super()`` so the cooperative init reaches ``SovereignBase``.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT.parent / "shared-layer" / "src"))
sys.path.insert(0, str(ROOT.parent / "governance_rule"))

from governance.sovereigns.decision_sovereign import DecisionSovereign  # noqa: E402
from governance.sovereigns.permission_sovereign import (  # noqa: E402
    PermissionSovereign,
)
from governance.sovereigns.automation_sovereign import (  # noqa: E402
    AutomationSovereign,
)
from governance.sovereigns.system_runtime_sovereign import (  # noqa: E402
    SystemRuntimeSovereign,
)
from governance.sovereigns.xingcheng_sovereign import (  # noqa: E402
    XingchengSovereign,
)

SOVEREIGN_CLASSES = (
    DecisionSovereign,
    PermissionSovereign,
    AutomationSovereign,
    SystemRuntimeSovereign,
    XingchengSovereign,
)


def test_sovereigns_instantiate() -> None:
    """Every sovereign constructs: mixin __init__ chain reaches the base."""
    for cls in SOVEREIGN_CLASSES:
        instance = cls()
        assert instance.sovereign_id
        assert hasattr(instance, "_independent_verifier")


def test_mixin_init_chain_runs() -> None:
    """Attributes owned by later mixins exist — no dead __init__."""
    xingcheng = XingchengSovereign()
    assert xingcheng._reviews == {}
    assert xingcheng._program_tasks == {}
    assert xingcheng._auto_metrics["observe_cycles"] == 0
    assert xingcheng._auto_enabled is True
    self_upgrade = xingcheng.auto_status()["self_upgrade"]
    assert self_upgrade == {
        "owner": "xingcheng",
        "handling": "owned-domain-internal",
        "user_switch": False,
        "assistant_release_switch": False,
        "scope": "xingcheng-owned-domain-only",
    }
    assert xingcheng.app is None  # SovereignBase.__init__ ran


def test_mixin_method_ownership() -> None:
    """Each named method resolves to the mixin intended to own it."""
    assert XingchengSovereign._observe_domain.__qualname__.startswith(
        "XingchengDomainMixin."
    )
    assert XingchengSovereign._notify_anomalies.__qualname__.startswith(
        "XingchengReviewMixin."
    )
    assert DecisionSovereign._save_state.__qualname__.startswith(
        "DecisionStartupDispatchMixin."
    )
    assert PermissionSovereign._governance.__qualname__ == (
        "PermissionSovereign._governance"
    )



def test_no_shadowed_mixin_methods() -> None:
    """Within one class no two mixins may define the same method name —
    the later definition would be unreachable dead code (cooperative
    ``__init__`` excluded)."""
    for cls in SOVEREIGN_CLASSES:
        mixins = [
            c for c in cls.mro() if c.__name__.endswith("Mixin")
        ]
        seen: dict[str, str] = {}
        duplicates = []
        for mixin in mixins:
            for name in mixin.__dict__:
                if name.startswith("__"):
                    continue
                if name in seen:
                    duplicates.append(
                        f"{cls.__name__}.{name}: {seen[name]} shadows "
                        f"{mixin.__name__}"
                    )
                else:
                    seen[name] = mixin.__name__
        assert duplicates == []


def test_base_does_not_shadow_mixins() -> None:
    """SovereignBase/SubSovereignBase come last in every MRO."""
    for cls in SOVEREIGN_CLASSES:
        names = [c.__name__ for c in cls.mro()]
        for base in ("SovereignBase", "SubSovereignBase"):
            if base in names:
                assert names.index(base) > max(
                    names.index(m) for m in names if m.endswith("Mixin")
                )
