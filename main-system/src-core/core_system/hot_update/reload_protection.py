"""Hot-update reload protection — split from HotUpdateService for A185 compliance."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ReloadProtection:
    """Persist reload protection state to pin source revisions."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def persist(self, module_names: list[str]) -> None:
        """Pin successfully reloaded source revisions against auto-repair."""
        protected: dict[str, str] = {}
        for module_name in module_names:
            module = sys.modules.get(module_name)
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            try:
                path = Path(file_path).resolve()
                relative = path.relative_to(self.project_root).as_posix()
                protected[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, ValueError):
                continue
        if not protected:
            return
        target = (
            self.project_root / "main-system" / "runtime" / "state"
            / "hot-reload-protection.json"
        )
        payload = {
            "version": 1,
            "protected_sources": protected,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError:
            pass