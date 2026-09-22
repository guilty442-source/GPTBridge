"""§10.67 E caps enforcement tests."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from governance_rule.execution.tool_runtime.tool_caps_enforcement import (  # noqa: E501
    run_caps_enforcement,
)


def _write(path: Path, size: int = 10, age_hours: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))
    return path


def test_missing_root_fails_closed(tmp_path):
    result = run_caps_enforcement("t", tmp_path / "nope")
    assert result["ok"] is False
    assert result["error"] == "MODULE_ROOT_MISSING"


def test_backups_keep_only_latest(tmp_path):
    root = tmp_path / "tool"
    _write(root / "backups" / "old.zip", age_hours=200)
    _write(root / "backups" / "mid.zip", age_hours=100)
    _write(root / "backups" / "new.zip", age_hours=1)
    result = run_caps_enforcement("t", root)
    assert result["ok"] is True
    assert (root / "backups" / "new.zip").exists()
    assert not (root / "backups" / "old.zip").exists()
    assert not (root / "backups" / "mid.zip").exists()
    reasons = {r["reason"] for r in result["removed"]}
    assert reasons == {"backup-keep-latest"}


def test_age_rule_removes_old_logs_keeps_fresh(tmp_path):
    root = tmp_path / "tool"
    _write(root / "logs" / "old.log", age_hours=80)
    _write(root / "logs" / "fresh.log", age_hours=10)
    result = run_caps_enforcement("t", root)
    assert result["ok"] is True
    assert not (root / "logs" / "old.log").exists()
    assert (root / "logs" / "fresh.log").exists()


def test_staging_tmp_removed_after_one_hour(tmp_path):
    root = tmp_path / "tool"
    _write(root / "work" / "x.staging-tmp", age_hours=2)
    _write(root / "work" / "y.staging-tmp", age_hours=0.5)
    result = run_caps_enforcement("t", root)
    assert not (root / "work" / "x.staging-tmp").exists()
    assert (root / "work" / "y.staging-tmp").exists()


def test_protected_dirs_never_touched(tmp_path):
    root = tmp_path / "tool"
    _write(root / "runtime" / "state" / "keep.log", age_hours=500)
    _write(root / "data" / "business" / "keep.log", age_hours=500)
    _write(root / ".venv" / "lib" / "keep.log", age_hours=500)
    result = run_caps_enforcement("t", root)
    assert result["ok"] is True
    assert (root / "runtime" / "state" / "keep.log").exists()
    assert (root / "data" / "business" / "keep.log").exists()
    assert (root / ".venv" / "lib" / "keep.log").exists()


def test_cap_prunes_oldest_eligible_first(tmp_path):
    root = tmp_path / "tool"
    # cap 100 bytes, headroom 0.9 -> target 90
    _write(root / "logs" / "a.log", size=40, age_hours=1)
    _write(root / "logs" / "b.log", size=40, age_hours=0.5)
    _write(root / "cache" / "c.log", size=40, age_hours=0.2)
    result = run_caps_enforcement(
        "t", root, cap_bytes=100, max_age_hours=1000
    )
    assert result["ok"] is True
    assert result["module_bytes_after"] <= 90
    # oldest removed first
    assert not (root / "logs" / "a.log").exists()


def test_lifecycle_referenced_paths_kept(tmp_path):
    root = tmp_path / "tool"
    target = _write(root / "snapshots" / "gen.pt", size=50, age_hours=500)
    (root / "runtime" / "state").mkdir(parents=True, exist_ok=True)
    (root / "runtime" / "state" / "lifecycle.json").write_text(
        '{"artifacts": [{"path": "../../snapshots/gen.pt"}]}',
        encoding="utf-8",
    )
    result = run_caps_enforcement("t", root)
    assert target.exists()
    assert all(r["path"] != "snapshots/gen.pt" for r in result["removed"])


def test_age_rule_ignores_noneligible_dirs(tmp_path):
    """Old files outside eligible categories are business data — kept."""
    root = tmp_path / "tool"
    _write(root / "config" / "settings.dat", age_hours=500)
    _write(root / "src" / "module.dat", age_hours=500)
    result = run_caps_enforcement("t", root)
    assert (root / "config" / "settings.dat").exists()
    assert (root / "src" / "module.dat").exists()
