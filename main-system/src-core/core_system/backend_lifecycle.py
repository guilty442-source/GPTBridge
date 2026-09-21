"""§10.21 統一 Backend Lifecycle Contract（`star-backend-lifecycle/v1`）。

Blue-Green 之前每個模組各自發明生命週期狀態，導致同一語意（如「後端停止
接收新請求但仍在處理舊請求」）在不同模組有不同名稱。本模組定義**唯一**
的統一生命週期契約，供 Backend Self-Report、BootCore、Maintenance 等
元層共用，並擋住重複定義。

生命週期角色（Lifecycle Role）與健康狀態（Health State）必須分開表示：
- 角色：STARTING／ACTIVE／STANDBY／DRAINING／STOPPING／STOPPED
- 健康：READY／DEGRADED／RECOVERING／FAILED
- 例：`ACTIVE+READY`、`STANDBY+READY` 並存皆合法（同一生命週期，不可把
  READY 與 ACTIVE 當互斥同類）。

狀態機（可驗證轉移，非法轉移 fail-closed）：

```
STARTING ─→ (READY) ─→ ACTIVE ／ STANDBY ／ DRAINING ─→ STOPPING ─→ STOPPED
                │            ▲
                └─ DEGRADED ─┘   （DEGRADED / RECOVERING / FAILED 可掛載於
                                 任何非終止角色）
```

重名註冊防護：同 idempotency（backend_id）重複註冊回傳既有狀態，不遮蔽。
Filed 保真：`active_requests` 於角色離開 ACTIVE／STANDBY 時續存（可在
transition 前查詢），不因狀態轉移被清空。
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

_logger = logging.getLogger("gptbridge.backend_lifecycle")

BACKEND_LIFECYCLE_VERSION = "star-backend-lifecycle/v1"

# -- vocabulary ---------------------------------------------------------------

# 生命週期角色：決定後端是否負責／可切。
LIFECYCLE_ROLE_STARTING = "STARTING"
LIFECYCLE_ROLE_ACTIVE = "ACTIVE"
LIFECYCLE_ROLE_STANDBY = "STANDBY"
LIFECYCLE_ROLE_DRAINING = "DRAINING"
LIFECYCLE_ROLE_STOPPING = "STOPPING"
LIFECYCLE_ROLE_STOPPED = "STOPPED"

# 健康狀態：READY 之外的、DEGRADED / RECOVERING / FAILED。
HEALTH_READY = "READY"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_RECOVERING = "RECOVERING"
HEALTH_FAILED = "FAILED"

TERMINAL_ROLES = frozenset({LIFECYCLE_ROLE_STOPPED})
_ACTIVE_ROLES = frozenset(
    {LIFECYCLE_ROLE_ACTIVE, LIFECYCLE_ROLE_STANDBY}
)

# 角色轉移表。健康狀態可於任何非終止角色獨立變更，不受此表約束。
_ROLE_TRANSITIONS: dict[str, frozenset[str]] = {
    LIFECYCLE_ROLE_STARTING: frozenset(
        {
            LIFECYCLE_ROLE_ACTIVE,
            LIFECYCLE_ROLE_STANDBY,
            LIFECYCLE_ROLE_DRAINING,
            LIFECYCLE_ROLE_STOPPING,
        }
    ),
    LIFECYCLE_ROLE_ACTIVE: frozenset(
        {
            LIFECYCLE_ROLE_STANDBY,
            LIFECYCLE_ROLE_DRAINING,
            LIFECYCLE_ROLE_STOPPING,
            LIFECYCLE_ROLE_STOPPED,
        }
    ),
    LIFECYCLE_ROLE_STANDBY: frozenset(
        {
            LIFECYCLE_ROLE_ACTIVE,
            LIFECYCLE_ROLE_DRAINING,
            LIFECYCLE_ROLE_STOPPING,
            LIFECYCLE_ROLE_STOPPED,
        }
    ),
    LIFECYCLE_ROLE_DRAINING: frozenset(
        {LIFECYCLE_ROLE_STOPPING, LIFECYCLE_ROLE_STOPPED}
    ),
    LIFECYCLE_ROLE_STOPPING: frozenset({LIFECYCLE_ROLE_STOPPED}),
    LIFECYCLE_ROLE_STOPPED: frozenset(),
}

_HEALTH_TRANSITIONS: dict[str, frozenset[str]] = {
    HEALTH_READY: frozenset({HEALTH_DEGRADED, HEALTH_RECOVERING, HEALTH_FAILED}),
    HEALTH_DEGRADED: frozenset(
        {HEALTH_READY, HEALTH_RECOVERING, HEALTH_FAILED}
    ),
    HEALTH_RECOVERING: frozenset({HEALTH_READY, HEALTH_DEGRADED, HEALTH_FAILED}),
    HEALTH_FAILED: frozenset(),
}


def role_transition_valid(source: str, target: str) -> bool:
    return target in _ROLE_TRANSITIONS.get(source, frozenset())


def health_transition_valid(source: str, target: str) -> bool:
    return target in _HEALTH_TRANSITIONS.get(source, frozenset())


@dataclass
class BackendLifecycleState:
    """一個 Backend 的生命週期狀態（角色＋健康分開表示）。"""

    backend_id: str
    lifecycle_role: str = LIFECYCLE_ROLE_STARTING
    health_state: str = HEALTH_READY
    release_id: str = ""
    generation: str = ""
    started_at: str = field(default_factory=lambda: _utc_iso())
    last_heartbeat: str = field(default_factory=lambda: _utc_iso())
    active_requests: int = 0
    accepting_new_requests: bool = False
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LifecycleTransitionResult:
    ok: bool
    reason: str
    state: Optional[dict[str, Any]] = None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BackendLifecycleRegistry:
    """統一生命週期登錄簿（atomic write，backend_id 主鍵）。

    ``backend_id`` 為主鍵，同 id 只能有一份狀態。角色／健康轉移都會被
    fail-closed 驗證；heartbeat 更新 active_requests／last_heartbeat，
    並把角色是否接受新請求一併明確化。
    """

    def __init__(self, state_path: str | Path) -> None:
        self._path = Path(state_path)
        self._states: dict[str, BackendLifecycleState] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for entry in data.get("backends", []):
            try:
                state = BackendLifecycleState(**entry)
            except TypeError:
                continue
            self._states[state.backend_id] = state

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "backend_lifecycle_version": BACKEND_LIFECYCLE_VERSION,
            "updated_at": _utc_iso(),
            "backends": [
                state.as_dict() for state in self._states.values()
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

    # -- registration ---------------------------------------------------------

    def register(
        self,
        backend_id: str,
        *,
        release_id: str = "",
        generation: str = "",
        role: str = LIFECYCLE_ROLE_STARTING,
        health: str = HEALTH_READY,
    ) -> LifecycleTransitionResult:
        """註冊（或重註冊）一個 Backend。同 id 重註冊遮蔽為申請，不報錯。"""
        state = self._states.get(backend_id)
        if state is not None:
            return LifecycleTransitionResult(
                False, "already-registered", state.as_dict()
            )
        state = BackendLifecycleState(
            backend_id=backend_id,
            lifecycle_role=role,
            health_state=health,
            release_id=release_id,
            generation=generation,
        )
        self._states[backend_id] = state
        self._persist()
        return LifecycleTransitionResult(True, "registered", state.as_dict())

    # -- transitions ----------------------------------------------------------

    def _set_role(
        self, backend_id: str, target: str, error: str = ""
    ) -> LifecycleTransitionResult:
        state = self._states.get(backend_id)
        if state is None:
            return LifecycleTransitionResult(False, "backend-not-found")
        if not role_transition_valid(state.lifecycle_role, target):
            return LifecycleTransitionResult(
                False,
                f"illegal-role:{state.lifecycle_role}->{target}",
                state.as_dict(),
            )
        state.lifecycle_role = target
        state.last_heartbeat = _utc_iso()
        if error:
            state.last_error = error
        state.accepting_new_requests = target in _ACTIVE_ROLES
        if target in TERMINAL_ROLES:
            state.accepting_new_requests = False
        self._persist()
        return LifecycleTransitionResult(True, "role-set", state.as_dict())

    def _set_health(
        self, backend_id: str, target: str, error: str = ""
    ) -> LifecycleTransitionResult:
        state = self._states.get(backend_id)
        if state is None:
            return LifecycleTransitionResult(False, "backend-not-found")
        if not health_transition_valid(state.health_state, target):
            return LifecycleTransitionResult(
                False,
                f"illegal-health:{state.health_state}->{target}",
                state.as_dict(),
            )
        state.health_state = target
        state.last_heartbeat = _utc_iso()
        if error:
            state.last_error = error
        self._persist()
        return LifecycleTransitionResult(True, "health-set", state.as_dict())

    def become_active(self, backend_id: str) -> LifecycleTransitionResult:
        return self._set_role(backend_id, LIFECYCLE_ROLE_ACTIVE)

    def become_standby(self, backend_id: str) -> LifecycleTransitionResult:
        return self._set_role(backend_id, LIFECYCLE_ROLE_STANDBY)

    def begin_draining(self, backend_id: str) -> LifecycleTransitionResult:
        return self._set_role(backend_id, LIFECYCLE_ROLE_DRAINING)

    def begin_stopping(self, backend_id: str) -> LifecycleTransitionResult:
        return self._set_role(backend_id, LIFECYCLE_ROLE_STOPPING)

    def set_stopped(
        self, backend_id: str, error: str = ""
    ) -> LifecycleTransitionResult:
        return self._set_role(backend_id, LIFECYCLE_ROLE_STOPPED, error)

    def mark_ready(self, backend_id: str) -> LifecycleTransitionResult:
        return self._set_health(backend_id, HEALTH_READY)

    def mark_degraded(
        self, backend_id: str, error: str = ""
    ) -> LifecycleTransitionResult:
        return self._set_health(backend_id, HEALTH_DEGRADED, error)

    def mark_recovering(
        self, backend_id: str, error: str = ""
    ) -> LifecycleTransitionResult:
        return self._set_health(backend_id, HEALTH_RECOVERING, error)

    def mark_failed(
        self, backend_id: str, error: str = ""
    ) -> LifecycleTransitionResult:
        return self._set_health(backend_id, HEALTH_FAILED, error)

    # -- heartbeat -------------------------------------------------------------

    def heartbeat(
        self,
        backend_id: str,
        *,
        active_requests: Optional[int] = None,
        accepting: Optional[bool] = None,
    ) -> LifecycleTransitionResult:
        """Backend 定時回報：更新 last_heartbeat／active_requests。

        active_requests 為 current 數量（不是 delta）；不傳則保留現值。
        """
        state = self._states.get(backend_id)
        if state is None:
            return LifecycleTransitionResult(False, "backend-not-found")
        state.last_heartbeat = _utc_iso()
        if active_requests is not None:
            state.active_requests = max(0, int(active_requests))
        if accepting is not None:
            state.accepting_new_requests = bool(accepting)
        self._persist()
        return LifecycleTransitionResult(True, "heartbeat", state.as_dict())

    # -- queries ----------------------------------------------------------------

    def get(self, backend_id: str) -> Optional[BackendLifecycleState]:
        return self._states.get(backend_id)

    def by_generation(self, generation: str) -> list[BackendLifecycleState]:
        return [
            state
            for state in self._states.values()
            if state.generation == generation
        ]

    def active_backends(self) -> list[BackendLifecycleState]:
        """回傳目前接受新請求的後端（角色屬 ACTIVE/STANDBY 且 health 非
        FAILED）。可被 Gateway／Maintenance 查詢以決定負載承接對象。"""
        return [
            state
            for state in self._states.values()
            if state.accepting_new_requests
            and state.health_state != HEALTH_FAILED
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "backend_lifecycle_version": BACKEND_LIFECYCLE_VERSION,
            "backends": [state.as_dict() for state in self._states.values()],
            "count": len(self._states),
        }


__all__ = [
    "BACKEND_LIFECYCLE_VERSION",
    "BackendLifecycleRegistry",
    "BackendLifecycleState",
    "HEALTH_DEGRADED",
    "HEALTH_FAILED",
    "HEALTH_READY",
    "HEALTH_RECOVERING",
    "LIFECYCLE_ROLE_ACTIVE",
    "LIFECYCLE_ROLE_DRAINING",
    "LIFECYCLE_ROLE_STANDBY",
    "LIFECYCLE_ROLE_STARTING",
    "LIFECYCLE_ROLE_STOPPED",
    "LIFECYCLE_ROLE_STOPPING",
    "TERMINAL_ROLES",
    "LifecycleTransitionResult",
    "health_transition_valid",
    "role_transition_valid",
]