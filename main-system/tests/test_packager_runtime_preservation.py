"""Update must not overwrite runtime state (blueprint: 更新不覆寫 runtime 強制).

`promote_staged_distribution` moves the whole live distribution aside and
installs the staged package.  Files the live dist carries outside
``resources/app/`` that are not part of the staged payload are runtime state
(settings/data/weights/sessions/unfinished tasks) — they must be restored
into the promoted tree, not silently relocated into the recovery root.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(ROOT),
    str(ROOT / "main-system" / "src-core"),
    str(ROOT / "main-system"),
    str(ROOT / "shared-layer" / "src"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _p

sys.path.insert(0, str(ROOT / "main-system" / "src-core" / "tasks"))

from packager_distribution import (  # noqa: E402
    _synchronize_distribution_files_in_place,
    promote_staged_distribution,
)


def _staged_dist(root: Path) -> Path:
    staged = root / "package" / "staged-dist"
    app = staged / "resources" / "app"
    app.mkdir(parents=True)
    (app / "main.py").write_text("print('v2')\n", encoding="utf-8")
    (app / ".gptbridge-package.json").write_text(
        json.dumps({"payload_digest": "x", "payload_files": {"main.py": {}}}),
        encoding="utf-8",
    )
    return staged


def _live_dist(root: Path) -> Path:
    dist = root / "dist"
    app = dist / "resources" / "app"
    app.mkdir(parents=True)
    (app / "main.py").write_text("print('v1')\n", encoding="utf-8")
    (app / ".gptbridge-package.json").write_text(
        json.dumps({"payload_digest": "y", "payload_files": {"main.py": {}}}),
        encoding="utf-8",
    )
    # Runtime state created by the running app — outside the payload domain.
    (dist / "runtime" / "state").mkdir(parents=True)
    (dist / "runtime" / "state" / "session.json").write_text(
        json.dumps({"session": "in-flight"}), encoding="utf-8"
    )
    (dist / "data" / "weights").mkdir(parents=True)
    (dist / "data" / "weights" / "final.pt").write_bytes(b"weights-v1")
    return dist


def test_promotion_restores_live_runtime_paths(tmp_path: Path) -> None:
    staged = _staged_dist(tmp_path)
    dist = _live_dist(tmp_path)

    recovery_root = promote_staged_distribution(staged, dist)

    assert recovery_root == tmp_path / "package"
    # New payload installed.
    assert (dist / "resources" / "app" / "main.py").read_text() == (
        "print('v2')\n"
    )
    # Runtime state preserved inside the promoted tree.
    assert json.loads(
        (dist / "runtime" / "state" / "session.json").read_text(
            encoding="utf-8"
        )
    ) == {"session": "in-flight"}
    assert (dist / "data" / "weights" / "final.pt").read_bytes() == (
        b"weights-v1"
    )
    manifest = json.loads(
        (tmp_path / "package" / "promotion-recovery-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert sorted(manifest["runtime_paths_restored"]) == [
        "data/weights/final.pt",
        "runtime/state/session.json",
    ]


def test_in_place_sync_preserves_runtime_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.py").write_text("v2", encoding="utf-8")
    live = tmp_path / "live"
    live.mkdir()
    (live / "payload.py").write_text("v1", encoding="utf-8")
    (live / "runtime").mkdir()
    (live / "runtime" / "session.json").write_text("{}", encoding="utf-8")
    (live / "stale.py").write_text("stale", encoding="utf-8")

    _synchronize_distribution_files_in_place(
        source,
        live,
        retired_root=tmp_path / "retired",
        preserve_paths={"runtime/session.json"},
    )

    # Payload updated, runtime preserved in place, stale file retired.
    assert (live / "payload.py").read_text() == "v2"
    assert (live / "runtime" / "session.json").is_file()
    assert not (live / "stale.py").exists()
    assert (
        tmp_path / "retired" / "removed-from-live" / "stale.py"
    ).is_file()
