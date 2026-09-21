"""§10.10 長駐程序統一管理——Process Registry（`star-process-registry/v1`）。

記錄每個受管程序的 PID、Parent PID、Executable、Module ID、Release ID、
Start Time、Health、Restart Count、Shutdown State；持久化到
``runtime/state/process-registry.json``（atomic write）。

所有權界線（fail-closed）：``is_owned(pid)`` 只對 GPTBridge 實際啟動且
仍在冊的程序回 True——Ollama／PostgreSQL 等共享服務若非本系統啟動，
只能請求／提示，不得終止。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.process_registry")

REGISTRY_VERSION = "star-process-registry/v1"


@dataclass
class ProcessRecord:
    pid: int
    ppid: int
    executable: str
    module_id: str
    release_id: str = ""
    request_id: str = ""
    started_at: str = ""
    health: str = "running"
    restart_count: int = 0
    shutdown_state: str = ""  # ""=running, "exited", "released", "failed"
    owned: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class ProcessRegistry:
    """受管程序的持久化登錄簿；變更即寫盤（atomic）。"""

    def __init__(self, state_path: str | Path) -> None:
        self._path = Path(state_path)
        self._records: dict[int, ProcessRecord] = {}
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for entry in data.get("processes", []):
            try:
                record = ProcessRecord(**entry)
            except TypeError:
                continue
            self._records[record.pid] = record

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "registry_version": REGISTRY_VERSION,
            "updated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "processes": [asdict(r) for r in self._records.values()],
        }
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- registration -------------------------------------------------------

    def register(
        self,
        pid: int,
        *,
        module_id: str,
        executable: str = "",
        request_id: str = "",
        release_id: str = "",
        owned: bool = True,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ProcessRecord:
        """登錄一個受管程序；同 module 前代已 exited 時累計 restart_count。"""
        prior = self._records.get(pid)
        restart = 0
        if prior is not None and prior.module_id == module_id:
            restart = prior.restart_count + (1 if prior.shutdown_state else 0)
        record = ProcessRecord(
            pid=int(pid),
            ppid=os.getppid() if hasattr(os, "getppid") else 0,
            executable=executable,
            module_id=module_id,
            release_id=release_id,
            request_id=request_id,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            health="running",
            restart_count=restart,
            owned=owned,
            metadata=metadata or {},
        )
        self._records[record.pid] = record
        self._persist()
        return record

    def mark_shutdown(self, pid: int, state: str = "exited") -> None:
        record = self._records.get(int(pid))
        if record is None:
            return
        record.shutdown_state = state
        record.health = "stopped" if state in ("exited", "failed") else "detached"
        self._persist()

    def mark_health(self, pid: int, health: str) -> None:
        record = self._records.get(int(pid))
        if record is None:
            return
        record.health = health
        self._persist()

    # -- queries ------------------------------------------------------------

    def is_owned(self, pid: int) -> bool:
        """所有權界線：只有本系統啟動、仍在冊且尚未結束的程序可終止。

        ``released``（放手追蹤但仍存活的本系統子程序）仍屬 owned；
        ``exited``／``failed`` 則已無可終止。"""
        record = self._records.get(int(pid))
        return bool(
            record is not None
            and record.owned
            and record.shutdown_state not in ("exited", "failed")
        )

    def get(self, pid: int) -> Optional[ProcessRecord]:
        return self._records.get(int(pid))

    def snapshot(self) -> dict[str, Any]:
        return {
            "registry_version": REGISTRY_VERSION,
            "processes": [asdict(r) for r in self._records.values()],
            "active": sum(
                1
                for r in self._records.values()
                if r.shutdown_state not in ("exited", "failed")
            ),
        }

    # -- liveness reconciliation --------------------------------------------

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Return liveness without treating a recycled/zombie PID as active."""
        if pid <= 0:
            return False
        try:
            import psutil

            process = psutil.Process(pid)
            if not process.is_running():
                return False
            return process.status() != psutil.STATUS_ZOMBIE
        except ImportError:
            # Keep the registry usable in the minimal release environment.
            pass
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False
        except psutil.AccessDenied:
            return True
        except OSError:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def reconcile(self) -> dict[str, int]:
        """掃描在冊程序：PID 已消失者標記 exited（孤兒偵測）。"""
        stats = {"checked": 0, "marked_exited": 0}
        changed = False
        for record in self._records.values():
            if record.shutdown_state in ("exited", "failed"):
                continue
            stats["checked"] += 1
            if not self._pid_alive(record.pid):
                record.shutdown_state = "exited"
                record.health = "stopped"
                stats["marked_exited"] += 1
                changed = True
        if changed:
            self._persist()
        return stats
