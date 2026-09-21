"""Configuration classification tests (five classes over existing stores).

RELEASE_CONFIG / RUNTIME_CONFIG / PERSISTENT_CONFIG / SECRET /
DEVELOPMENT_CONFIG.  Labels existing stores only; no second Configuration
Manager is created.  Synthetic stores live in temporary directories; the
shipped contract is validated read-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from governance_rule.execution.integrity.python_release_dependencies import (  # noqa: E402
    validate_config_classification,
)
from shared_layer.database import release_manifest  # noqa: E402


def _store(tmp_path: Path, relative: str) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def _contract(**classes: dict[str, object]) -> dict[str, object]:
    return {"config_classes": classes}


def test_shipped_classification_passes_read_only() -> None:
    contract = release_manifest.load_dependency_contract()
    errors = validate_config_classification(
        contract, release_root=ROOT, official_root=ROOT
    )
    assert errors == [], errors


def test_class_conflict_rejected(tmp_path: Path) -> None:
    _store(tmp_path, "main-system/config/a.json")
    _store(tmp_path, "tools/x/runtime/settings/a.json")
    contract = _contract(
        RELEASE_CONFIG={"paths": ["main-system/config/a.json"]},
        RUNTIME_CONFIG={
            "governed_roots": ["tools/*/runtime/settings"],
            "paths": ["main-system/config/a.json"],
        },
    )
    errors = validate_config_classification(
        contract, release_root=tmp_path, official_root=tmp_path
    )
    assert any(error.startswith("CONFIG_CLASS_CONFLICT") for error in errors)


def test_runtime_config_outside_governed_root_rejected(tmp_path: Path) -> None:
    _store(tmp_path, "tools/x/runtime/settings/ok.json")
    _store(tmp_path, "elsewhere/bad.json")
    contract = _contract(
        RUNTIME_CONFIG={
            "governed_roots": ["tools/*/runtime/settings"],
            "paths": ["tools/x/runtime/settings/ok.json", "elsewhere/bad.json"],
        },
    )
    errors = validate_config_classification(
        contract, release_root=tmp_path, official_root=tmp_path
    )
    assert "CONFIG_ROOT_VIOLATION:elsewhere/bad.json" in errors


def test_missing_release_config_rejected(tmp_path: Path) -> None:
    contract = _contract(
        RELEASE_CONFIG={"paths": ["main-system/config"]},
    )
    errors = validate_config_classification(
        contract, release_root=tmp_path, official_root=tmp_path
    )
    assert "CONFIG_MISSING:main-system/config" in errors


def test_runtime_and_development_config_inside_payload_rejected(tmp_path: Path) -> None:
    official = tmp_path / "official"
    payload = tmp_path / "payload"
    _store(official, "tools/x/runtime/settings/a.json")
    _store(payload, "tools/x/runtime/settings/a.json")
    _store(payload, "tests/conf.json")
    contract = _contract(
        RUNTIME_CONFIG={
            "governed_roots": ["tools/*/runtime/settings"],
            "paths": ["tools/x/runtime/settings/a.json"],
        },
        DEVELOPMENT_CONFIG={"paths": ["tests"]},
    )
    errors = validate_config_classification(
        contract,
        release_root=payload,
        official_root=official,
        check_release_payload=True,
    )
    assert any(
        error.startswith("CONFIG_INSIDE_RELEASE:RUNTIME_CONFIG") for error in errors
    )
    assert any(
        error.startswith("CONFIG_INSIDE_RELEASE:DEVELOPMENT_CONFIG")
        for error in errors
    )


def test_secret_config_in_payload_rejected(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir(parents=True)
    (payload / ".env").write_text("TOKEN=1\n", encoding="utf-8")
    (payload / "server.pem").write_text("pem\n", encoding="utf-8")
    contract = _contract(SECRET={"patterns": [".env", "*.pem"]})
    errors = validate_config_classification(
        contract,
        release_root=payload,
        official_root=tmp_path,
        check_release_payload=True,
    )
    assert any(error.startswith("SECRET_CONFIG_PACKAGED") for error in errors)


def test_persistent_config_not_overwritten(tmp_path: Path) -> None:
    _store(tmp_path, "tools/x/runtime/settings/user_rules.json")
    contract = _contract(
        RUNTIME_CONFIG={
            "governed_roots": ["tools/*/runtime/settings"],
            "paths": ["tools/x/runtime/settings/user_rules.json"],
        },
        PERSISTENT_CONFIG={"paths": ["tools/x/runtime/settings/user_rules.json"]},
    )
    errors = validate_config_classification(
        contract, release_root=tmp_path, official_root=tmp_path
    )
    assert any(error.startswith("CONFIG_CLASS_CONFLICT") for error in errors)
