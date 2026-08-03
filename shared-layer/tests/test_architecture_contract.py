from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_sql_uses_real_postgresql_security_and_no_chunk_content() -> None:
    sql = (ROOT / "sql" / "central_index.sql").read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "pg_has_role(session_user" in sql
    assert "pg_has_role(current_user, principal.role_name" not in sql
    assert "content text NOT NULL" not in sql
    assert "locator_fragment text NOT NULL" in sql
    assert "current_setting('gptbridge.actor_kind', true) = 'xingcheng'" in sql
    assert "can_write_resource(module_id, classification)" in sql


def test_xingcheng_has_select_only_grants() -> None:
    sql = (ROOT / "sql" / "central_index.sql").read_text(encoding="utf-8")
    star_lines = [line.strip() for line in sql.splitlines() if "gptbridge_xingcheng_reader" in line]
    assert star_lines
    assert not any(line.startswith("GRANT INSERT") or line.startswith("GRANT UPDATE") or line.startswith("GRANT DELETE") for line in star_lines)
    migration = (ROOT / "migrations" / "001_registry_locations.sql").read_text(encoding="utf-8")
    assert "physical_location" not in migration.split("CREATE OR REPLACE VIEW registry.resource_locations", 1)[1].split("FROM registry.locations", 1)[0]


def test_python_gateway_is_default_deny_and_xingcheng_read_only() -> None:
    module = load_module("gateway_contract", "src/shared_layer/access_gateway/gateway.py")
    denied = module.AccessGateway(lambda *_: False)
    own = module.Principal("tool-a", "file-sorter")
    assert denied.decide(own, "read", "file-sorter").allowed is False
    assert denied.decide(own, "read", "vaultly").reason == "CROSS_MODULE_DEFAULT_DENY"
    star = module.Principal("xingcheng", "local-ai", is_xingcheng=True)
    allowed = module.AccessGateway(lambda *_: True)
    assert allowed.decide(star, "read", "vaultly").allowed is True
    assert allowed.decide(star, "update", "vaultly").reason == "XINGCHENG_CROSS_MODULE_READ_ONLY"
    assert allowed.decide(star, "update", "local-ai").allowed is True
    assert allowed.decide(star, "manage", "local-ai").allowed is True
    assert allowed.decide(
        star, "update", "local-ai", resource_class="permission-file"
    ).reason == "PROTECTED_AUTHORITY_READ_ONLY"
    assert allowed.decide(
        star, "read", "local-ai", resource_class="permission-file"
    ).allowed is True
    assert allowed.decide(star, "execute", "system-rescue").allowed is False


def test_qdrant_runtime_is_loopback_and_fixed_location(tmp_path: Path) -> None:
    module = load_module("local_rag_contract", "src/shared_layer/rag_bridge/local_runtime.py")
    runtime = module.runtime_for(tmp_path)
    assert runtime.qdrant_root == (tmp_path / "local-model" / "runtime" / "qdrant").resolve()
    try:
        module.LocalRagRuntime(tmp_path / "qdrant", "https://example.com")
    except ValueError as exc:
        assert str(exc) == "QDRANT_MUST_BE_LOCAL"
    else:
        raise AssertionError("remote Qdrant endpoint accepted")


def test_qdrant_hits_require_postgresql_authorization_and_no_content_payload() -> None:
    module = load_module("rag_bridge_contract", "src/shared_layer/rag_bridge/bridge.py")
    hits = (
        module.QdrantHit("R1", "C1", "vaultly", 0.9),
        module.QdrantHit("R2", "C2", "file-sorter", 0.8),
    )
    bridge = module.RagAuthorizationBridge(lambda _actor, resource: resource == "R2")
    assert tuple(hit.resource_id for hit in bridge.filter_authorized("xingcheng", hits)) == ("R2",)

    class Store:
        def replace_document(self, *_args, **_kwargs):
            raise AssertionError("invalid payload reached Qdrant")

    coordinator = module.RagIndexCoordinator(Store(), lambda *_: None, lambda *_: None)
    try:
        coordinator.upsert("R1", "vaultly", [{"id": "P1", "payload": {"chunk_id": "C1", "content": "secret"}}])
    except ValueError as exc:
        assert str(exc) == "RAG_PAYLOAD_MUST_NOT_CONTAIN_PHYSICAL_CONTENT_OR_PATH"
    else:
        raise AssertionError("physical content was accepted into Qdrant payload")


def test_no_installer_or_docker_dependency_in_python_core() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src" / "shared_layer").rglob("*.py"))
    forbidden = ("pip install", "winget install", "choco install", "docker compose", "docker run")
    assert not any(command in sources.casefold() for command in forbidden)


def test_xingcheng_self_database_write_is_executor_only() -> None:
    manifest = json.loads((ROOT.parent / "local-model" / "manifest.json").read_text(encoding="utf-8"))
    star = manifest["capabilities"]["local-ai"]["star_native_model_permissions"]
    assert star["database_write"] is True
    assert star["database_write_scope"] == "local-ai-model-internal-unrestricted-excluding-permission-data"
    assert star["investment_database_write"] is False
    assert manifest["permissions"]["database_scope"] == "opaque-central-index-read-and-local-ai-internal-read-write"
