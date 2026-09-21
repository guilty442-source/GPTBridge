"""Architecture Registry single-source-of-truth tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from governance_rule.execution.audit.architecture_registry import (
    CANONICAL_AUTHORITY_KINDS,
    ArchitectureRegistryError,
    compatibility_shim_ids,
    components,
    discover_manifest_ids,
    load_registry,
    registry_path,
    validate,
    validate_dependency_graph,
    validate_manifest_coverage,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_DIR = Path(__file__).resolve().parents[1] / "execution" / "audit"


def _shim_record(sovereign_id: str = "maintenance") -> dict:
    return {
        "sovereign_id": sovereign_id,
        "status": "retired",
        "replacement": "health-maintenance-test-sub-sovereign",
        "deprecated_since": "A302",
        "removal_after": "zero-active-references",
        "new_reference_forbidden": True,
    }


def _base_payload() -> dict:
    return {
        "schema_version": 1,
        "registry_id": "test",
        "architectural_roles": ["governance", "data", "execution"],
        "runtime_forms": ["python-process", "database", "standalone-service"],
        "lifecycles": ["resident", "on-demand", "retired"],
        "sovereigns": {
            "active": ["decision", "system-runtime"],
            "compatibility_shims": [_shim_record()],
        },
        "canonical_authorities": dict(CANONICAL_AUTHORITY_KINDS),
        "components": [
            {
                "component_id": "postgresql",
                "architectural_role": "data",
                "runtime_form": "database",
                "owner_sovereign": "system-runtime",
                "owner_sub_sovereign": "",
                "execution_identity": "pg",
                "physical_path": ".",
                "lifecycle": "resident",
                "canonical": True,
                "dependencies": [],
                "information_channels": [],
            },
            {
                "component_id": "codex-authority",
                "architectural_role": "governance",
                "runtime_form": "database",
                "owner_sovereign": "decision",
                "owner_sub_sovereign": "",
                "execution_identity": "codex",
                "physical_path": ".",
                "lifecycle": "resident",
                "canonical": True,
                "dependencies": [],
                "information_channels": [],
            },
            {
                "component_id": "qdrant",
                "architectural_role": "data",
                "runtime_form": "database",
                "owner_sovereign": "system-runtime",
                "owner_sub_sovereign": "",
                "execution_identity": "qdrant",
                "physical_path": ".",
                "lifecycle": "resident",
                "canonical": True,
                "dependencies": [],
                "information_channels": [],
            },
        ],
    }


# ---------------------------------------------------------------------------
# the real registry must be internally consistent
# ---------------------------------------------------------------------------


def test_real_registry_validates_and_covers_manifests() -> None:
    payload = load_registry(registry_path(AUDIT_DIR))
    assert validate(payload, REPO_ROOT) == []
    assert validate_manifest_coverage(payload, REPO_ROOT) == []


def test_real_registry_has_no_retired_sovereign_owners() -> None:
    payload = load_registry(registry_path(AUDIT_DIR))
    retired = compatibility_shim_ids(payload)
    owners = {component.owner_sovereign for component in components(payload)}
    assert retired == {"maintenance", "automation"}
    assert not (owners & retired)
    # A592 retired the sub-sovereign orchestration model entirely.
    assert payload["execution_model"]["sub_sovereign"] == "retired-A592"


def test_real_registry_has_no_dependency_cycles() -> None:
    payload = load_registry(registry_path(AUDIT_DIR))
    assert validate_dependency_graph(payload) == []


def test_real_registry_declares_bounded_compatibility_shims() -> None:
    payload = load_registry(registry_path(AUDIT_DIR))
    for shim in payload["sovereigns"]["compatibility_shims"]:
        assert shim["status"] in {"retired", "deprecated"}
        assert shim["replacement"] and shim["replacement"] != shim["sovereign_id"]
        assert shim["deprecated_since"]
        assert shim["removal_after"]
        assert shim["new_reference_forbidden"] is True

    maintenance = payload["sovereigns"]["compatibility_shims"][0]
    assert maintenance["sovereign_id"] == "maintenance"
    assert maintenance["replacement"] == "health-maintenance-test-sub-sovereign"


def test_every_tool_component_has_a_manifest() -> None:
    payload = load_registry(registry_path(AUDIT_DIR))
    discovered = discover_manifest_ids(REPO_ROOT)
    for component in components(payload):
        if component.architectural_role == "execution":
            manifest = REPO_ROOT / component.physical_path / "manifest.json"
            if manifest.is_file():
                assert component.component_id in discovered


# ---------------------------------------------------------------------------
# drift detection
# ---------------------------------------------------------------------------


def test_unknown_role_runtime_form_and_owner_are_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["components"][0]["architectural_role"] = "not-a-role"
    payload["components"][1]["runtime_form"] = "not-a-form"
    payload["components"][2]["owner_sovereign"] = "ghost-sovereign"
    errors = validate(payload, tmp_path)
    assert any("unknown architectural_role" in error for error in errors)
    assert any("unknown runtime_form" in error for error in errors)
    assert any("not an active sovereign" in error for error in errors)


def test_retired_sovereign_owner_is_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["components"][0]["owner_sovereign"] = "maintenance"
    errors = validate(payload, tmp_path)
    assert any("compatibility shim only" in error for error in errors)


def test_missing_path_and_unknown_dependency_are_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["components"][0]["physical_path"] = "does-not-exist"
    payload["components"][1]["dependencies"] = ["ghost-component"]
    errors = validate(payload, tmp_path)
    assert any("physical_path does not exist" in error for error in errors)
    assert any("unknown dependency" in error for error in errors)


def test_canonical_authority_rules_are_enforced(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["canonical_authorities"]["structured-authority"] = "sqlite"
    payload["components"][1]["canonical"] = False
    errors = validate(payload, tmp_path)
    assert any("canonical authority mismatch" in error for error in errors)
    assert any("canonical authority is not marked canonical" in error for error in errors)


def test_duplicate_component_ids_are_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["components"].append(dict(payload["components"][0]))
    errors = validate(payload, tmp_path)
    assert any("duplicate component id" in error for error in errors)


def test_unknown_lifecycle_and_missing_taxonomy_are_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["components"][0]["lifecycle"] = "sometimes"
    errors = validate(payload, tmp_path)
    assert any("unknown lifecycle" in error for error in errors)

    payload = _base_payload()
    del payload["lifecycles"]
    errors = validate(payload, tmp_path)
    assert any("lifecycle taxonomy" in error for error in errors)


def test_dependency_cycle_is_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["components"][0]["dependencies"] = ["codex-authority"]
    payload["components"][1]["dependencies"] = ["postgresql"]
    errors = validate(payload, tmp_path)
    assert any("dependency graph contains a cycle" in error for error in errors)
    assert validate_dependency_graph(payload) != []


def test_self_dependency_is_a_cycle(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["components"][0]["dependencies"] = ["postgresql"]
    errors = validate(payload, tmp_path)
    assert any("dependency graph contains a cycle" in error for error in errors)


def test_incomplete_shim_record_is_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    del payload["sovereigns"]["compatibility_shims"][0]["removal_after"]
    errors = validate(payload, tmp_path)
    assert any("compatibility shim lacks required fields" in error for error in errors)


def test_shim_record_rules_are_enforced(tmp_path: Path) -> None:
    payload = _base_payload()
    shim = payload["sovereigns"]["compatibility_shims"][0]
    shim["status"] = "active"
    shim["replacement"] = "maintenance"
    shim["new_reference_forbidden"] = False
    errors = validate(payload, tmp_path)
    assert any("invalid status" in error for error in errors)
    assert any("replacement must differ" in error for error in errors)
    assert any("must forbid new references" in error for error in errors)


def test_shim_must_not_be_active_or_own_components(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["sovereigns"]["active"].append("maintenance")
    errors = validate(payload, tmp_path)
    assert any("compatibility shim is also declared active" in error for error in errors)


def test_manifest_not_registered_is_drift(tmp_path: Path) -> None:
    tool = tmp_path / "Standalone tools" / "ghost-tool"
    tool.mkdir(parents=True)
    (tool / "manifest.json").write_text(
        json.dumps({"id": "ghost-tool"}), encoding="utf-8"
    )
    payload = _base_payload()
    errors = validate_manifest_coverage(payload, tmp_path)
    assert any("manifest is not registered" in error for error in errors)


def test_schema_version_mismatch_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "architecture_registry.json"
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(ArchitectureRegistryError):
        load_registry(path)


def test_audit_check_reports_no_errors_on_the_real_repo() -> None:
    from governance_rule.execution.audit.audit_architecture import (
        check_architecture_registry,
    )

    errors: list[str] = []
    check_architecture_registry(REPO_ROOT, errors)
    assert errors == []


def test_architecture_document_report_shape() -> None:
    from governance_rule.execution.audit.architecture_docs import (
        architecture_document_report,
    )

    report = architecture_document_report(REPO_ROOT)
    for key in (
        "ok", "complete", "errors", "gaps", "documents",
        "document_count", "canonical_components",
    ):
        assert key in report, key
    assert isinstance(report["documents"], list)
    assert report["document_count"] == len(report["documents"])
    for document in report["documents"]:
        assert document["name"].startswith("architecture-")
        assert isinstance(document["mermaid"], bool)
        assert isinstance(document["referenced_components"], list)
    for gap in report["gaps"]:
        assert gap["component_id"] and gap["reason"]


def test_codex_diagnostic_routes_authorized() -> None:
    from governance_rule.permission_directory.registries.permissions.tool_routes import (
        authorize_ai_route,
        tool_actor,
    )

    for command in (
        "xingcheng_codex_alignment",
        "xingcheng_codex_mirror_check",
    ):
        authorize_ai_route(tool_actor("model-dialogue"), "xingcheng", command)
