"""§10.17 Runtime Execution Lease（`star-execution-lease/v1`）。

Blue-Green 時 ACTIVE 與 STANDBY 同時載入後，只有持有有效租約的 Backend
才能執行指定的單例工作（清理、備份、資料庫遷移、排程）。本模組提供：

* **單一權威來源**：持久化 ``runtime/state/execution-lease.json``（atomic
  write）——不得由兩個 Python 程序各自改記憶體 ``active=True``。
* **fencing token**：每次授予租約遞增（monotonic）；執行端（DB 寫入、
  排程、備份）以 ``verify`` 校驗，避免舊程序恢復後執行過期工作。
* **原子化交接**：``handover`` 在同一持有者世代內原子地撤銷並重授予，
  供 Gateway 切換流程呼叫（新版健康檢查通過 → 停止送新任務 → 撤銷 A →
  授予 B → 切換）。

fail-closed：租約不存在／過期／世代不符／token 不符一律拒絕；逾時可按
``ttl_seconds`` 授予並以 ``expire_stale`` 回收。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.execution_lease")

LEASE_VERSION = "star-execution-lease/v1"

DEFAULT_LEASE_TTL_SECONDS: float = 300.0
# How long an acquired lease survives without renewal before it is stale
# (owner likely crashed).  Bounds the window for duplicate singleton work.
DEFAULT_STALE_SECONDS: float = 120.0


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExecutionLease:
    """一份執行權租約：只有 holder 且 fencing token 相符者才能執行。

    世代（backend_generation）綁定租約歸屬——舊世代程序即使 token 相符
    也因世代不符而被拒絕，跨 Backend 更新不會誤授執行權。
    """

    lease_type: str
    backend_generation: str
    holder: str
    acquired_at: str
    expires_at: str
    fencing_token: int
    ttl_seconds: float = field(default=DEFAULT_LEASE_TTL_SECONDS)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_expired(self, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        try:
            return now > datetime.fromisoformat(self.expires_at).timestamp()
        except (ValueError, OSError):
            return True


@dataclass
class LeaseResult:
    ok: bool
    reason: str
    lease: Optional[dict[str, Any]] = None
    fencing_token: Optional[int] = None


class RuntimeExecutionLease:
    """單一權威來源的執行租約登錄簿；變更即寫盤（atomic）。

    每種 ``lease_type`` 各持有一份租約；同 lease_type 同世代持有者為同一
    fence 主體，可透過 ``renew`` 或 ``handover`` 延續或交接（token 遞增）。
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
    ) -> None:
        self._path = Path(state_path)
        self._leases: dict[str, ExecutionLease] = {}
        self._token_counters: dict[str, int] = {}
        self._ttl_seconds = ttl_seconds
        self._stale_seconds = stale_seconds
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for entry in data.get("leases", []):
            try:
                lease = ExecutionLease(**entry)
            except TypeError:
                continue
            self._leases[lease.lease_type] = lease
        for token in data.get("fencing_tokens", []):
            self._token_counters[token["lease_type"]] = int(
                token["last_token"]
            )

    def _persist(self) -> None:
        payload = {
            "lease_version": LEASE_VERSION,
            "updated_at": _iso_now(),
            "leases": [lease.as_dict() for lease in self._leases.values()],
            "fencing_tokens": [
                {"lease_type": lt, "last_token": token}
                for lt, token in sorted(self._token_counters.items())
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
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

    # -- helpers --------------------------------------------------------------

    def _next_token(self, lease_type: str) -> int:
        token = self._token_counters.get(lease_type, 0) + 1
        self._token_counters[lease_type] = token
        return token

    @staticmethod
    def _expires(ttl_seconds: float) -> str:
        return datetime.fromtimestamp(
            time.time() + ttl_seconds, timezone.utc
        ).isoformat()

    def _drop_expired(self, lease_type: str) -> None:
        lease = self._leases.get(lease_type)
        if lease is None:
            return
        if lease.is_expired():
            _logger.warning(
                "execution-lease expired lease_type=%s generation=%s holder=%s",
                lease_type,
                lease.backend_generation,
                lease.holder,
            )
            del self._leases[lease_type]

    # -- operations ----------------------------------------------------------

    def acquire(
        self,
        lease_type: str,
        holder: str,
        backend_generation: str,
        *,
        ttl_seconds: Optional[float] = None,
    ) -> LeaseResult:
        """嘗試取得執行權租約。

        已被同一持有者持有 → 視為續期（token 不變）；被他人持有且未過期
        → 拒絶（fail-closed）。首次取得授予新的 fencing token。
        """
        self._drop_expired(lease_type)
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl_seconds
        existing = self._leases.get(lease_type)
        if existing is not None:
            if existing.holder == holder:
                return LeaseResult(
                    False, "already-held-by-self", existing.as_dict()
                )
            return LeaseResult(False, "lease-busy", existing.as_dict())

        token = self._next_token(lease_type)
        lease = ExecutionLease(
            lease_type=lease_type,
            backend_generation=backend_generation,
            holder=holder,
            acquired_at=_iso_now(),
            expires_at=self._expires(ttl),
            fencing_token=token,
            ttl_seconds=ttl,
        )
        self._leases[lease_type] = lease
        self._persist()
        _logger.info(
            "execution-lease acquired lease_type=%s holder=%s generation=%s token=%d",
            lease_type,
            holder,
            backend_generation,
            token,
        )
        return LeaseResult(
            True, "acquired", lease.as_dict(), fencing_token=token
        )

    def renew(
        self,
        lease_type: str,
        holder: str,
        backend_generation: str,
        fencing_token: int,
        *,
        ttl_seconds: Optional[float] = None,
    ) -> LeaseResult:
        """由現任持有者續期（token 不變）。不吻合一律拒絕。"""
        self._drop_expired(lease_type)
        existing = self._leases.get(lease_type)
        if existing is None:
            return LeaseResult(False, "lease-not-held")
        if (
            existing.holder != holder
            or existing.backend_generation != backend_generation
            or existing.fencing_token != fencing_token
        ):
            return LeaseResult(False, "lease-mismatch", existing.as_dict())
        ttl = ttl_seconds if ttl_seconds is not None else existing.ttl_seconds
        existing.ttl_seconds = ttl
        existing.expires_at = self._expires(ttl)
        self._persist()
        return LeaseResult(
            True,
            "renewed",
            existing.as_dict(),
            fencing_token=existing.fencing_token,
        )

    def verify(
        self,
        lease_type: str,
        holder: str,
        backend_generation: str,
        fencing_token: int,
    ) -> bool:
        """執行端校驗：租約存在、未過期、持有者與世代與 token 全相符。

        fencing token 防護：舊 token 或舊世代程序恢復後執行時直接失效。
        """
        self._drop_expired(lease_type)
        lease = self._leases.get(lease_type)
        if lease is None:
            return False
        return bool(
            lease.holder == holder
            and lease.backend_generation == backend_generation
            and lease.fencing_token == fencing_token
            and not lease.is_expired()
        )

    def handover(
        self,
        lease_type: str,
        old_holder: str,
        old_fencing_token: int,
        new_holder: str,
        backend_generation: str,
        *,
        ttl_seconds: Optional[float] = None,
    ) -> LeaseResult:
        """原子化交接：撤銷舊持有者並授予新持有者（同一世代）。

        舊 token 不符／無現租約 → fail-closed 拒絕，不產生新 token。
        """
        self._drop_expired(lease_type)
        existing = self._leases.get(lease_type)
        if existing is None:
            return LeaseResult(False, "lease-not-held")
        if (
            existing.holder != old_holder
            or existing.fencing_token != old_fencing_token
            or existing.backend_generation != backend_generation
        ):
            return LeaseResult(False, "handover-mismatch", existing.as_dict())

        ttl = ttl_seconds if ttl_seconds is not None else self._ttl_seconds
        token = self._next_token(lease_type)
        lease = ExecutionLease(
            lease_type=lease_type,
            backend_generation=backend_generation,
            holder=new_holder,
            acquired_at=_iso_now(),
            expires_at=self._expires(ttl),
            fencing_token=token,
            ttl_seconds=ttl,
        )
        self._leases[lease_type] = lease
        self._persist()
        _logger.info(
            "execution-lease handover lease_type=%s %s->%s generation=%s token=%d",
            lease_type,
            old_holder,
            new_holder,
            backend_generation,
            token,
        )
        return LeaseResult(
            True, "handed-over", lease.as_dict(), fencing_token=token
        )

    def release(
        self,
        lease_type: str,
        holder: str,
        backend_generation: str,
        fencing_token: int,
    ) -> LeaseResult:
        """由現任持有者釋放租約（供排空後回收執行權）。"""
        existing = self._leases.get(lease_type)
        if existing is None:
            return LeaseResult(False, "lease-not-held")
        if (
            existing.holder != holder
            or existing.backend_generation != backend_generation
            or existing.fencing_token != fencing_token
        ):
            return LeaseResult(False, "lease-mismatch", existing.as_dict())
        del self._leases[lease_type]
        self._persist()
        return LeaseResult(True, "released")

    def expire_stale(self) -> dict[str, int]:
        """回收所有已過期租約；回報清除數。"""
        dropped = 0
        for lease_type in list(self._leases):
            lease = self._leases[lease_type]
            if lease.is_expired():
                _logger.warning(
                    "execution-lease stale-removed lease_type=%s holder=%s",
                    lease_type,
                    lease.holder,
                )
                del self._leases[lease_type]
                dropped += 1
        if dropped:
            self._persist()
        return {"dropped": dropped}

    # -- queries --------------------------------------------------------------

    def held(self, lease_type: str) -> bool:
        self._drop_expired(lease_type)
        return lease_type in self._leases

    def get(self, lease_type: str) -> Optional[ExecutionLease]:
        self._drop_expired(lease_type)
        return self._leases.get(lease_type)

    def snapshot(self) -> dict[str, Any]:
        return {
            "lease_version": LEASE_VERSION,
            "leases": [lease.as_dict() for lease in self._leases.values()],
            "count": len(self._leases),
        }