from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable


class ProjectAgent:
    """Lightweight project helper used by autonomous coder flows."""

    def __init__(self, max_backup_count: int = 3, project_root: Path | None = None) -> None:
        self.max_backup_count = max(1, int(max_backup_count))
        self.project_root = (project_root or Path(__file__).resolve().parent.parent).resolve()
        self.backup_root = self.project_root / "backups" / "main-system"
        self.backup_root.mkdir(parents=True, exist_ok=True)

    def update_desktop_shortcut(self) -> None:
        # Keep as a no-op in source mode. Desktop shortcut generation is optional.
        return

    def apply_project_dump(self, dump_text: str) -> tuple[list[str], Path]:
        """
        Persist AI output for audit trail and return a stable result contract.
        The actual patching pipeline can be layered on later.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_dir = self.backup_root / "ai-dumps"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        dump_file = snapshot_dir / f"dump_{ts}.md"
        dump_file.write_text(dump_text or "", encoding="utf-8")
        self._prune_old(snapshot_dir.glob("dump_*.md"))
        return [], dump_file

    def _prune_old(self, files: Iterable[Path]) -> None:
        sorted_files = sorted(files, key=lambda path: path.name, reverse=True)
        for old in sorted_files[self.max_backup_count :]:
            try:
                old.unlink()
            except OSError:
                continue

