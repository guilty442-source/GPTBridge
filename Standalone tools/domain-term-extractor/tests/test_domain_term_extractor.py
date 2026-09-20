from __future__ import annotations

import json
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_TOOL_ROOT = _ROOT / "Standalone tools" / "domain-term-extractor"


def test_manifest_declares_domain_term_extractor() -> None:
    manifest = json.loads((_TOOL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "domain-term-extractor"
    assert manifest["name_key"] == "tool.name"
