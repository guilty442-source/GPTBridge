"""governance_rule consolidated test suite (A57/E43)

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
    str(_ROOT / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "global-cleaner" / "src"),
    str(_ROOT / "ai-assistant" / "src"),
    str(_ROOT / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

import json

from governance_rule.codex import GOVERNANCE_CODEX, GOVERNANCE_CODEX_CHINESE
from governance_rule.execution.git_tiers import classify
from governance_rule.governance_policy import GOVERNANCE_POLICY


def test_governance_codex_references_are_structurally_aligned() -> None:
    assert [item.id for item in GOVERNANCE_CODEX.principles] == [
        item.id for item in GOVERNANCE_CODEX_CHINESE.principles
    ]
    assert [item.id for item in GOVERNANCE_CODEX.articles] == [
        item.id for item in GOVERNANCE_CODEX_CHINESE.articles
    ]
    assert [item.id for item in GOVERNANCE_CODEX.edicts] == [
        item.id for item in GOVERNANCE_CODEX_CHINESE.edicts
    ]
    assert [item.id for item in GOVERNANCE_CODEX.sovereigns] == [
        item.id for item in GOVERNANCE_CODEX_CHINESE.sovereigns
    ]


def test_governance_manifest_declares_collectable_self_health_target() -> None:
    tool_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((tool_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "governance_rule"
    assert manifest["test_targets"] == ["tests/test_governance_health.py"]
    assert (tool_root / manifest["test_targets"][0]).is_file()


def test_codex_is_the_enforcement_policy_source() -> None:
    assert GOVERNANCE_CODEX.codex_version == 3
    assert GOVERNANCE_POLICY.authority == "governance-codex-v3-derived-enforcement-policy"
    assert GOVERNANCE_POLICY.top_level_rule == "governance_codex"
    assert GOVERNANCE_POLICY.governance_rule_sources == (
        "governance_rule/codex/__init__.py",
    )


def test_data_roles_and_unknown_git_operations_fail_closed() -> None:
    responsibilities = GOVERNANCE_POLICY.system_responsibilities
    assert responsibilities.sql == "structured-mutable-official-data-postgresql"
    assert "owner-private" in responsibilities.sqlite
    assert responsibilities.qdrant_rag == "qdrant-semantic-knowledge-index"
    assert "never-canonical" in responsibilities.local_vector_fallback
    assert classify("unknown-governance-operation") == 3
