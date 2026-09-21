from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_TOOL_ROOT = _ROOT / "Standalone tools" / "domain-term-extractor"


def test_governed_entrypoints_exist() -> None:
    assert (_TOOL_ROOT / "src" / "main.py").is_file()
    assert (_TOOL_ROOT / "src" / "channel_runtime.py").is_file()
    assert (_TOOL_ROOT / "locales" / "zh-TW.json").is_file()
