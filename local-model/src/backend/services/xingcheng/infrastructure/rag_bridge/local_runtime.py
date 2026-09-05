from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..vector_store import LocalVectorStore


@dataclass(frozen=True)
class LocalRagRuntime:
    index_root: Path

    def __post_init__(self) -> None:
        root = self.index_root.resolve()
        if root.parts[-3:] != ("shared-layer", "runtime", "semantic-index"):
            raise ValueError("LOCAL_SEMANTIC_INDEX_LOCATION_INVALID")
        object.__setattr__(self, "index_root", root)

    @property
    def vector_store(self) -> LocalVectorStore:
        return LocalVectorStore(self.index_root / "vectors.sqlite3")

    def with_root(self, index_root: Path | str) -> "LocalRagRuntime":
        return replace(self, index_root=Path(index_root).resolve())


def runtime_for(project_root: Path | str) -> LocalRagRuntime:
    root = Path(project_root).resolve()
    return LocalRagRuntime(root / "shared-layer" / "runtime" / "semantic-index")


__all__ = ["LocalRagRuntime", "runtime_for"]