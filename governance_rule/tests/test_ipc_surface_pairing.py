"""IPC surface pairing tests (Renderer→Preload→Main→Backend; isolated).

Verifies the INTEGRATION-03A/03B rules: Frontend Release A must talk to the
allowed Backend Release B; a backend requiring IPC the frontend does not
support is rejected (never break the official frontend).  Synthetic
surfaces live in temporary directories; the shipped surfaces are also
validated read-only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from governance_rule.execution.integrity.python_release_dependencies import (  # noqa: E402
    validate_ipc_surface_pairing,
)
from shared_layer.database import release_manifest  # noqa: E402


def _surface(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _pair(
    tmp_path: Path,
    *,
    frontend: dict[str, object],
    backend: dict[str, object],
    pin_frontend: str | None = None,
    pin_backend: str | None = None,
) -> list[str]:
    frontend_path = _surface(tmp_path / "frontend.json", frontend)
    backend_path = _surface(tmp_path / "backend.json", backend)
    contract = {
        "ipc_contract": {
            "surface_pairing": {
                "frontend_surface_file": "frontend.json",
                "backend_surface_file": "backend.json",
                "frontend_surface_version": pin_frontend or frontend.get("surface_version"),
                "backend_surface_version": pin_backend or backend.get("surface_version"),
            }
        }
    }
    return validate_ipc_surface_pairing(
        contract, repo_root=tmp_path
    )


def test_compatible_pair_passes(tmp_path: Path) -> None:
    frontend = {
        "surface_version": "fe1",
        "backend_commands": ["app:get-runtime-status", "toolbox_list_tools"],
        "events": ["app:get-runtime-status_result"],
        "error_codes": ["CAPABILITY_BOUNDARY_DENIED"],
        "features": {"cancellation": 1, "streaming": 1, "connection_state": 1},
    }
    backend = {
        "surface_version": "be1",
        "requires": {
            "commands": {"app:get-runtime-status": 1, "toolbox_list_tools": 1, "app:new-feature": 1},
            "events": {"app:get-runtime-status_result": 1},
            "error_codes": ["CAPABILITY_BOUNDARY_DENIED"],
            "features": {"cancellation": 1, "streaming": 1, "connection_state": 1},
        },
    }
    assert _pair(tmp_path, frontend=frontend, backend=backend) == []


def test_frontend_command_not_supported_by_backend_rejected(tmp_path: Path) -> None:
    frontend = {
        "surface_version": "fe1",
        "backend_commands": ["app:legacy-command"],
        "events": [],
        "error_codes": [],
        "features": {},
    }
    backend = {
        "surface_version": "be1",
        "requires": {"commands": {"app:other": 1}, "events": {}, "error_codes": [], "features": {}},
    }
    errors = _pair(tmp_path, frontend=frontend, backend=backend)
    assert "IPC_COMMAND_UNSUPPORTED:app:legacy-command" in errors


def test_event_not_emitted_rejected(tmp_path: Path) -> None:
    frontend = {
        "surface_version": "fe1",
        "backend_commands": ["app:a"],
        "events": ["app:a_result", "state_event_resync"],
        "error_codes": [],
        "features": {},
    }
    backend = {
        "surface_version": "be1",
        "requires": {
            "commands": {"app:a": 1},
            "events": {"app:a_result": 1},
            "error_codes": [],
            "features": {},
        },
    }
    errors = _pair(tmp_path, frontend=frontend, backend=backend)
    assert "IPC_EVENT_UNSUPPORTED:state_event_resync" in errors


def test_error_code_unsupported_rejected(tmp_path: Path) -> None:
    frontend = {
        "surface_version": "fe1",
        "backend_commands": [],
        "events": [],
        "error_codes": ["CAPABILITY_BOUNDARY_DENIED"],
        "features": {},
    }
    backend = {
        "surface_version": "be1",
        "requires": {
            "commands": {},
            "events": {},
            "error_codes": ["NEW_ERROR_CODE_V2"],
            "features": {},
        },
    }
    errors = _pair(tmp_path, frontend=frontend, backend=backend)
    assert "IPC_ERROR_CODE_UNSUPPORTED:NEW_ERROR_CODE_V2" in errors


def test_backend_requires_newer_ipc_rejected(tmp_path: Path) -> None:
    frontend = {
        "surface_version": "fe1",
        "backend_commands": [],
        "events": [],
        "error_codes": [],
        "features": {"streaming": 1, "cancellation": 1},
    }
    backend = {
        "surface_version": "be1",
        "requires": {
            "commands": {},
            "events": {},
            "error_codes": [],
            "features": {"streaming": 2},
        },
    }
    errors = _pair(tmp_path, frontend=frontend, backend=backend)
    assert "IPC_FEATURE_UNSUPPORTED:streaming" in errors


def test_pin_mismatch_rejected(tmp_path: Path) -> None:
    frontend = {"surface_version": "fe1", "backend_commands": [], "events": [], "error_codes": [], "features": {}}
    backend = {"surface_version": "be1", "requires": {"commands": {}, "events": {}, "error_codes": [], "features": {}}}
    errors = _pair(
        tmp_path, frontend=frontend, backend=backend, pin_frontend="other"
    )
    assert "IPC_SURFACE_PIN_MISMATCH:frontend" in errors


def test_unreadable_surface_rejected(tmp_path: Path) -> None:
    contract = {
        "ipc_contract": {
            "surface_pairing": {
                "frontend_surface_file": "missing.json",
                "backend_surface_file": "missing2.json",
            }
        }
    }
    errors = validate_ipc_surface_pairing(contract, repo_root=tmp_path)
    assert any(error.startswith("IPC_SURFACE_UNREADABLE:frontend") for error in errors)
    assert any(error.startswith("IPC_SURFACE_UNREADABLE:backend") for error in errors)


def test_shipped_surfaces_pair_read_only() -> None:
    contract = release_manifest.load_dependency_contract()
    errors = validate_ipc_surface_pairing(contract, repo_root=ROOT)
    assert errors == [], errors
