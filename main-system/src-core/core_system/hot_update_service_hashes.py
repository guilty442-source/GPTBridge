"""Source-hash tracking mixin for HotUpdateService (A185 split).

Persists module source hashes so reloads can diff against the last
successful reload and skip unchanged modules.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hot_update_service_helpers import module_source_hash

_logger = logging.getLogger("gptbridge.hot_update")


class HotUpdateHashMixin:
    """Source-hash persistence and change-detection helpers."""

    _hash_lock: threading.Lock
    _source_hashes: dict[str, str]
    project_root: Path

    def _load_source_hashes(self) -> None:
        """Load persisted source hashes from the state file."""
        path = (
            self.project_root / "main-system" / "runtime" / "state"
            / "hot-reload-hashes.json"
        )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            with self._hash_lock:
                self._source_hashes = data.get("hashes", {})
        except (OSError, json.JSONDecodeError):
            pass

    def _save_source_hashes(self) -> None:
        """Persist source hashes so the next reload can diff."""
        path = (
            self.project_root / "main-system" / "runtime" / "state"
            / "hot-reload-hashes.json"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._hash_lock:
                payload = {
                    "version": 1,
                    "hashes": dict(self._source_hashes),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass

    def _filter_changed(
        self, candidates: list[tuple[str, Any]]
    ) -> list[tuple[str, Any]]:
        """Return only candidates whose source hash changed since last reload."""
        changed: list[tuple[str, Any]] = []
        with self._hash_lock:
            stored = dict(self._source_hashes)
        for module_name, module in candidates:
            file_path = getattr(module, "__file__", None)
            if not file_path:
                changed.append((module_name, module))
                continue
            current_hash = module_source_hash(file_path)
            if current_hash is None:
                changed.append((module_name, module))
                continue
            if stored.get(module_name) != current_hash:
                changed.append((module_name, module))
            else:
                _logger.debug("hot_reload_skip_unchanged module=%s", module_name)
        return changed

    def _update_source_hashes(self, module_names: list[str]) -> None:
        """Update stored hashes for successfully reloaded modules."""
        with self._hash_lock:
            for module_name in module_names:
                module = sys.modules.get(module_name)
                if module is None:
                    continue
                file_path = getattr(module, "__file__", None)
                if not file_path:
                    continue
                h = module_source_hash(file_path)
                if h is not None:
                    self._source_hashes[module_name] = h
        self._save_source_hashes()


__all__ = ["HotUpdateHashMixin"]
