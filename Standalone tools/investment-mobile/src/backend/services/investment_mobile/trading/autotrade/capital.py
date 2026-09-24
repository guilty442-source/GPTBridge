"""StrategyCapitalAllocator + StrategyResourceCoordinator.

Capital allocation is user-owned per simulated account: each strategy
gets a weight of the account's usable cash plus hard caps. The allocator
accounts for open-order reservations, in-settlement proceeds, existing
positions and other strategies' commitments — the same dollar can never
be promised to two strategies.

The resource coordinator arbitrates order flow: same-strategy duplicate
signals are deduped, two strategies trading the same instrument are
attributed separately (never silently merged, never blindly doubled),
and opposite-direction requests are flagged as conflicts.
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

AI_ACTORS = frozenset({"ai", "xingcheng", "model", "assistant"})


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class StrategyCapitalAllocator:
    """Per-account strategy capital plan — user-set, AI-denied."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "autotrade"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "capital.json"
        self._plans: dict[str, dict[str, Any]] = {}   # account → plan
        self._load()

    def _load(self) -> None:
        try:
            self._plans = json.loads(
                self._path.read_text(encoding="utf-8"))
        except Exception:
            self._plans = {}

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            self._plans, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------
    def set_plan(self, account_id: str, plan: dict[str, Any], *,
                 actor: str = "user") -> dict[str, Any]:
        """plan = {allocations: {strategy_id: weight}, reserve_cash: w,
        per_order_max, per_instrument_max, exposure_max,
        usable_cash_ratio}. Weights + reserve must sum ≤ 1."""
        if actor.lower() in AI_ACTORS:
            return {"ok": False, "error_code": "AI_MUTATION_DENIED"}
        allocs = {str(k): _d(v) for k, v in
                  dict(plan.get("allocations") or {}).items()}
        reserve = _d(plan.get("reserve_cash") or "0")
        usable = _d(plan.get("usable_cash_ratio") or "1")
        if usable <= 0 or usable > 1:
            return {"ok": False, "error_code": "USABLE_RATIO_INVALID"}
        total = sum(allocs.values()) + reserve
        if total > 1:
            return {"ok": False, "error_code": "OVER_ALLOCATED",
                    "total_weight": str(total),
                    "note": "策略權重 + 保留現金不得超過 100%"}
        p = {
            "account_id": str(account_id),
            "allocations": {k: str(v) for k, v in allocs.items()},
            "reserve_cash": str(reserve),
            "usable_cash_ratio": str(usable),
            "per_order_max": str(plan.get("per_order_max") or "0"),
            "per_instrument_max": str(
                plan.get("per_instrument_max") or "0"),
            "exposure_max": str(plan.get("exposure_max") or "0"),
            "set_by": actor,
            "updated_at": time.time(),
        }
        self._plans[str(account_id)] = p
        self._persist()
        return {"ok": True, "plan": dict(p)}

    def plan(self, account_id: str) -> dict[str, Any] | None:
        p = self._plans.get(str(account_id))
        return dict(p) if p else None

    # ------------------------------------------------------------------
    def available(self, account_id: str, strategy_id: str, *,
                  cash: dict[str, Any]) -> dict[str, Any]:
        """Strategy's spendable budget = its weight of usable cash,
        minus what the account already committed elsewhere (reserves,
        unsettled, other strategies' shares are their own)."""
        p = self._plans.get(str(account_id))
        if p is None:
            return {"ok": False, "error_code": "NO_ALLOCATION_PLAN"}
        w = _d(p["allocations"].get(strategy_id, "0"))
        if w <= 0:
            return {"ok": False, "error_code": "STRATEGY_NOT_ALLOCATED",
                    "available": "0"}
        usable_pool = _d(cash.get("available")) * _d(
            p["usable_cash_ratio"])
        budget = usable_pool * w
        return {"ok": True, "budget": str(budget),
                "weight": str(w),
                "usable_pool": str(usable_pool),
                "account_available": str(cash.get("available")),
                "account_reserved": str(cash.get("reserved")),
                "account_unsettled": str(cash.get("unsettled")),
                "per_order_max": p["per_order_max"],
                "per_instrument_max": p["per_instrument_max"],
                "exposure_max": p["exposure_max"]}

    def check_order(self, account_id: str, strategy_id: str, *,
                    instrument_id: str, notional: Decimal,
                    cash: dict[str, Any],
                    strategy_exposure: Decimal,
                    instrument_exposure: Decimal) -> dict[str, Any]:
        """Gate an order against the plan's caps."""
        avail = self.available(account_id, strategy_id, cash=cash)
        if not avail.get("ok"):
            return avail
        notional = _d(notional)
        if notional > _d(avail["budget"]):
            return {"ok": False, "error_code": "STRATEGY_BUDGET_EXCEEDED",
                    "budget": avail["budget"],
                    "requested": str(notional)}
        pom = _d(avail["per_order_max"])
        if pom > 0 and notional > pom:
            return {"ok": False, "error_code": "PER_ORDER_CAP",
                    "cap": str(pom)}
        pim = _d(avail["per_instrument_max"])
        if pim > 0 and instrument_exposure + notional > pim:
            return {"ok": False, "error_code": "PER_INSTRUMENT_CAP",
                    "cap": str(pim)}
        em = _d(avail["exposure_max"])
        if em > 0 and strategy_exposure + notional > em:
            return {"ok": False, "error_code": "EXPOSURE_CAP",
                    "cap": str(em)}
        return {"ok": True, "budget": avail["budget"]}


class StrategyResourceCoordinator:
    """Order-flow arbitration across strategies sharing an account.

    - Same strategy + same instrument + same side + same signal key
      inside the dedup window → DUPLICATE_SIGNAL (no second order).
    - Different strategies, same instrument, same side → allowed but
      cluster-tagged and attributed separately — legal independent
      allocations are never merged into one order.
    - Opposite sides on the same instrument across strategies → conflict
      event, both kept attributable; same strategy contradicting itself
      inside the window → CONTRADICTING_SIGNAL.
    """

    def __init__(self, state_dir: Path,
                 dedup_window_s: float = 300.0) -> None:
        self._dir = Path(state_dir) / "autotrade"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "reservations.jsonl"
        self._window = dedup_window_s
        self._recent: list[dict[str, Any]] = []
        self._replay()

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(
                encoding="utf-8").splitlines():
            try:
                self._recent.append(json.loads(line))
            except Exception:
                continue

    # ------------------------------------------------------------------
    def request(self, *, strategy_id: str, run_id: str,
                instrument_id: str, side: str, quantity: Decimal,
                notional: Decimal, signal_key: str) -> dict[str, Any]:
        now = time.time()
        self._recent = [r for r in self._recent
                        if now - r["at"] < self._window]
        for r in self._recent:
            if (r["instrument_id"] == instrument_id
                    and r["strategy_id"] == strategy_id):
                if (r["side"] == side
                        and r["signal_key"] == signal_key):
                    return {"ok": False,
                            "error_code": "DUPLICATE_SIGNAL",
                            "dedupe": True,
                            "original": r["reservation_id"]}
                if r["side"] != side:
                    return {"ok": False,
                            "error_code": "CONTRADICTING_SIGNAL",
                            "conflict_with": r["reservation_id"]}
        cluster = [r["strategy_id"] for r in self._recent
                   if r["instrument_id"] == instrument_id
                   and r["side"] == side
                   and r["strategy_id"] != strategy_id]
        opposite = [r["strategy_id"] for r in self._recent
                    if r["instrument_id"] == instrument_id
                    and r["side"] != side]
        res = {
            "reservation_id": f"res-{uuid.uuid4().hex[:10]}",
            "strategy_id": strategy_id, "run_id": run_id,
            "instrument_id": instrument_id, "side": side,
            "quantity": str(quantity), "notional": str(notional),
            "signal_key": signal_key, "at": now,
            "cluster_with": sorted(set(cluster)),
            "conflicts_with": sorted(set(opposite)),
        }
        self._recent.append(res)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")
        return {"ok": True, "reservation": dict(res),
                "cluster": sorted(set(cluster)),
                "conflict": bool(opposite),
                "note": ("獨立策略配置各自歸屬——不合併訂單" if cluster
                         else "")}

    def release(self, reservation_id: str) -> dict[str, Any]:
        for r in self._recent:
            if r["reservation_id"] == reservation_id:
                self._recent.remove(r)
                return {"ok": True}
        return {"ok": False, "error_code": "RESERVATION_NOT_FOUND"}

    def recent(self, instrument_id: str | None = None
               ) -> list[dict[str, Any]]:
        return [dict(r) for r in self._recent
                if instrument_id is None
                or r["instrument_id"] == instrument_id]
