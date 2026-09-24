"""StrategyValidationService — IS / validation / OOS / walk-forward.

Anti-overfit guarantees:
- every evaluation records its split and data window
- out-of-sample windows are consumed-once: once a result is recorded
  against an OOS window it is marked 'used' — re-running params on it
  is flagged OOS_REUSED rather than presented as fresh OOS evidence
- param search log is append-only; tiny valid ranges flag INSTABILITY
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .contracts import ParamSearchRecord


class StrategyValidationService:
    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "strategy_validations.jsonl"
        self._search_path = Path(state_dir) / "param_search.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._oos_used: set[tuple[str, str]] = set()  # (strategy_id, window)
        self._load_oos()

    # ------------------------------------------------------------------
    def split_windows(
        self, total_bars: int,
        in_sample: float = 0.6, validation: float = 0.2,
    ) -> dict[str, tuple[int, int]]:
        """Contiguous IS / validation / OOS index windows."""
        n = int(total_bars)
        a = int(n * in_sample)
        b = int(n * (in_sample + validation))
        return {
            "in_sample": (0, a),
            "validation": (a, b),
            "out_of_sample": (b, n),
        }

    def walk_forward_windows(
        self, total_bars: int, train: int, test: int,
    ) -> list[dict[str, tuple[int, int]]]:
        """Rolling train→test windows."""
        out: list[dict[str, tuple[int, int]]] = []
        i = 0
        n = int(total_bars)
        while i + train + test <= n:
            out.append({"train": (i, i + train),
                        "test": (i + train, i + train + test)})
            i += test
        return out

    # ------------------------------------------------------------------
    def record_evaluation(
        self, strategy_id: str, version: int, split: str,
        window: str, parameters: dict[str, Any],
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        flags: list[str] = []
        key = (strategy_id, window)
        if split == "out_of_sample" and key in self._oos_used:
            flags.append("OOS_REUSED")   # never claim fresh OOS twice
        if split == "out_of_sample":
            self._oos_used.add(key)
            self._persist_oos()
        rec = ParamSearchRecord(
            strategy_id=strategy_id, version=int(version),
            parameters=dict(parameters), data_window=window,
            split=split, metrics=dict(metrics))
        self._append(self._search_path, rec.to_dict())
        self._append(self._path, {
            "strategy_id": strategy_id, "version": int(version),
            "split": split, "window": window, "metrics": metrics,
            "flags": flags})
        return {"ok": True, "flags": flags, "record": rec.to_dict()}

    def search_log(self, strategy_id: str | None = None
                   ) -> list[dict[str, Any]]:
        return self._read(self._search_path, strategy_id)

    def stability_check(self, strategy_id: str) -> dict[str, Any]:
        """Flag when only a tiny parameter island is profitable."""
        recs = [r for r in self._read(self._search_path, strategy_id)
                if r.get("split") in ("in_sample", "validation")]
        if len(recs) < 3:
            return {"ok": True, "stability": "insufficient_runs",
                    "runs": len(recs)}
        rets = [float(r["metrics"].get("total_return") or 0) for r in recs]
        pos = sum(1 for r in rets if r > 0)
        ratio = pos / len(rets)
        flags = []
        if 0 < ratio < 0.35:
            flags.append("NARROW_PROFIT_ISLAND")
        return {"ok": True, "stability": "flagged" if flags else "ok",
                "runs": len(recs), "positive_ratio": ratio,
                "flags": flags}

    def history(self, strategy_id: str | None = None) -> list[dict[str, Any]]:
        return self._read(self._path, strategy_id)

    # ------------------------------------------------------------------
    def _load_oos(self) -> None:
        if not self._path.exists():
            return
        for row in self._read(self._path):
            if row.get("split") == "out_of_sample":
                self._oos_used.add(
                    (row.get("strategy_id", ""), row.get("window", "")))

    def _persist_oos(self) -> None:
        pass  # derived from validations journal — no extra file

    @staticmethod
    def _read(path: Path, strategy_id: str | None = None
              ) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if strategy_id and row.get("strategy_id") != strategy_id:
                continue
            out.append(row)
        return out

    @staticmethod
    def _append(path: Path, row: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class StrategyEvaluationService:
    """Objective-aware strategy assessment — user sets the objective."""

    OBJECTIVES = ("long_term_growth", "stable_cashflow",
                  "drawdown_control", "low_turnover", "concentration")

    def evaluate(self, metrics: dict[str, Any],
                 objective: str = "long_term_growth",
                 trades: list[dict[str, Any]] | None = None
                 ) -> dict[str, Any]:
        if objective not in self.OBJECTIVES:
            return {"ok": False, "error_code": "OBJECTIVE_UNKNOWN"}
        score = self._score(metrics, objective)
        return {
            "ok": True, "objective": objective, "score": score,
            "summary": {
                "total_return": metrics.get("total_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "sharpe": metrics.get("sharpe"),
                "trade_count": metrics.get("trade_count"),
                "cost_total": metrics.get("cost_total"),
            },
            "note": "目標函數由使用者設定；AI 不得自行變更",
        }

    @staticmethod
    def _score(m: dict[str, Any], objective: str) -> float:
        ret = float(m.get("total_return") or 0)
        dd = abs(float(m.get("max_drawdown") or 0))
        sharpe = float(m.get("sharpe") or 0)
        trades = float(m.get("trade_count") or 0)
        if objective == "long_term_growth":
            return ret * 0.5 + sharpe * 0.3 - dd * 0.2
        if objective == "stable_cashflow":
            return sharpe * 0.4 - dd * 0.5 + min(trades, 20) * 0.001
        if objective == "drawdown_control":
            return -dd + sharpe * 0.2
        if objective == "low_turnover":
            return ret * 0.4 - trades * 0.01
        if objective == "concentration":
            return -float(m.get("max_position_weight") or 0)
        return ret
