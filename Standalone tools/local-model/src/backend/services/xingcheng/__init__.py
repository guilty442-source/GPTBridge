from __future__ import annotations

import importlib
from typing import Any

__all__ = ["LocalAiService"]

def __getattr__(name: str) -> Any:
    if name == "LocalAiService":
        return importlib.import_module(".application.service", __name__).LocalAiService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
