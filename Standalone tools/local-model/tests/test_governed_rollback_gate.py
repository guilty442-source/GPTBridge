"""Governed rollback gate (2026-09-22 governor ruling: prune-latest first).

Rollback targets must be retained, certified and config-compatible; the
gate fails closed (WEIGHTS_ROLLBACK_DENIED) otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "backend"
    / "services"
)
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from xingcheng.infrastructure.native_transformer.lifecycle import (  # noqa: E402
    ModelLifecycle,
)

FINGERPRINT = "cfg-sha-aaa"


def _mk(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(b"w" * 32)
    return path


def _lifecycle(tmp_path: Path) -> ModelLifecycle:
    lc = ModelLifecycle("m1")
    lc.transition("INITIALIZED")
    return lc


def test_governed_rollback_accepts_certified_compatible(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    old = _mk(tmp_path, "v1.pt")
    lc.register_artifact(
        "weights",
        old,
        metadata={"maturity_level": 5, "config_sha256": FINGERPRINT},
    )
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v2.pt"),
        metadata={"maturity_level": 5, "config_sha256": FINGERPRINT},
        activate=True,
    )
    targets = lc.rollback_target_versions(compat_fingerprint=FINGERPRINT)
    assert targets == [1, 2]
    entry = lc.governed_rollback_weights(1, compat_fingerprint=FINGERPRINT)
    assert lc.active_weights_version == 1
    assert entry["version"] == 1
    assert lc.history[-1]["gate"] == "star-rollback-gate/v1"


def test_governed_rollback_denies_uncertified(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v1.pt"),
        metadata={"config_sha256": FINGERPRINT},
    )
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v2.pt"),
        metadata={"maturity_level": 5, "config_sha256": FINGERPRINT},
        activate=True,
    )
    assert lc.rollback_target_versions(compat_fingerprint=FINGERPRINT) == [2]
    with pytest.raises(ValueError, match="WEIGHTS_ROLLBACK_DENIED"):
        lc.governed_rollback_weights(1, compat_fingerprint=FINGERPRINT)
    assert lc.active_weights_version == 2


def test_governed_rollback_denies_incompatible(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v1.pt"),
        metadata={"maturity_level": 5, "config_sha256": "cfg-other"},
    )
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v2.pt"),
        metadata={"maturity_level": 5, "config_sha256": FINGERPRINT},
        activate=True,
    )
    with pytest.raises(ValueError, match="WEIGHTS_ROLLBACK_DENIED"):
        lc.governed_rollback_weights(1, compat_fingerprint=FINGERPRINT)


def test_governed_rollback_denies_without_anchor(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v1.pt"),
        metadata={"maturity_level": 5, "config_sha256": FINGERPRINT},
    )
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v2.pt"),
        metadata={"maturity_level": 5, "config_sha256": FINGERPRINT},
        activate=True,
    )
    assert lc.rollback_target_versions(compat_fingerprint=None) == []
    with pytest.raises(ValueError, match="WEIGHTS_ROLLBACK_DENIED"):
        lc.governed_rollback_weights(1, compat_fingerprint=None)


def test_governed_rollback_denies_pruned_version(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    old = _mk(tmp_path, "v1.pt")
    lc.register_artifact(
        "weights",
        old,
        metadata={"maturity_level": 5, "config_sha256": FINGERPRINT},
    )
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v2.pt"),
        metadata={"maturity_level": 5, "config_sha256": FINGERPRINT},
        activate=True,
    )
    old.unlink()
    assert lc.rollback_target_versions(compat_fingerprint=FINGERPRINT) == [2]
    with pytest.raises(ValueError, match="WEIGHTS_ROLLBACK_DENIED"):
        lc.governed_rollback_weights(1, compat_fingerprint=FINGERPRINT)


def test_governed_rollback_compat_resolver_fallback(tmp_path: Path) -> None:
    lc = _lifecycle(tmp_path)
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v1.pt"),
        metadata={"maturity_report": "maturity-1.json"},
    )
    lc.register_artifact(
        "weights",
        _mk(tmp_path, "v2.pt"),
        metadata={"maturity_level": 5},
        activate=True,
    )
    resolver = lambda path: FINGERPRINT if path.name == "v1.pt" else "nope"
    targets = lc.rollback_target_versions(
        compat_fingerprint=FINGERPRINT, compat_resolver=resolver
    )
    assert targets == [1]
