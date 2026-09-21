"""P0 G33: protected governance material never enters training corpus."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_INFRA = _ROOT / "src" / "backend" / "services" / "xingcheng" / "infrastructure"
if str(_INFRA) not in sys.path:
    sys.path.insert(0, str(_INFRA))

from native_transformer.training.corpus import (  # noqa: E402
    DEFAULT_SOURCES,
    build_corpus,
    iter_documents,
)


def test_default_sources_exclude_governance_codex() -> None:
    assert "governance_rule/codex" not in DEFAULT_SOURCES


def test_explicit_governance_codex_source_is_rejected_and_audited(tmp_path: Path) -> None:
    protected = tmp_path / "governance_rule" / "codex"
    docs = tmp_path / "docs"
    protected.mkdir(parents=True)
    docs.mkdir()
    secret = "confidential governance mirror " * 8
    (protected / "governance_codex.zh-TW.part-1.txt").write_text(secret, encoding="utf-8")
    (docs / "allowed.md").write_text("first-party training document " * 8, encoding="utf-8")

    documents = list(iter_documents(tmp_path, sources=("governance_rule/codex", "docs")))
    assert [document.source for document in documents] == ["docs/allowed.md"]
    assert all(secret not in document.text for document in documents)

    manifest = build_corpus(
        tmp_path,
        tmp_path / "out",
        sources=("governance_rule/codex", "docs"),
    )
    assert manifest["rejected_sources"] == ["governance_rule/codex"]
    assert manifest["rejection_reasons"] == {
        "governance_rule/codex": "protected-governance-source"
    }
    records = [
        json.loads(line)
        for line in (tmp_path / "out" / "train.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert all("governance_rule/codex" not in record["source"] for record in records)
