"""governance — 治理層。

治理權歸治理法典與治理權威，為最高規則層之維護執行主體（P12/A29/E16）。
本層僅守護法典之不可變與完整（A14/A15/A29），一律 fail-closed：
任何無法驗證之狀況直接關閉（A11），不揭露內部細節。
"""

from __future__ import annotations

from .authority import AuthorityGuard, IntegrityVerdict
from .delegation import (
    GovernedExecutorRegistry,
    Delegation,
    GovernedExecutor,
    delegation_registry,
)

__all__ = [
    "AuthorityGuard",
    "IntegrityVerdict",
    "GovernedExecutorRegistry",
    "Delegation",
    "GovernedExecutor",
    "delegation_registry",
]