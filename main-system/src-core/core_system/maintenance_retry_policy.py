"""§10.4 預防性維護閉環——有界重試與冷卻政策（`star-maintenance-policy/v1`）。

每個修復目標（fault_code／target）獨立追蹤：

- **max_attempts**：冷卻窗內最大嘗試次數
- **retry_interval_s**：兩次嘗試最小間隔
- **cooldown_s**：達上限後的冷卻時間；冷卻期內新嘗試一律拒絕
- **修復前狀態**：每次嘗試記錄 pre_state（呼叫方提供）
- **修復後驗證**：`record_outcome(verified=…)`；未驗證成功計入重試預算
- **失敗升級**：預算耗盡 → `escalated=True`，不再自動嘗試（待人工）

防止「數秒內反覆重啟 PostgreSQL／Qdrant／Ollama」——反覆修復可能比原故障
更拖垮機器。持久化 JSON（atomic write）；預設閉合：狀態檔損毀時拒絕嘗試。
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

_logger = logging.getLogger("gptbridge.maintenance_retry_policy")

POLICY_VERSION = "star-maintenance-policy/v1"

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_INTERVAL_S = 60.0
DEFAULT_COOLDOWN_S = 900.0


@dataclass
class RetryDecision:
    allowed: bool
    reason: str
    retry_after_s: float = 0.0
    attempts_used: int = 0
    escalated: bool = False


@dataclass
class TargetLedger:
    target: str
    attempts: int = 0
    first_attempt_at: float = 0.0
    last_attempt_at: float = 0.0
    cooldown_until: float = 0.0
    escalated: bool = False
    last_pre_state: dict[str, Any] = field(default_factory=dict)
    last_verified: Optional[bool] = None
    history: list[dict[str, Any]] = field(default_factory=list)


class MaintenanceRetryPolicy:
    """有界重試＋冷卻閘門；查詢側永遠即時，寫入側 fail-closed。"""

    def __init__(
        self,
        state_path: str | Path,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        history_limit: int = 50,
    ) -> None:
        self._path = Path(state_path)
        self._max_attempts = max_attempts
        self._retry_interval = retry_interval_s
        self._cooldown = cooldown_s
        self._history_limit = history_limit
        self._ledgers: dict[str, TargetLedger] = {}
        self._corrupt = False
        self._load()

    # ---- persistence ----------------------------------------------------

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in data.get("targets", []):
                ledger = TargetLedger(**{
                    k: v for k, v in entry.items()
                    if k in TargetLedger.__dataclass_fields__
                })
                self._ledgers[ledger.target] = ledger
        except Exception as error:
            # fail-closed: unreadable ledger ⇒ treat every target as cooled
            _logger.warning("retry-policy ledger unreadable: %s", error)
            self._corrupt = True

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": POLICY_VERSION,
            "max_attempts": self._max_attempts,
            "retry_interval_s": self._retry_interval,
            "cooldown_s": self._cooldown,
            "targets": [asdict(l) for l in self._ledgers.values()],
        }
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp", prefix="retry-policy-"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- policy ---------------------------------------------------------

    def check(self, target: str, *, now: Optional[float] = None) -> RetryDecision:
        """May a repair attempt run for ``target`` right now?"""
        now = time.monotonic() if now is None else now
        if self._corrupt:
            return RetryDecision(False, "ledger-corrupt-fail-closed")
        ledger = self._ledgers.get(target)
        if ledger is None:
            return RetryDecision(True, "first-attempt")
        if ledger.escalated:
            return RetryDecision(
                False, "escalated", escalated=True,
                attempts_used=ledger.attempts,
            )
        if now < ledger.cooldown_until:
            return RetryDecision(
                False, "cooldown",
                retry_after_s=ledger.cooldown_until - now,
                attempts_used=ledger.attempts,
            )
        if ledger.attempts >= self._max_attempts:
            ledger.escalated = True
            self._persist()
            return RetryDecision(
                False, "attempts-exhausted-escalated",
                attempts_used=ledger.attempts, escalated=True,
            )
        if ledger.last_attempt_at and (
            now - ledger.last_attempt_at < self._retry_interval
        ):
            return RetryDecision(
                False, "retry-interval",
                retry_after_s=self._retry_interval
                - (now - ledger.last_attempt_at),
                attempts_used=ledger.attempts,
            )
        return RetryDecision(True, "allowed", attempts_used=ledger.attempts)

    def record_attempt(
        self, target: str, *, pre_state: Optional[dict[str, Any]] = None
    ) -> None:
        """Record an attempt start; callers must check() first."""
        now = time.monotonic()
        ledger = self._ledgers.get(target)
        if ledger is None:
            ledger = TargetLedger(target=target, first_attempt_at=now)
            self._ledgers[target] = ledger
        ledger.attempts += 1
        ledger.last_attempt_at = now
        ledger.last_pre_state = dict(pre_state or {})
        ledger.last_verified = None
        self._append(ledger, "attempt", {"pre_state": ledger.last_pre_state})
        self._persist()

    def record_outcome(self, target: str, *, verified: bool) -> None:
        """Post-repair verification result; unverified counts toward budget."""
        ledger = self._ledgers.get(target)
        if ledger is None:
            return
        ledger.last_verified = verified
        self._append(ledger, "outcome", {"verified": verified})
        if verified:
            # Verified repair closes the episode: the attempt budget is
            # per-episode (consecutive unverified attempts), not lifetime.
            ledger.attempts = 0
            ledger.cooldown_until = 0.0
        elif ledger.attempts >= self._max_attempts:
            ledger.cooldown_until = time.monotonic() + self._cooldown
        self._persist()

    def reset(self, target: str) -> None:
        """Manual reset after human review clears the escalation."""
        ledger = self._ledgers.get(target)
        if ledger is None:
            return
        ledger.attempts = 0
        ledger.cooldown_until = 0.0
        ledger.escalated = False
        self._append(ledger, "reset", {})
        self._persist()

    def ledger_snapshot(self) -> dict[str, Any]:
        return {
            "schema": POLICY_VERSION,
            "targets": {
                name: asdict(l) for name, l in sorted(self._ledgers.items())
            },
        }

    def _append(
        self, ledger: TargetLedger, event: str, extra: dict[str, Any]
    ) -> None:
        ledger.history.append(
            {"event": event, "at": time.time(), **extra}
        )
        if len(ledger.history) > self._history_limit:
            del ledger.history[: len(ledger.history) - self._history_limit]
