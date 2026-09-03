from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))


def test_local_sql_engine_declared_and_no_physical_content_in_index() -> None:
    from governance_rule.permission_directory.directory_authority import (
        directory_authority_snapshot,
    )
    from governance_rule.governance_policy import governance_policy_snapshot

    authority = directory_authority_snapshot().shared_layer_access_policy
    policy = governance_policy_snapshot().shared_layer
    assert authority.database_path.startswith("postgresql:")
    assert authority.ai_database_path.startswith("postgresql:")
    assert authority.database_path != authority.ai_database_path
    assert authority.central_index_engine == "postgresql"
    assert policy.database_path == authority.database_path
    assert policy.ai_database_path == authority.ai_database_path


def test_xingcheng_has_select_only_grants() -> None:
    sql = (ROOT / "sql" / "central_index.sql").read_text(encoding="utf-8")
    star_lines = [line.strip() for line in sql.splitlines() if "gptbridge_xingcheng_reader" in line]
    assert star_lines
    assert not any(line.startswith("GRANT INSERT") or line.startswith("GRANT UPDATE") or line.startswith("GRANT DELETE") for line in star_lines)
    migration = (ROOT / "migrations" / "001_registry_locations.sql").read_text(encoding="utf-8")
    assert "physical_location" not in migration.split("CREATE OR REPLACE VIEW registry.resource_locations", 1)[1].split("FROM registry.locations", 1)[0]


def test_python_gateway_is_default_deny_and_xingcheng_read_only() -> None:
    from shared_layer.access_gateway import gateway as module

    denied = module.AccessGateway(lambda *_: False)
    own = module.Principal("tool-a", "file-sorter")
    assert denied.decide(own, "read", "file-sorter").allowed is False
    assert denied.decide(own, "read", "vaultly").reason == "CROSS_MODULE_DEFAULT_DENY"
    star = module.Principal("xingcheng", "xingcheng", is_xingcheng=True)
    allowed = module.AccessGateway(lambda *_: True)
    assert allowed.decide(star, "read", "vaultly").allowed is True
    assert allowed.decide(star, "update", "vaultly").reason == "XINGCHENG_CROSS_MODULE_READ_ONLY"
    assert allowed.decide(star, "update", "xingcheng").allowed is True
    assert allowed.decide(star, "manage", "xingcheng").allowed is True
    assert allowed.decide(
        star, "update", "xingcheng", resource_class="permission-file"
    ).reason == "PROTECTED_AUTHORITY_READ_ONLY"
    assert allowed.decide(
        star, "read", "xingcheng", resource_class="permission-file"
    ).allowed is True
    assert allowed.decide(star, "execute", "system-rescue").allowed is False


def test_local_rag_runtime_is_fixed_location(tmp_path: Path) -> None:
    from shared_layer.rag_bridge import local_runtime as module

    runtime = module.runtime_for(tmp_path)
    assert runtime.index_root == (tmp_path / "shared-layer" / "runtime" / "semantic-index").resolve()
    try:
        module.LocalRagRuntime(tmp_path / "private" / "runtime" / "qdrant")
    except ValueError as exc:
        assert str(exc) == "LOCAL_SEMANTIC_INDEX_LOCATION_INVALID"
    else:
        raise AssertionError("out-of-contract index root accepted")


def test_local_hits_require_authorization_and_no_content_payload() -> None:
    from shared_layer.rag_bridge import bridge as module

    hits = (
        module.QdrantHit("R1", "C1", "vaultly", 0.9),
        module.QdrantHit("R2", "C2", "file-sorter", 0.8),
    )
    bridge = module.RagAuthorizationBridge(lambda _actor, resource: resource == "R2")
    assert tuple(hit.resource_id for hit in bridge.filter_authorized("xingcheng", hits)) == ("R2",)

    class Store:
        def replace_document(self, *_args, **_kwargs):
            raise AssertionError("invalid payload reached the index")

    coordinator = module.RagIndexCoordinator(Store(), lambda *_: None, lambda *_: None)
    try:
        coordinator.upsert("R1", "vaultly", [{"id": "P1", "payload": {"chunk_id": "C1", "content": "secret"}}])
    except ValueError as exc:
        assert str(exc) == "RAG_PAYLOAD_MUST_NOT_CONTAIN_PHYSICAL_CONTENT_OR_PATH"
    else:
        raise AssertionError("physical content was accepted into index payload")


def test_no_installer_or_docker_dependency_in_python_core() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src" / "shared_layer").rglob("*.py"))
    forbidden = ("pip install", "winget install", "choco install", "docker compose", "docker run")
    assert not any(command in sources.casefold() for command in forbidden)


def test_xingcheng_self_database_write_is_executor_only() -> None:
    manifest = json.loads((ROOT.parent / "local-model" / "manifest.json").read_text(encoding="utf-8"))
    star = manifest["capabilities"]["xingcheng"]["star_native_model_permissions"]
    assert star["database_write"] is True
    assert star["database_write_scope"] == "xingcheng-model-internal-unrestricted-excluding-permission-data"
    assert star["investment_database_write"] is False
    assert manifest["permissions"]["database_scope"] == "opaque-central-index-read-and-xingcheng-internal-read-write"
