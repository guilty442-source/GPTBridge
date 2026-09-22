"""§10.67 weight-generation retirement — keep active + previous only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVICE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "backend"
    / "services"
)
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from xingcheng.infrastructure.native_transformer.lifecycle import (  # noqa: E402
    ModelLifecycle,
)
from xingcheng.infrastructure.native_transformer.retention import (  # noqa: E402
    RetentionPolicy,
    apply_retention,
)

SETTINGS_DIR = Path("runtime") / "settings"
LIFECYCLE_DIR = (
    Path("xingcheng") / "runtime" / "models" / "lifecycle" / "m1"
)


def _mk_weights(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"w" * 64)
    return path


def _seed(root: Path, count: int = 4) -> tuple[ModelLifecycle, list[Path]]:
    jobs = Path("xingcheng") / "runtime" / "models" / "jobs"
    files = [
        _mk_weights(root, str(jobs / f"job-{i}" / "final.pt"))
        for i in range(count)
    ]
    lifecycle = ModelLifecycle.load_or_create(root / LIFECYCLE_DIR, "m1")
    lifecycle.transition("INITIALIZED")
    for i, f in enumerate(files):
        lifecycle.register_artifact("weights", f, activate=(i == count - 1))
    lifecycle.save(root / LIFECYCLE_DIR)
    return lifecycle, files


def test_retirement_keeps_active_and_previous(tmp_path: Path) -> None:
    lifecycle, files = _seed(tmp_path)
    policy = RetentionPolicy(keep_weight_versions=2)
    result = apply_retention(tmp_path, policy=policy)
    assert result["ok"]
    assert result["weight_retirement"]["models"]["m1"] == [1, 2]

    reloaded = ModelLifecycle.load(tmp_path / LIFECYCLE_DIR)
    remaining = [
        int(e["version"])
        for e in reloaded.artifacts["weights"]["versions"]
    ]
    assert remaining == [3, 4]
    assert reloaded.active_weights_version == 4
    retired = reloaded.artifacts["weights"]["retired"]
    assert [int(e["version"]) for e in retired] == [1, 2]
    assert all("sha256" in e for e in retired)  # evidence preserved

    assert not files[0].exists() and not files[1].exists()
    assert files[2].exists() and files[3].exists()


def test_dry_run_does_not_mutate_lifecycle(tmp_path: Path) -> None:
    lifecycle, files = _seed(tmp_path)
    before = (
        tmp_path / LIFECYCLE_DIR / "lifecycle.json"
    ).read_text(encoding="utf-8")
    result = apply_retention(tmp_path, dry_run=True)
    assert result["ok"]
    assert result["weight_retirement"].get("skipped") == "dry-run"
    after = (
        tmp_path / LIFECYCLE_DIR / "lifecycle.json"
    ).read_text(encoding="utf-8")
    assert before == after
    assert all(f.exists() for f in files)


def test_pinned_checkpoint_never_retired(tmp_path: Path) -> None:
    lifecycle, files = _seed(tmp_path)
    settings = tmp_path / SETTINGS_DIR
    settings.mkdir(parents=True, exist_ok=True)
    pinned_rel = files[0].relative_to(tmp_path)
    (settings / "native-engine.json").write_text(
        json.dumps({"checkpoint": str(pinned_rel)}), encoding="utf-8"
    )
    policy = RetentionPolicy(keep_weight_versions=2)
    apply_retention(tmp_path, policy=policy)

    reloaded = ModelLifecycle.load(tmp_path / LIFECYCLE_DIR)
    remaining = {
        int(e["version"]) for e in reloaded.artifacts["weights"]["versions"]
    }
    assert remaining == {1, 3, 4}
    assert files[0].exists()
    assert not files[1].exists()


def test_retired_payload_deleted_but_evidence_referenced_kept(
    tmp_path: Path,
) -> None:
    """退役版本的檔案若仍被 maturity 證據引用 → fail-closed 保留。"""
    lifecycle, files = _seed(tmp_path)
    logs = tmp_path / "xingcheng" / "runtime" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "maturity-x.json").write_text(
        json.dumps({"checkpoint": str(files[0].relative_to(tmp_path))}),
        encoding="utf-8",
    )
    apply_retention(tmp_path, policy=RetentionPolicy(keep_weight_versions=2))
    assert files[0].exists()   # evidence-referenced → kept
    assert not files[1].exists()
