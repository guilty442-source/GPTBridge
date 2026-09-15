"""Visual smoke test entry point (facade for split modules).

This module re-exports the public API from the split submodules so that
existing callers that import from ``visual_smoke`` continue to work, and
``python visual_smoke.py`` still runs the full smoke test.
"""

from __future__ import annotations

from smoke_constants import (
    DEFAULT_OUTPUT_DIR,
    PROJECT_ROOT,
    RENDERER_DIR,
    WORKSPACE_LABELS,
)
from smoke_fixtures import synthetic_fixture
from smoke_renderer import (
    QuietHandler,
    build_renderer,
    browser_bootstrap_script,
    renderer_server,
)
from smoke_runner import (
    accessibility_audit,
    exercise_holding_editor,
    exercise_workspaces,
    main,
    run_page,
    wait_for_fixture,
)


__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "PROJECT_ROOT",
    "QuietHandler",
    "RENDERER_DIR",
    "WORKSPACE_LABELS",
    "accessibility_audit",
    "browser_bootstrap_script",
    "build_renderer",
    "exercise_holding_editor",
    "exercise_workspaces",
    "main",
    "renderer_server",
    "run_page",
    "synthetic_fixture",
    "wait_for_fixture",
]


if __name__ == "__main__":
    raise SystemExit(main())
