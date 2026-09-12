"""Data classes for file sorter CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class FileSorterError(Exception):
    """檔案分類規則或路徑錯誤。"""



@dataclass(frozen=True)
class KeywordRule:
    keyword: str
    folder: str
    source: str = "custom"



@dataclass
class OrganizeResult:
    moved_count: int = 0
    unmatched_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rules: list[KeywordRule] = field(default_factory=list)
    transaction_id: str | None = None
    journal_path: str | None = None
    plan: dict[str, Any] | None = None



@dataclass
class KeywordUpsertResult:
    added: list[KeywordRule] = field(default_factory=list)
    updated: list[KeywordRule] = field(default_factory=list)
    unchanged: list[KeywordRule] = field(default_factory=list)



@dataclass(frozen=True)
class LegacyRulesMigration:
    rules: tuple[KeywordRule, ...]
    rejected: tuple[dict[str, Any], ...]
    converted_count: int = 0
