from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class LocalRagRuntime:
    qdrant_root: Path
    endpoint: str = "http://127.0.0.1:6333"

    def __post_init__(self) -> None:
        root = self.qdrant_root.resolve()
        parsed = urlparse(self.endpoint)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("QDRANT_MUST_BE_LOCAL")
        if root.parts[-3:] != ("local-model", "runtime", "qdrant"):
            raise ValueError("QDRANT_RUNTIME_LOCATION_INVALID")
        object.__setattr__(self, "qdrant_root", root)


def runtime_for(project_root: Path | str) -> LocalRagRuntime:
    root = Path(project_root).resolve()
    return LocalRagRuntime(root / "local-model" / "runtime" / "qdrant")


__all__ = ["LocalRagRuntime", "runtime_for"]
