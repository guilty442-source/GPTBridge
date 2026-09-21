"""§10.22 Request Registry（`star-request-registry/v1`，向後相容擴充）。

擴充既有 Electron Main ↔ Python Backend 連線的請求追蹤，為 Backend
Gateway Handover 建立基礎。**優先擴充，不建立第二套**：既有結構對映——

* 後端：`tasks/queue.py::TaskRecord`（command／created_at／status）
* IPC：`server_commands.py`（request_id／cancelled）
* UI：`useBackendSocketOutbox`／`backendRecovery`（backend_generation）

本登錄簿以既有 ``request_id`` 為主鍵，新增可空欄位（task_id、backend_id、
release_id、started_at、completed_at、timeout、cancellation_state、
error_code…，§10.22 之 14 欄位），舊欄位一律保留 → 向前相容（加欄不加刪）。
狀態對齊 §10.16 Task Lifecycle 狀態集（CREATED→…→COMPLETED／FAILED／
TIMED_OUT／CANCELLED），不再各自為政。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core_system.task_lifecycle import (
    CANCELLED,
    COMPLETED,
    FAILED,
    RUNNING,
    TIMED_OUT,
)

_logger = logging.getLogger("gptbridge.request_registry")

REQUEST_REGISTRY_VERSION = "star-request-registry/v1"

# §10.22 14 欄位（優先擴充既有，不適用允許 null）。absence 以空字串/0 表示，
# 但寫入時保留 key——欄位存在性即「該追蹤信息可用」。
REQUEST_FIELDS = (
    "request_id",
    "session_id",
    "task_id",
    "backend_id",
    "backend_generation",
    "release_id",
    "method",
    "created_at",
    "started_at",
    "completed_at",
    "status",
    "timeout",
    "cancellation_state",
    "error_code",
)

_CREATED = "CREATED"
_QUEUED = "QUEUED"


@dataclass
class RequestRecord:
    """一份請求的 14 欄位追蹤紀錄；request_id 為主鍵。"""

    request_id: str
    session_id: str = ""
    task_id: str = ""
    backend_id: str = ""
    backend_generation: str = ""
    release_id: str = ""
    method: str = ""
    created_at: str = field(default_factory=lambda: _utc_iso())
    started_at: str = ""
    completed_at: str = ""
    status: str = _CREATED
    timeout: float = 0.0
    cancellation_state: str = ""
    error_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RequestResult:
    ok: bool
    reason: str
    record: Optional[dict[str, Any]] = None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RequestRegistry:
    """擴充既有 IPC 請求追蹤的持久化登錄簿（atomic write）。

    ``record`` 接受既有 payload（只帶 command/status/cancelled 的舊格式），
    會把舊欄位映射到新結構：
    - ``TaskRecord.command`` → method
    - ``TaskRecord.status`` → status（對齊 §10.16）
    - ``cancelled`` 布林 → cancellation_state（``cancelled``／``requested``）
    """

    def __init__(self, state_path: str | Path) -> None:
        self._path = Path(state_path)
        self._records: dict[str, RequestRecord] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for entry in data.get("requests", []):
            try:
                record = RequestRecord(**entry)
            except TypeError:
                continue
            self._records[record.request_id] = record

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "request_registry_version": REQUEST_REGISTRY_VERSION,
            "updated_at": _utc_iso(),
            "requests": [record.as_dict() for record in self._records.values()],
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

    # -- create / track --------------------------------------------------------

    def upsert(
        self,
        request_id: str,
        *,
        session_id: str = "",
        task_id: str = "",
        backend_id: str = "",
        backend_generation: str = "",
        release_id: str = "",
        method: str = "",
        cancelled: bool | None = None,
        status: str = "",
    ) -> RequestResult:
        """建立或更新請求紀錄。

        重複 request_id → 回傳既有紀錄並合併新欄位（重試語意，不改 id）。
        ``cancelled`` 布林（既有 UI/IPC 欄位）對映 cancellation_state。
        """
        existing = self._records.get(request_id)
        if existing is None:
            existing = RequestRecord(
                request_id=request_id,
                session_id=session_id,
                task_id=task_id,
                backend_id=backend_id,
                backend_generation=backend_generation,
                release_id=release_id,
                method=method,
            )
            self._records[request_id] = existing
        else:
            if session_id:
                existing.session_id = session_id
            if task_id:
                existing.task_id = task_id
            if backend_id:
                existing.backend_id = backend_id
            if backend_generation:
                existing.backend_generation = backend_generation
            if release_id:
                existing.release_id = release_id
            if method:
                existing.method = method
        if status:
            existing.status = status
        if cancelled is not None:
            existing.cancellation_state = (
                "cancelled" if cancelled else ""
            )
        self._persist()
        return RequestResult(True, "upserted", existing.as_dict())

    def update(
        self,
        request_id: str,
        *,
        status: str | None = None,
        task_id: str | None = None,
        backend_id: str | None = None,
        backend_generation: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        timeout: float | None = None,
        cancellation_state: str | None = None,
        error_code: str | None = None,
    ) -> RequestResult:
        """欄位定向更新；request_id 不存在 → fail-closed 拒絕。"""
        record = self._records.get(request_id)
        if record is None:
            return RequestResult(False, "request-not-found")
        if status is not None:
            record.status = status
        if task_id is not None:
            record.task_id = task_id
        if backend_id is not None:
            record.backend_id = backend_id
        if backend_generation is not None:
            record.backend_generation = backend_generation
        if started_at is not None:
            record.started_at = started_at
        if completed_at is not None:
            record.completed_at = completed_at
        if timeout is not None:
            record.timeout = float(timeout)
        if cancellation_state is not None:
            record.cancellation_state = cancellation_state
        if error_code is not None:
            record.error_code = error_code
        self._persist()
        return RequestResult(True, "updated", record.as_dict())

    def mark_started(self, request_id: str) -> RequestResult:
        return self.update(request_id, status=RUNNING, started_at=_utc_iso())

    def mark_completed(
        self, request_id: str, *, error_code: str = ""
    ) -> RequestResult:
        return self.update(
            request_id,
            status=COMPLETED,
            completed_at=_utc_iso(),
            error_code=error_code,
        )

    def mark_failed(
        self, request_id: str, *, error_code: str = ""
    ) -> RequestResult:
        return self.update(
            request_id,
            status=FAILED,
            completed_at=_utc_iso(),
            error_code=error_code,
        )

    def mark_timed_out(self, request_id: str) -> RequestResult:
        return self.update(
            request_id, status=TIMED_OUT, completed_at=_utc_iso()
        )

    def request_cancel(self, request_id: str) -> RequestResult:
        return self.update(
            request_id, cancellation_state="requested"
        )

    def mark_cancelled(self, request_id: str) -> RequestResult:
        return self.update(
            request_id, status=CANCELLED, cancellation_state="cancelled"
        )

    # -- queries ---------------------------------------------------------------

    def get(self, request_id: str) -> Optional[RequestRecord]:
        return self._records.get(request_id)

    def in_flight(self) -> list[RequestRecord]:
        """未終止的請求：RUNNING／QUEUED／CREATED。"""
        return [
            record
            for record in self._records.values()
            if record.status in (RUNNING, _QUEUED, _CREATED)
        ]

    def by_backend_generation(self, generation: str) -> list[RequestRecord]:
        return [
            record
            for record in self._records.values()
            if record.backend_generation == generation
        ]

    def by_backend(self, backend_id: str) -> list[RequestRecord]:
        return [
            record
            for record in self._records.values()
            if record.backend_id == backend_id
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "request_registry_version": REQUEST_REGISTRY_VERSION,
            "fields": list(REQUEST_FIELDS),
            "requests": [record.as_dict() for record in self._records.values()],
            "count": len(self._records),
        }


__all__ = [
    "REQUEST_FIELDS",
    "REQUEST_REGISTRY_VERSION",
    "RequestRecord",
    "RequestRegistry",
    "RequestResult",
]