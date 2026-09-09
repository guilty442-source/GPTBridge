from __future__ import annotations

import contextlib
import shutil
import uuid
from pathlib import Path
from typing import Any


class VaultlyDestinationMixin:
    """Download destination validation and health checks."""

    async def _check_destination(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = str(payload.get("path", "")).strip()
        return {
            "ok": True,
            "destination_health": self._destination_health(path),
        }

    def _validate_destination(
        self,
        destination_text: str,
        required: bool,
    ) -> tuple[Path | None, dict[str, Any]]:
        health = self._destination_health(destination_text)
        if required and not health["ok"]:
            raise ValueError(str(health["message"]))
        if not health["ok"]:
            return None, health
        path = Path(str(health.get("path", ""))).expanduser() if health.get("path") else None
        return path, health

    def _destination_health(self, destination_text: str) -> dict[str, Any]:
        raw_path = str(destination_text or "").strip()
        if not raw_path:
            return {
                "ok": False,
                "path": "",
                "exists": False,
                "is_dir": False,
                "writable": False,
                "free_bytes": 0,
                "message": "尚未選擇下載資料夾",
            }
        try:
            path = Path(raw_path).expanduser()
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            return {
                "ok": False,
                "path": raw_path,
                "exists": False,
                "is_dir": False,
                "writable": False,
                "free_bytes": 0,
                "message": f"下載資料夾路徑無效：{self._short_error(exc)}",
            }
        exists = resolved.exists()
        is_dir = resolved.is_dir() if exists else False
        free_bytes = 0
        writable = False
        message = "下載資料夾可用"
        if exists and is_dir:
            with contextlib.suppress(OSError):
                free_bytes = int(shutil.disk_usage(resolved).free)
            writable = self._can_write_to_directory(resolved)
            if not writable:
                message = "下載資料夾無法寫入，請更換位置或調整權限"
            elif free_bytes and free_bytes < 100 * 1024 * 1024:
                message = "下載資料夾可寫入，但剩餘空間低於 100 MB"
        elif not exists:
            message = "下載資料夾不存在"
        else:
            message = "下載位置不是資料夾"
        return {
            "ok": bool(exists and is_dir and writable),
            "path": str(resolved),
            "exists": exists,
            "is_dir": is_dir,
            "writable": writable,
            "free_bytes": free_bytes,
            "message": message,
        }

    @staticmethod
    def _can_write_to_directory(path: Path) -> bool:
        probe = path / f".vaultly-write-test-{uuid.uuid4().hex}.tmp"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            with contextlib.suppress(OSError):
                probe.unlink()
            return False
