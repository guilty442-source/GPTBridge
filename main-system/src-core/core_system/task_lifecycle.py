"""§10.16 全專案唯一 Task Lifecycle（`star-task-lifecycle/v1`）。

所有產生工作的功能（Chat／Coding／RAG／Git／File Sorter／AI Collaboration
／Maintenance／Updater）不得各自發明任務生命週期，統一以本狀態機為準：

``CREATED → VALIDATED → AUTHORIZED → QUEUED → RUNNING → COMPLETED``
異常分支：``FAILED／CANCELLED／TIMED_OUT／RECOVERING``；新增
``INTERRUPTED``（任務本身不一定失敗，但執行它的 Backend 已中斷或被替換）。

識別與冪等：Task ID／Request ID／Operation ID／Execution Receipt／
Idempotency Key——**Operation ID 必須跨 Backend 更新保留**（本登錄簿以
``operation_id`` 為主鍵持久化），使中斷後可驗證再恢復。

規則：
* 終止狀態（COMPLETED／FAILED／CANCELLED／TIMED_OUT）不得再轉出。
* 非法轉移 fail-closed（拒絕並回報原因，不靜默忽略）。
* ``INTERRUPTED`` 僅可經 ``verify_then_resume`` 確認後恢復；不得盲刷全部
  RUNNING 重新執行（部分動作可能已完成但結果未回報）。
* 重複建立（同 idempotency_key）回傳既有紀錄，不產生第二次副作用。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.task_lifecycle")

TASK_LIFECYCLE_VERSION = "star-task-lifecycle/v1"

# -- status vocabulary -------------------------------------------------------

CREATED = "CREATED"
VALIDATED = "VALIDATED"
AUTHORIZED = "AUTHORIZED"
QUEUED = "QUEUED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
TIMED_OUT = "TIMED_OUT"
RECOVERING = "RECOVERING"
INTERRUPTED = "INTERRUPTED"

TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, CANCELLED, TIMED_OUT})

# operation_id → 最後一詞：決定任務在跨 Backend 更新中的歸屬與續存基礎。
# Validation (可進入 QUEUED/RUNNING 的起點)：僅 CREATED→VALIDATED 起始；
# AUTHORIZED 是受治理執行前的許可閘門。
_TRANSITIONS: dict[str, frozenset[str]] = {
    CREATED: frozenset({VALIDATED, CANCELLED, FAILED}),
    VALIDATED: frozenset({AUTHORIZED, CANCELLED, FAILED}),
    AUTHORIZED: frozenset({QUEUED, CANCELLED, FAILED}),
    QUEUED: frozenset({RUNNING, CANCELLED, TIMED_OUT, FAILED}),
    RUNNING: frozenset({COMPLETED, FAILED, CANCELLED, TIMED_OUT, INTERRUPTED}),
    RECOVERING: frozenset({RUNNING, FAILED, CANCELLED}),
    INTERRUPTED: frozenset({RECOVERING, FAILED, CANCELLED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    TIMED_OUT: frozenset(),
}

VALID_STATUSES = frozenset(_TRANSITIONS)


def transition_is_valid(source: str, target: str) -> bool:
    return target in _TRANSITIONS.get(source, frozenset())


@dataclass
class TaskLifecycleRecord:
    """一份持久化的任務生命週期紀錄；operation_id 為跨 Backend 主鍵。"""

    operation_id: str
    command: str
    idempotency_key: str
    status: str = CREATED
    task_id: str = ""
    request_id: str = ""
    execution_receipt: str = ""
    backend_generation: str = ""
    last_error: str = ""
    attempt_count: int = 0
    created_at: str = field(default_factory=lambda: _utc_iso())
    updated_at: str = field(default_factory=lambda: _utc_iso())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def new(
        operation_id: str, command: str, idempotency_key: str
    ) -> "TaskLifecycleRecord":
        return TaskLifecycleRecord(
            operation_id=operation_id,
            command=command,
            idempotency_key=idempotency_key,
        )


@dataclass
class TransitionResult:
    ok: bool
    reason: str
    record: Optional[dict[str, Any]] = None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskLifecycleRegistry:
    """持久化任務生命週期登錄簿（atomic write，operation_id 主鍵）。"""

    def __init__(self, state_path: str | Path) -> None:
        self._path = Path(state_path)
        self._records: dict[str, TaskLifecycleRecord] = {}
        self._idempotency: dict[str, str] = {}  # idempotency_key → op_id
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for entry in data.get("records", []):
            try:
                record = TaskLifecycleRecord(**entry)
            except TypeError:
                continue
            self._records[record.operation_id] = record
        for mapping in data.get("idempotency", []):
            self._idempotency[mapping["key"]] = mapping["operation_id"]

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_lifecycle_version": TASK_LIFECYCLE_VERSION,
            "updated_at": _utc_iso(),
            "records": [r.as_dict() for r in self._records.values()],
            "idempotency": [
                {"key": key, "operation_id": op_id}
                for key, op_id in sorted(self._idempotency.items())
            ],
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

    # -- creation / idempotency -----------------------------------------------

    def create(
        self,
        command: str,
        *,
        idempotency_key: Optional[str] = None,
        operation_id: Optional[str] = None,
        task_id: str = "",
        request_id: str = "",
        backend_generation: str = "",
    ) -> TransitionResult:
        """建立任務；同 idempotency_key 重複建立回傳既有紀錄（不重跑）。"""
        key = idempotency_key or uuid.uuid4().hex
        existing_op = self._idempotency.get(key)
        if existing_op is not None:
            existing = self._records.get(existing_op)
            if existing is not None:
                return TransitionResult(
                    False,
                    "duplicate-idempotency-key",
                    existing.as_dict(),
                )
        op_id = operation_id or uuid.uuid4().hex
        record = TaskLifecycleRecord.new(op_id, command, key)
        record.task_id = task_id
        record.request_id = request_id
        record.backend_generation = backend_generation
        self._records[op_id] = record
        self._idempotency[key] = op_id
        self._persist()
        return TransitionResult(True, CREATED, record.as_dict())

    # -- transitions -----------------------------------------------------------

    def _apply(self, op_id: str, target: str, reason: str) -> TransitionResult:
        record = self._records.get(op_id)
        if record is None:
            return TransitionResult(False, "operation-not-found")
        if not transition_is_valid(record.status, target):
            return TransitionResult(
                False,
                f"illegal-transition:{record.status}->{target}",
                record.as_dict(),
            )
        record.status = target
        record.updated_at = _utc_iso()
        self._persist()
        return TransitionResult(True, "transitioned", record.as_dict())

    def validate(self, operation_id: str) -> TransitionResult:
        return self._apply(operation_id, VALIDATED, "validation")

    def authorize(self, operation_id: str) -> TransitionResult:
        return self._apply(operation_id, AUTHORIZED, "authorization")

    def queue(self, operation_id: str) -> TransitionResult:
        return self._apply(operation_id, QUEUED, "queue")

    def start(
        self,
        operation_id: str,
        *,
        backend_generation: str = "",
        execution_receipt: str = "",
    ) -> TransitionResult:
        result = self._apply(operation_id, RUNNING, "start")
        if result.ok:
            record = self._records[operation_id]
            record.attempt_count += 1
            record.backend_generation = backend_generation
            record.execution_receipt = execution_receipt or uuid.uuid4().hex
            record.updated_at = _utc_iso()
            self._persist()
            return TransitionResult(True, "started", record.as_dict())
        return result

    def complete(self, operation_id: str) -> TransitionResult:
        return self._apply(operation_id, COMPLETED, "complete")

    def fail(
        self, operation_id: str, error: str = ""
    ) -> TransitionResult:
        result = self._apply(operation_id, FAILED, "failed")
        if result.ok and error:
            self._records[operation_id].last_error = error
            self._records[operation_id].updated_at = _utc_iso()
            self._persist()
        return result

    def cancel(self, operation_id: str) -> TransitionResult:
        return self._apply(operation_id, CANCELLED, "cancelled")

    def timeout(self, operation_id: str) -> TransitionResult:
        return self._apply(operation_id, TIMED_OUT, "timed-out")

    def interrupt(
        self, operation_id: str, error: str = ""
    ) -> TransitionResult:
        """Backend 中斷／被替換：標記 INTERRUPTED（本步可安全冪等）。"""
        result = self._apply(operation_id, INTERRUPTED, "interrupted")
        if result.ok and error:
            self._records[operation_id].last_error = error
            self._records[operation_id].updated_at = _utc_iso()
            self._persist()
        return result

    # -- recovery (INTERRUPTED → RECOVERING → verify) --------------------------

    def verify_and_resume(
        self,
        operation_id: str,
        *,
        safe: bool,
        verified_error: str = "",
    ) -> TransitionResult:
        """中斷後恢復：以 Maintenance 驗證結果決定再執行或終止。

        safe=True → RECOVERING（隨之可再 RUNNING）；safe=False → 不得重跑
        （結果不確定且可能已完成），依 last_error 終止。
        """
        record = self._records.get(operation_id)
        if record is None:
            return TransitionResult(False, "operation-not-found")
        if record.status != INTERRUPTED:
            return TransitionResult(
                False,
                f"not-interrupted:{record.status}",
                record.as_dict(),
            )
        target = RECOVERING if safe else FAILED
        result = self._apply(operation_id, target, "verify-then-resume")
        if (not safe) and verified_error:
            self._records[operation_id].last_error = verified_error
            self._records[operation_id].updated_at = _utc_iso()
            self._persist()
        return result

    # -- queries ----------------------------------------------------------------

    def get(self, operation_id: str) -> Optional[TaskLifecycleRecord]:
        return self._records.get(operation_id)

    def get_by_idempotency(
        self, idempotency_key: str
    ) -> Optional[TaskLifecycleRecord]:
        op_id = self._idempotency.get(idempotency_key)
        if op_id is None:
            return None
        return self._records.get(op_id)

    def running_count(self) -> int:
        return sum(
            1
            for r in self._records.values()
            if r.status in (RUNNING, RECOVERING, INTERRUPTED)
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_lifecycle_version": TASK_LIFECYCLE_VERSION,
            "records": [r.as_dict() for r in self._records.values()],
            "count": len(self._records),
            "running": self.running_count(),
        }


__all__ = [
    "AUTHORIZED",
    "CANCELLED",
    "COMPLETED",
    "CREATED",
    "FAILED",
    "INTERRUPTED",
    "QUEUED",
    "RECOVERING",
    "RUNNING",
    "TERMINAL_STATUSES",
    "TASK_LIFECYCLE_VERSION",
    "TIMED_OUT",
    "TaskLifecycleRecord",
    "TaskLifecycleRegistry",
    "TransitionResult",
    "VALIDATED",
    "transition_is_valid",
]