"""InvestmentIntentParser — 繁體中文投資語句 → structured intent.

Instrument names resolve through the instrument registry; ambiguous
aliases (台積電 vs TSM ADR) are kept distinct and surface an explicit
disambiguation request rather than collapsing to one identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import AnalysisTaskKind


@dataclass
class ParsedIntent:
    task_kind: str = ""
    market: str = ""                       # tw|us|fund|portfolio|""
    instrument_id: str = ""                # canonical if resolved
    account_id: str = ""
    time_range: str = ""                   # e.g. "1m", "1y"
    action: str = ""                       # analyze|compare|check_risk|observe
    candidates: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: bool = False
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_kind": self.task_kind, "market": self.market,
            "instrument_id": self.instrument_id,
            "account_id": self.account_id, "time_range": self.time_range,
            "action": self.action, "candidates": self.candidates,
            "ambiguous": self.ambiguous, "raw": self.raw,
        }


# Alias table — curated, NOT name-similarity matching. Same entity on
# different markets stays separate: 台積電(2330 TW) ≠ TSM(ADR US).
_ALIAS_TABLE: dict[str, list[tuple[str, str]]] = {
    "台積電": [("tw", "tw:TW_STOCK:2330:TWD")],
    "2330": [("tw", "tw:TW_STOCK:2330:TWD")],
    "tsm": [("us", "us:US_STOCK:TSM:USD")],
    "apple": [("us", "us:US_STOCK:AAPL:USD")],
    "蘋果": [("us", "us:US_STOCK:AAPL:USD")],
    "aapl": [("us", "us:US_STOCK:AAPL:USD")],
    "spy": [("us", "us:ETF:SPY:USD")],
}

_TASK_KEYWORDS: list[tuple[str, str]] = [
    ("風險", AnalysisTaskKind.PORTFOLIO_RISK),
    ("持倉", AnalysisTaskKind.PORTFOLIO_RISK),
    ("總資產", AnalysisTaskKind.PORTFOLIO_RISK),
    ("資產配置", AnalysisTaskKind.PORTFOLIO_RISK),
    ("基金", AnalysisTaskKind.FUND),
    ("淨值", AnalysisTaskKind.FUND),
    ("定期定額", AnalysisTaskKind.FUND),
    ("財報", AnalysisTaskKind.EARNINGS),
    ("財測", AnalysisTaskKind.EARNINGS),
    ("研究", AnalysisTaskKind.DEEP_RESEARCH),
    ("報告", AnalysisTaskKind.REPORT),
    ("分析", AnalysisTaskKind.QUICK_MARKET),
    ("觀察", AnalysisTaskKind.QUICK_MARKET),
    ("比較", AnalysisTaskKind.FUND),
]

_MARKET_KEYWORDS = [
    ("台股", "tw"), ("台灣", "tw"), ("上市", "tw"), ("上櫃", "tw"),
    ("國泰", "tw"),
    ("美股", "us"), ("美國", "us"), ("富邦", "us"), ("複委託", "us"),
    ("nasdaq", "us"), ("s&p", "us"),
    ("基金", "fund"), ("淨值", "fund"),
    ("資產", "portfolio"), ("全部", "portfolio"), ("整體", "portfolio"),
]

_ACCOUNT_KEYWORDS = [
    ("國泰", "cathay-tw-main"), ("富邦", "fubon-us-main"),
    ("複委託", "fubon-us-main"),
]

_TIME_PATTERNS = [
    (r"近一個月|一個月", "1m"), (r"近三個月|三個月|一季", "3m"),
    (r"近半年|半年|六個月", "6m"), (r"近一年|一年", "1y"),
    (r"近三年|三年", "3y"), (r"近五年|五年", "5y"),
    (r"成立以來", "inception"),
]


class InvestmentIntentParser:
    """Deterministic parser — model only refines, never rewrites identity."""

    def __init__(
        self,
        alias_table: dict[str, list[tuple[str, str]]] | None = None,
    ) -> None:
        self._aliases = dict(_ALIAS_TABLE)
        if alias_table:
            self._aliases.update(alias_table)

    def register_alias(self, phrase: str, market: str,
                       instrument_id: str) -> None:
        """Registry-fed aliases (e.g. fund names from FundIdentity)."""
        self._aliases.setdefault(phrase, []).append((market, instrument_id))

    def parse(self, text: str) -> ParsedIntent:
        raw = str(text or "")
        low = raw.lower()
        intent = ParsedIntent(raw=raw)

        for kw, kind in _TASK_KEYWORDS:
            if kw in raw:
                intent.task_kind = kind
                break
        for kw, market in _MARKET_KEYWORDS:
            if kw.lower() in low:
                intent.market = market
                break
        for kw, acct in _ACCOUNT_KEYWORDS:
            if kw in raw:
                intent.account_id = acct
                break
        for pat, rng in _TIME_PATTERNS:
            if re.search(pat, raw):
                intent.time_range = rng
                break

        # instrument resolution — collect ALL alias hits
        hits: list[tuple[str, str]] = []
        for phrase, targets in self._aliases.items():
            if phrase.lower() in low:
                hits.extend(targets)
        # dedupe, keep order
        seen: set[str] = set()
        for market, iid in hits:
            if iid not in seen:
                seen.add(iid)
                intent.candidates.append(
                    {"market": market, "instrument_id": iid})
        if len(intent.candidates) == 1:
            intent.instrument_id = intent.candidates[0]["instrument_id"]
            intent.market = intent.market or intent.candidates[0]["market"]
        elif len(intent.candidates) > 1:
            intent.ambiguous = True  # never silently pick one

        if not intent.task_kind:
            intent.task_kind = AnalysisTaskKind.QUICK_MARKET
        intent.action = (
            "compare" if "比較" in raw else
            "check_risk" if "風險" in raw else
            "observe" if "觀察" in raw else
            "analyze"
        )
        return intent
