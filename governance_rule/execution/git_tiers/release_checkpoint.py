"""Git-layer release checkpoints (task §34).

A completed merge into ``main`` is *not* a release.  This module records a
checkpoint — durable version evidence, not activation authority:

    main SHA + audit sequence + governance audit result
    + origin SHA (when push is enabled) + timestamp.

Checkpoints are JSON records under
``<git-common>/gptbridge-automation/releases/`` and may additionally create
a governed annotated tag ``release/gptbridge/<version>`` (Tier-2, audited).
Tags are NEVER pushed automatically.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .git_repository import GitRepository
from .repo_sync import sync_state

RELEASE_TAG_PREFIX = "release/gptbridge/"


def _releases_dir(root: str | Path) -> Path:
    repo = GitRepository(root)
    result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (result.stdout or "").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = repo.path / common
    directory = common.resolve() / "gptbridge-automation" / "releases"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def record_checkpoint(
    root: str | Path,
    *,
    audit_result: str = "",
    audit_sequence: int | None = None,
    queue_id: str = "",
    create_tag: bool = False,
    version: str = "",
    actor: str = "governance/release",
) -> dict[str, Any]:
    """Record a release checkpoint; optionally tag it (never pushed)."""
    repo = GitRepository(root)
    state = sync_state(root)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    checkpoint = {
        "timestamp": timestamp,
        "main_sha": state["local_main_sha"],
        "origin_sha": state["origin_main_sha"],
        "governance_audit": audit_result,
        "audit_sequence": audit_sequence,
        "queue_id": queue_id,
        "sync_state": state["state"],
    }
    directory = _releases_dir(root)
    name = f"{timestamp.replace(':', '')}-{(state['local_main_sha'] or 'none')[:12]}"
    path = directory / f"{name}.json"
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    checkpoint["path"] = str(path)

    if create_tag and version:
        from .capability_gate import execute_system_safe

        tag = f"{RELEASE_TAG_PREFIX}{version}"
        gate = execute_system_safe(
            [
                "tag", "-a", tag,
                "-m", f"governed release checkpoint {version} @ {timestamp}",
                state["local_main_sha"] or "main",
            ],
            actor=actor, repo_path=repo.path,
        )
        tagged = gate.execution_result
        checkpoint["tag"] = tag if (
            gate.allowed is not False
            and tagged is not None
            and tagged.returncode == 0
        ) else None
        if tagged is None or tagged.returncode != 0:
            checkpoint["tag_error"] = (
                f"{gate.code}:{gate.detail}"[:200]
                if tagged is None else (tagged.stderr or "")[:200]
            )
    return checkpoint


def list_checkpoints(root: str | Path) -> list[dict[str, Any]]:
    directory = _releases_dir(root)
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload["path"] = str(path)
            out.append(payload)
    return out


__all__ = ["RELEASE_TAG_PREFIX", "list_checkpoints", "record_checkpoint"]
