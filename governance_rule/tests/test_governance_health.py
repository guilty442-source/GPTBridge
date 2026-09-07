from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from governance_rule.codex import GOVERNANCE_CODEX  # noqa: E402
from governance_rule.codex.chinese import GOVERNANCE_CODEX_CHINESE  # noqa: E402


def test_authoritative_codex_declares_the_governance_foundation() -> None:
    assert len(GOVERNANCE_CODEX.principles) >= 20
    assert len(GOVERNANCE_CODEX.articles) >= 40
    assert len(GOVERNANCE_CODEX.edicts) >= 30
    assert len(GOVERNANCE_CODEX.sovereigns) >= 4


def test_codex_ids_use_standardized_prefixes() -> None:
    for principle in GOVERNANCE_CODEX.principles:
        assert re.fullmatch(r"P\d+", principle.id)
    for article in GOVERNANCE_CODEX.articles:
        assert re.fullmatch(r"A\d+", article.id)
    for edict in GOVERNANCE_CODEX.edicts:
        assert re.fullmatch(r"E\d+", edict.id)
    for sovereign in GOVERNANCE_CODEX.sovereigns:
        assert sovereign.id and sovereign.area


def test_chinese_codex_mirrors_the_authoritative_codex() -> None:
    assert {
        principle.id for principle in GOVERNANCE_CODEX.principles
    } == {
        principle.id for principle in GOVERNANCE_CODEX_CHINESE.principles
    }
    assert {
        article.id for article in GOVERNANCE_CODEX.articles
    } == {
        article.id for article in GOVERNANCE_CODEX_CHINESE.articles
    }
    assert {
        edict.id for edict in GOVERNANCE_CODEX.edicts
    } == {
        edict.id for edict in GOVERNANCE_CODEX_CHINESE.edicts
    }


def test_audit_execution_module_is_present_for_self_detection() -> None:
    audit_dir = ROOT / "governance_rule" / "execution" / "audit"
    assert (audit_dir / "__init__.py").is_file()
    source = (audit_dir / "__init__.py").read_text(encoding="utf-8")
    assert "def audit_runtime_governance" in source


def test_governance_rule_module_exports_audit_entry_point() -> None:
    from governance_rule.execution import audit  # noqa: E402

    assert callable(audit.audit_runtime_governance)