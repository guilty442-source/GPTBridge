"""PortfolioEnvironmentIsolation — four non-interchangeable views.

MANUAL_PORTFOLIO   user-maintained assets (this package's authority)
SHADOW_PORTFOLIO   AI signal-research view (immutable signals)
PAPER_PORTFOLIO    simulated trading books (paper-* ledgers)
LIVE_PORTFOLIO     future broker-synced view (unreachable this phase)

Rules:

- PAPER writes can never mutate MANUAL records — paper ids are paper-*.
- Manual/imported holdings are tagged MANUAL/FILE_IMPORT forever; they
  can never be re-tagged as LIVE/broker-confirmed.
- LIVE view is empty this phase (no broker connection).
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class PortfolioEnvironment(str, Enum):
    MANUAL = "MANUAL_PORTFOLIO"
    SHADOW = "SHADOW_PORTFOLIO"
    PAPER = "PAPER_PORTFOLIO"
    LIVE = "LIVE_PORTFOLIO"


class PortfolioEnvironmentIsolation:
    def __init__(self, offline: Any, sim: Any | None = None,
                 live: Any | None = None) -> None:
        self._offline = offline
        self._sim = sim
        self._live = live

    # ------------------------------------------------------------------
    def classify(self, account_id: str) -> PortfolioEnvironment:
        aid = str(account_id)
        if aid.startswith("paper-"):
            return PortfolioEnvironment.PAPER
        if self._offline.get(aid):
            return PortfolioEnvironment.MANUAL
        return PortfolioEnvironment.LIVE

    def assert_write(self, account_id: str, writer: str,
                     environment: PortfolioEnvironment) -> dict[str, Any]:
        """Environment-confined writes; cross-env writes denied."""
        actual = self.classify(account_id)
        if actual != environment:
            return {"ok": False,
                    "error_code": "ENVIRONMENT_MISMATCH",
                    "account_id": account_id,
                    "declared": environment.value,
                    "actual": actual.value}
        return {"ok": True}

    def environment_view(self, env: PortfolioEnvironment) -> dict[str, Any]:
        if env is PortfolioEnvironment.MANUAL:
            return {"ok": True, "environment": env.value,
                    "accounts": self._offline.list_accounts(),
                    "note": "manual/imported — broker_confirmed=false"}
        if env is PortfolioEnvironment.PAPER and self._sim:
            return {"ok": True, "environment": env.value,
                    "simulated": True,
                    "accounts": getattr(self._sim, "accounts", None)
                    and self._sim.accounts.list_accounts() or []}
        if env is PortfolioEnvironment.SHADOW:
            return {"ok": True, "environment": env.value,
                    "simulated": True,
                    "note": "immutable AI signal records only"}
        return {"ok": True, "environment": env.value,
                "accounts": [],
                "note": "LIVE view empty — broker integration disabled"}
