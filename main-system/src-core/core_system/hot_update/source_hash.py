"""Hot-update source hash tracking — split from HotUpdateService for A185 compliance."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core_system.circuit_breaker import CircuitBreaker
from startup_core.feature_flags import get_flags


def _module_source_hash(file_path: str) -> str | None:
    """Return SHA-256 of a Python module's source file, or None on error."""
    try:
        content = Path(file_path).read_bytes()
        return hashlib.sha256(content).hexdigest()
    except OSError:
        return None


class SourceHashTracker:
    """Track and persist source file hashes for change detection."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self._hash_lock = threading.Lock()
        self._source_hashes: dict[str, str] = {}
        self._file_hash_cache: dict[str, tuple[float, str]] = {}  # path -> (mtime, hash)
        self._load_source_hashes()

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

    def get_stored_hashes(self) -> dict[str, str]:
        """Get a copy of stored hashes."""
        with self._hash_lock:
            return dict(self._source_hashes)

    def filter_changed(
        self, candidates: list[tuple[str, Any]]
    ) -> list[tuple[str, Any]]:
        """Return only candidates whose source hash changed since last reload."""
        changed: list[tuple[str, Any]] = []
        stored = self.get_stored_hashes()
        for module_name, module in candidates:
            file_path = getattr(module, "__file__", None)
            if not file_path:
                changed.append((module_name, module))
                continue
            current_hash = _module_source_hash(file_path)
            if current_hash is None:
                changed.append((module_name, module))
                continue
            if stored.get(module_name) != current_hash:
                changed.append((module_name, module))
            else:
                _logger.debug(
                    "hot_reload_skip_unchanged module=%s", module_name,
                )
        return changed

    def update_hashes(self, module_names: list[str]) -> None:
        """Update stored hashes for successfully reloaded modules."""
        with self._hash_lock:
            for module_name in module_names:
                module = sys.modules.get(module_name)
                if module is None:
                    continue
                file_path = getattr(module, "__file__", None)
                if not file_path:
                    continue
                h = _module_source_hash(file_path)
                if h is not None:
                    self._source_hashes[module_name] = h
        self._save_source_hashes()