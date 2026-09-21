"""shared-layer consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "global-cleaner" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

########################################################################
# source: shared-layer/tests/test_architecture_contract.py
########################################################################
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
    from governance_rule.permission_directory.execution.access_gateway import (
        gateway as module,
    )

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
    assert allowed.decide(star, "execute", "governance_rule").allowed is False


def test_local_vector_store_is_fixed_location(tmp_path: Path) -> None:
    """The degraded local cache lives at one fixed path and fails closed on drift."""

    from shared_layer.local.vector_store import LocalVectorStore, embed_vector

    store = LocalVectorStore(tmp_path)
    assert store.database_path == (tmp_path / "local-rag-vectors.sqlite3").resolve()
    assert store.status()["canonical"] is False
    assert store.status()["reconciliation_required"] is True

    vector = embed_vector("alpha beta")
    store.replace_document(
        "doc-1",
        [{"id": "p1", "vector": vector, "module_id": "vaultly"}],
        module_id="vaultly",
    )
    store.ensure_collection(len(vector))
    try:
        store.ensure_collection(len(vector) + 1)
    except RuntimeError as exc:
        assert "RAG_VECTOR_DIMENSION_MISMATCH" in str(exc)
    else:
        raise AssertionError("dimension mismatch accepted")


def test_local_hits_require_module_scope_and_stay_in_scope(tmp_path: Path) -> None:
    """Degraded local hits obey the same module-scope discipline as Qdrant."""

    from shared_layer.local.vector_store import LocalVectorStore, embed_vector
    from shared_layer.security.qdrant_scope import QdrantScopeError

    store = LocalVectorStore(tmp_path)
    store.replace_document(
        "doc-1",
        [{"id": "p1", "text": "alpha beta", "module_id": "vaultly"}],
        module_id="vaultly",
    )
    vector = embed_vector("alpha beta")

    try:
        store.query(vector, limit=5)
    except QdrantScopeError as exc:
        assert "QDRANT_MODULE_SCOPE_REQUIRED" in str(exc)
    else:
        raise AssertionError("empty module scope accepted")

    hits = store.query(vector, limit=5, module_ids=("vaultly",))
    assert hits
    assert hits[0]["module_id"] == "vaultly"
    assert store.query(vector, limit=5, module_ids=("file-sorter",)) == []


def test_no_installer_or_docker_dependency_in_python_core() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src" / "shared_layer").rglob("*.py"))
    forbidden = ("pip install", "winget install", "choco install", "docker compose", "docker run")
    assert not any(command in sources.casefold() for command in forbidden)


def test_xingcheng_self_database_write_is_executor_only() -> None:
    local_manifest = json.loads((ROOT.parent / "Standalone tools" / "local-model" / "manifest.json").read_text(encoding="utf-8"))
    xingcheng_manifest = json.loads((ROOT.parent / "Standalone tools" / "local-model" / "xingcheng" / "manifest.json").read_text(encoding="utf-8"))
    star = local_manifest["capabilities"]["xingcheng"]["star_native_model_permissions"]
    assert star["database_write"] is True
    assert star["database_write_scope"] == "xingcheng-model-internal-unrestricted-excluding-permission-data"
    assert star["investment_database_write"] is True
    assert xingcheng_manifest["permissions"]["database_scope"] == "opaque-central-index-read-and-xingcheng-internal-read-write"


def test_local_database_settings_identifier_boundaries() -> None:
    """AA14: settings regex boundary cases — accept/reject at edges."""
    import pytest

    from shared_layer.local.database import LocalDatabaseSettings

    # Valid baselines
    LocalDatabaseSettings()
    LocalDatabaseSettings(
        admin_dsn="local:" + "a" * 1,
        database="a" * 63,          # max identifier length
        owner_role="z9_",
    )
    # admin_dsn boundaries
    with pytest.raises(ValueError, match="GPTBRIDGE_LOCAL_DB_REQUIRED"):
        LocalDatabaseSettings(admin_dsn="")
    with pytest.raises(ValueError, match="GPTBRIDGE_LOCAL_DB_REQUIRED"):
        LocalDatabaseSettings(admin_dsn="local:UPPER")           # uppercase rejected
    with pytest.raises(ValueError, match="GPTBRIDGE_LOCAL_DB_REQUIRED"):
        LocalDatabaseSettings(admin_dsn="local:" + "a" * 513)    # over 512-char tail
    with pytest.raises(ValueError, match="GPTBRIDGE_LOCAL_DB_REQUIRED"):
        LocalDatabaseSettings(admin_dsn="local:-lead-dash")      # leading dash rejected
    # SQL identifier boundaries
    with pytest.raises(ValueError, match="INVALID_LOCAL_IDENTIFIER:database"):
        LocalDatabaseSettings(database="9starts_digit")
    with pytest.raises(ValueError, match="INVALID_LOCAL_IDENTIFIER:database"):
        LocalDatabaseSettings(database="a" * 64)                 # over 63
    with pytest.raises(ValueError, match="INVALID_LOCAL_IDENTIFIER:owner_role"):
        LocalDatabaseSettings(owner_role="has-dash")
    with pytest.raises(ValueError, match="INVALID_LOCAL_IDENTIFIER:runtime_role"):
        LocalDatabaseSettings(runtime_role="")


def test_local_database_health_check_and_locator(tmp_path) -> None:
    """AA5/AA15: health check reports + locator repository round-trip."""
    import uuid

    from shared_layer.local.database import LocalDatabaseHealthCheck
    from shared_layer.local.module_locator import LocalModuleLocatorRepository

    repo = LocalModuleLocatorRepository(tmp_path / "locator.sqlite3", "mod-a")
    locator = uuid.uuid4()
    repo.put(locator, "res-1", "shared-layer/x.txt")
    assert repo.resolve(locator, "res-1") == "shared-layer/x.txt"
    assert repo.resolve(locator, "res-missing") is None
    assert repo.resolve(uuid.uuid4(), "res-1") is None

    # Cross-module isolation: a different module_id sees nothing
    other = LocalModuleLocatorRepository(tmp_path / "locator.sqlite3", "mod-b")
    assert other.resolve(locator, "res-1") is None
