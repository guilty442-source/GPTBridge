"""Compatibility entry for the project-cleaner v2 engine.

The implementation keeps plan_cleanup, get_status, and ProjectCleanupService in
the standalone project-cleaner backend while allowing direct file-based imports
used by the repository tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


try:
    from backend.cleanup_engine import ProjectCleanupService
except ModuleNotFoundError:
    engine_path = Path(__file__).with_name("cleanup_engine.py")
    spec = importlib.util.spec_from_file_location("project_cleaner_cleanup_engine", engine_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load project cleaner engine: {engine_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    ProjectCleanupService = module.ProjectCleanupService


__all__ = ["ProjectCleanupService"]
