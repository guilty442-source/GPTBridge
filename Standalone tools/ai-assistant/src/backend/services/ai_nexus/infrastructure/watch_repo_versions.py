from __future__ import annotations

import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .watch_repo_helpers import (
    _atomic_write_bytes,
    _validated_storage_path,
    decode_json_document,
    encode_json_document,
    utc_now,
)


class WatchRepoVersionsMixin:
    """State version snapshots, mobile sync settings, and clear-state operations."""

    def create_state_version(
        self,
        state: dict[str, Any] | None = None,
        *,
        reason: str,
    ) -> dict[str, Any] | None:
        with self._state_lock:
            return self._create_state_version_locked(
                state,
                reason=reason,
            )

    def _create_state_version_locked(
        self,
        state: dict[str, Any] | None = None,
        *,
        reason: str,
        allow_empty: bool = False,
    ) -> dict[str, Any] | None:
        source = dict(state) if isinstance(state, dict) else self.load_state()
        portfolio = source.get("portfolio") if isinstance(source.get("portfolio"), dict) else {}
        holdings = source.get("holdings") if isinstance(source.get("holdings"), list) else []
        if not allow_empty and not holdings and not portfolio:
            return None
        created_at = utc_now()
        version_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]
        document = {
            "version_id": version_id,
            "created_at": created_at,
            "reason": str(reason or "manual"),
            "holding_count": len(holdings),
            "file_name": str(portfolio.get("file_name") or ""),
            "manual_revision": int(portfolio.get("manual_revision") or 0),
            "state": source,
        }
        path = self.history_root / f"{version_id}.json"
        _atomic_write_bytes(path, encode_json_document(document).encode("utf-8"))
        return {key: value for key, value in document.items() if key != "state"}

    def list_state_versions(self, limit: int = 30) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        paths = sorted(
            self.history_root.glob("*.json"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        for path in paths[: max(1, min(100, int(limit)))]:
            try:
                _validated_storage_path(
                    path,
                    label="AI investment state version",
                    boundary=self.history_root,
                    require_exists=True,
                    expected_kind="file",
                )
                payload = decode_json_document(path.read_text(encoding="utf-8"))
            except (OSError, RuntimeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            output.append(
                {
                    "version_id": str(payload.get("version_id") or path.stem),
                    "created_at": str(payload.get("created_at") or ""),
                    "reason": str(payload.get("reason") or ""),
                    "holding_count": int(payload.get("holding_count") or 0),
                    "file_name": str(payload.get("file_name") or ""),
                    "manual_revision": int(payload.get("manual_revision") or 0),
                }
            )
        return output

    def restore_state_version(self, version_id: str) -> dict[str, Any]:
        with self._state_lock:
            return self._restore_state_version_locked(version_id)

    def _restore_state_version_locked(self, version_id: str) -> dict[str, Any]:
        normalized = str(version_id or "").strip()
        if not normalized or not re.fullmatch(r"[A-Za-z0-9_\-]+", normalized):
            raise ValueError("invalid state version id")
        path = self.history_root / f"{normalized}.json"
        if not path.exists():
            raise ValueError("state version not found")
        _validated_storage_path(
            path,
            label="AI investment state version",
            boundary=self.history_root,
            require_exists=True,
            expected_kind="file",
        )
        payload = decode_json_document(path.read_text(encoding="utf-8"))
        restored = payload.get("state") if isinstance(payload, dict) else None
        if not isinstance(restored, dict):
            raise ValueError("state version is invalid")
        current = self.load_state()
        self.create_state_version(current, reason="before_restore")
        restored["restored_from_version"] = normalized
        restored["restored_at"] = utc_now()
        return self.save_state(restored)

    def save_mobile_sync_remote_url(self, remote_base_url: str) -> dict[str, Any]:
        normalized = str(remote_base_url or "").strip()

        def update_remote_url(state: dict[str, Any]) -> None:
            settings = state.setdefault("mobile_sync_settings", {})
            if not isinstance(settings, dict):
                settings = {}
                state["mobile_sync_settings"] = settings
            settings["remote_base_url"] = normalized
            settings["updated_at"] = utc_now()

        return self.update_state(update_remote_url)

    def mobile_sync_remote_url(self) -> str:
        state = self.load_state()
        settings = state.get("mobile_sync_settings")
        if not isinstance(settings, dict):
            return ""
        return str(settings.get("remote_base_url") or "").strip()

    def clear_state(self, *, permanent: bool = False) -> dict[str, Any]:
        with self._state_lock:
            return self._clear_state_locked(permanent=permanent)

    def _clear_state_locked(self, *, permanent: bool = False) -> dict[str, Any]:
        remote_url = "" if permanent else self.mobile_sync_remote_url()
        current = self.load_state()
        if permanent:
            for current_root, directory_names, file_names in os.walk(
                self.state_root,
                topdown=False,
                followlinks=False,
            ):
                current_path = Path(current_root)
                for name in file_names:
                    (current_path / name).unlink()
                for name in directory_names:
                    child = current_path / name
                    if child.is_symlink():
                        child.unlink()
                    else:
                        child.rmdir()
        else:
            self.create_state_version(current, reason="before_clear")
        state = self._empty_state_with_memory()
        if remote_url:
            state["mobile_sync_settings"]["remote_base_url"] = remote_url
            state["mobile_sync_settings"]["updated_at"] = utc_now()
        saved = self.save_state(state)
        if permanent:
            for pattern in (
                "xingcheng-errors.jsonl",
                "xingcheng-errors.*.jsonl",
                "runtime-migration-manifest.json",
            ):
                for path in self.runtime_root.glob(pattern):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
        return saved
