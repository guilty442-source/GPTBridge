from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LocalRagRuntime:
    """Local RAG runtime handle bound to a fixed semantic-index location."""

    index_root: Path

    def __post_init__(self) -> None:
        self.index_root = Path(self.index_root).resolve()
        expected_suffix = ("shared-layer", "runtime", "semantic-index")
        if self.index_root.parts[-3:] != expected_suffix:
            raise ValueError("LOCAL_SEMANTIC_INDEX_LOCATION_INVALID")


def runtime_for(tool_root: Path | str) -> LocalRagRuntime:
    """Return the canonical local RAG runtime for a tool root."""
    tool_root = Path(tool_root).resolve()
    return LocalRagRuntime(tool_root / "shared-layer" / "runtime" / "semantic-index")


__all__ = ["LocalRagRuntime", "runtime_for"]
