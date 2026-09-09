from __future__ import annotations

import json
import shutil
from pathlib import Path

from packager_base import MAX_COMPLETED_RECOVERY_GENERATIONS
from packager_inventory import _is_link_or_reparse


def prune_completed_recovery_roots(
    tool_dir: Path,
    *,
    keep: int = MAX_COMPLETED_RECOVERY_GENERATIONS,
) -> list[str]:
    """Bound successful rollback generations without touching failed recovery."""

    build_root = (tool_dir / "build").resolve()
    if keep < 0 or not build_root.is_dir():
        return []
    candidates: list[Path] = []
    for candidate in build_root.glob("package-*"):
        try:
            if (
                not candidate.is_dir()
                or _is_link_or_reparse(candidate)
                or candidate.resolve(strict=True).parent != build_root
                or not (candidate / "promotion-complete.json").is_file()
                or not (candidate / "promotion-recovery-manifest.json").is_file()
            ):
                continue
            aborted_path = candidate / "promotion-aborted.json"
            if aborted_path.is_file():
                aborted = json.loads(aborted_path.read_text(encoding="utf-8"))
                if aborted.get("status") == "rollback-incomplete":
                    continue
            candidates.append(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    removed: list[str] = []
    for candidate in candidates[keep:]:
        try:
            shutil.rmtree(candidate)
        except OSError:
            # Promotion has already completed and the live package was
            # verified. A transient antivirus/Explorer lock on an older,
            # completed recovery generation must not turn that success into
            # a package failure. The retained recovery can be pruned later.
            continue
        removed.append(str(candidate))
    return removed
