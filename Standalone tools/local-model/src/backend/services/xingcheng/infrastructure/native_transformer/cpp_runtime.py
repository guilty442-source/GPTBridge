"""Python facade for the formal C++ inference extension.

Python remains the development/training layer. This module only locates and
wraps the compiled C++ runtime; it does not move model execution back into
Python or bypass the public C ABI used by the C++ engine.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any


def tool_root() -> Path:
    return Path(__file__).resolve().parents[6]


def _extension_dir() -> Path:
    return tool_root() / "dist-native"


def load_extension() -> Any:
    """Import ``_xingcheng_inference`` from dist-native or site-packages."""
    dist = str(_extension_dir())
    if dist not in sys.path:
        sys.path.insert(0, dist)
    return importlib.import_module("_xingcheng_inference")


def available() -> bool:
    try:
        load_extension()
    except ImportError:
        return False
    return True


def sampling_config(**kwargs: Any) -> Any:
    module = load_extension()
    config = module.SamplingConfig()
    for key, value in kwargs.items():
        if not hasattr(config, key):
            raise ValueError(f"SAMPLING_OPTION_UNSUPPORTED:{key}")
        setattr(config, key, value)
    return config


def load_engine(bundle_dir: str | Path, *, kv_memory_limit: int = 0) -> Any:
    module = load_extension()
    engine = module.NativeInferenceEngine()
    if kv_memory_limit:
        engine.set_kv_memory_limit(int(kv_memory_limit))
    engine.load(str(bundle_dir))
    return engine


__all__ = ["available", "load_engine", "load_extension", "sampling_config", "tool_root"]
