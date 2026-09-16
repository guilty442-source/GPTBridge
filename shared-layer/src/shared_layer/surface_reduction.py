"""Source/API Surface Final Reduction V1.

Consolidates all public API surfaces into a single canonical inventory
and classification model:

    Six-language Final Public Surface Inventory:
        Python      — public module/__all__/application entry
        TypeScript  — export/typed client/IPC operation
        C           — gptbridge_native.h symbols/structs/enums/handles
        C++         — exported symbols/private headers
        C#          — public types/members/interop entry
        SQL         — tables/views/functions/schema contract
        + CLI/config/build commands

    Classification (per public item):
        KEEP_PUBLIC     — has external consumer/cross-module contract/boundary/extension point
        MAKE_PRIVATE    — no external consumer, should be private
        MERGE           — duplicate of another public item
        DEPRECATE       — legacy, should be deprecated with compatibility window
        REMOVE          — CERTAIN_UNUSED or compatibility window expired

    Consumer evidence:
        EXTERNAL_CONSUMER   — used outside its own module
        CROSS_MODULE_CONTRACT — part of a cross-module contract
        CROSS_LANGUAGE_BOUNDARY — crosses language boundary
        STABLE_EXTENSION_POINT — declared extension point
        COMPATIBILITY_REQUIRED — release compatibility requires it
        CERTAIN_UNUSED      — no consumer found (static + dynamic search)
        LEGACY_ALIAS        — legacy alias, compatibility window

Rules:
    - No new language, governance Gate, capability, public API, or abstraction layer.
    - This phase only does public surface reduction and legacy/dead surface cleanup.
    - Every public item must have evidence, else MAKE_PRIVATE/MERGE/DEPRECATE/REMOVE.
    - Python: remove pass-through wrappers and excessive re-exports.
    - TypeScript: no `export *` of internal implementation; raw IPC not UI public API.
    - C ABI: frozen — no new public symbols for private C++ helpers.
    - C++: default hidden; exported set must match C ABI allowlist.
    - pybind11: no second native public API.
    - C#: interop helpers/Win32 mapping/handle impl → internal/private.
    - SQL: application access via Python repository, not raw schema as UI contract.
    - All removal requires static + dynamic consumer evidence search.
    - Only CERTAIN_UNUSED or expired compatibility window items can be removed.

Codex basis:
    A205 — API boundary
    A204/A220 — C ABI boundary (frozen)
    A211 — six-language canonical roles
    A221 — canonical native physical tree
    A208 — release verification
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


SURFACE_REDUCTION_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------

class SurfaceLanguage(str, Enum):
    """Languages with public surfaces."""
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    C = "c"
    CPP = "cpp"
    CSHARP = "csharp"
    SQL = "sql"
    CLI = "cli"        # CLI commands
    CONFIG = "config"  # configuration keys
    BUILD = "build"    # build scripts


# ---------------------------------------------------------------------------
# Surface item types
# ---------------------------------------------------------------------------

class SurfaceItemType(str, Enum):
    """Types of public surface items."""
    # Python
    PY_MODULE = "py_module"
    PY_ALL_EXPORT = "py_all_export"
    PY_ENTRY = "py_entry"
    # TypeScript
    TS_EXPORT = "ts_export"
    TS_TYPED_CLIENT = "ts_typed_client"
    TS_IPC_OPERATION = "ts_ipc_operation"
    # C
    C_SYMBOL = "c_symbol"
    C_STRUCT = "c_struct"
    C_ENUM = "c_enum"
    C_HANDLE = "c_handle"
    # C++
    CPP_EXPORTED_SYMBOL = "cpp_exported_symbol"
    CPP_PRIVATE_HEADER = "cpp_private_header"
    # C#
    CS_PUBLIC_TYPE = "cs_public_type"
    CS_PUBLIC_MEMBER = "cs_public_member"
    CS_INTEROP_ENTRY = "cs_interop_entry"
    # SQL
    SQL_TABLE = "sql_table"
    SQL_VIEW = "sql_view"
    SQL_FUNCTION = "sql_function"
    SQL_SCHEMA_CONTRACT = "sql_schema_contract"
    # CLI/Config/Build
    CLI_COMMAND = "cli_command"
    CONFIG_KEY = "config_key"
    BUILD_SCRIPT = "build_script"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class SurfaceClassification(str, Enum):
    """Classification of public surface items."""
    KEEP_PUBLIC = "KEEP_PUBLIC"          # has evidence, stays public
    MAKE_PRIVATE = "MAKE_PRIVATE"        # no external consumer
    MERGE = "MERGE"                      # duplicate, merge with canonical
    DEPRECATE = "DEPRECATE"              # legacy, deprecate with window
    REMOVE = "REMOVE"                    # CERTAIN_UNUSED or window expired


# ---------------------------------------------------------------------------
# Consumer evidence
# ---------------------------------------------------------------------------

class ConsumerEvidence(str, Enum):
    """Evidence types for public surface items."""
    EXTERNAL_CONSUMER = "EXTERNAL_CONSUMER"
    CROSS_MODULE_CONTRACT = "CROSS_MODULE_CONTRACT"
    CROSS_LANGUAGE_BOUNDARY = "CROSS_LANGUAGE_BOUNDARY"
    STABLE_EXTENSION_POINT = "STABLE_EXTENSION_POINT"
    COMPATIBILITY_REQUIRED = "COMPATIBILITY_REQUIRED"
    CERTAIN_UNUSED = "CERTAIN_UNUSED"
    LEGACY_ALIAS = "LEGACY_ALIAS"
    PASS_THROUGH_WRAPPER = "PASS_THROUGH_WRAPPER"
    EXCESSIVE_RE_EXPORT = "EXCESSIVE_RE_EXPORT"
    DUPLICATE = "DUPLICATE"
    DEAD_SURFACE = "DEAD_SURFACE"


# Evidence that justifies keeping public
KEEP_EVIDENCE: frozenset[ConsumerEvidence] = frozenset({
    ConsumerEvidence.EXTERNAL_CONSUMER,
    ConsumerEvidence.CROSS_MODULE_CONTRACT,
    ConsumerEvidence.CROSS_LANGUAGE_BOUNDARY,
    ConsumerEvidence.STABLE_EXTENSION_POINT,
    ConsumerEvidence.COMPATIBILITY_REQUIRED,
})

# Evidence that requires action
ACTION_EVIDENCE: frozenset[ConsumerEvidence] = frozenset({
    ConsumerEvidence.CERTAIN_UNUSED,
    ConsumerEvidence.LEGACY_ALIAS,
    ConsumerEvidence.PASS_THROUGH_WRAPPER,
    ConsumerEvidence.EXCESSIVE_RE_EXPORT,
    ConsumerEvidence.DUPLICATE,
    ConsumerEvidence.DEAD_SURFACE,
})


# ---------------------------------------------------------------------------
# Surface item
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SurfaceItem:
    """One item in the Final Public Surface Inventory.

    Every public item must have evidence, else it is classified
    MAKE_PRIVATE/MERGE/DEPRECATE/REMOVE.
    """
    language: SurfaceLanguage
    item_type: SurfaceItemType
    name: str               # fully qualified name
    file: str               # source file
    line: int               # line number
    evidence: tuple[ConsumerEvidence, ...] = ()
    consumers: tuple[str, ...] = ()  # modules/files that consume this
    classification: SurfaceClassification = SurfaceClassification.KEEP_PUBLIC
    notes: str = ""


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

def classify_item(item: SurfaceItem) -> SurfaceClassification:
    """Classify a surface item based on its evidence.

    Rules:
        - Has KEEP evidence → KEEP_PUBLIC
        - CERTAIN_UNUSED → REMOVE
        - LEGACY_ALIAS → DEPRECATE
        - PASS_THROUGH_WRAPPER → MAKE_PRIVATE
        - EXCESSIVE_RE_EXPORT → MAKE_PRIVATE
        - DUPLICATE → MERGE
        - DEAD_SURFACE → REMOVE
        - No evidence → MAKE_PRIVATE
    """
    evidence_set = set(item.evidence)

    # If has keep evidence and no action evidence, keep public
    if evidence_set & KEEP_EVIDENCE and not (evidence_set & ACTION_EVIDENCE):
        return SurfaceClassification.KEEP_PUBLIC

    # Action evidence takes priority
    if ConsumerEvidence.CERTAIN_UNUSED in evidence_set:
        return SurfaceClassification.REMOVE
    if ConsumerEvidence.DEAD_SURFACE in evidence_set:
        return SurfaceClassification.REMOVE
    if ConsumerEvidence.LEGACY_ALIAS in evidence_set:
        return SurfaceClassification.DEPRECATE
    if ConsumerEvidence.DUPLICATE in evidence_set:
        return SurfaceClassification.MERGE
    if ConsumerEvidence.PASS_THROUGH_WRAPPER in evidence_set:
        return SurfaceClassification.MAKE_PRIVATE
    if ConsumerEvidence.EXCESSIVE_RE_EXPORT in evidence_set:
        return SurfaceClassification.MAKE_PRIVATE

    # No evidence at all → make private
    if not evidence_set:
        return SurfaceClassification.MAKE_PRIVATE

    # Mixed evidence — keep if any keep evidence
    if evidence_set & KEEP_EVIDENCE:
        return SurfaceClassification.KEEP_PUBLIC

    return SurfaceClassification.MAKE_PRIVATE


def classify_with_evidence(
    item: SurfaceItem,
    evidence: tuple[ConsumerEvidence, ...],
) -> SurfaceClassification:
    """Classify an item with updated evidence."""
    updated = SurfaceItem(
        language=item.language,
        item_type=item.item_type,
        name=item.name,
        file=item.file,
        line=item.line,
        evidence=evidence,
        consumers=item.consumers,
        classification=item.classification,
        notes=item.notes,
    )
    return classify_item(updated)


# ---------------------------------------------------------------------------
# Consumer evidence search
# ---------------------------------------------------------------------------

@dataclass
class ConsumerSearchResult:
    """Result of static + dynamic consumer evidence search."""
    item_name: str
    static_consumers: list[str] = field(default_factory=list)
    dynamic_consumers: list[str] = field(default_factory=list)
    is_certain_unused: bool = False

    @property
    def has_consumer(self) -> bool:
        return bool(self.static_consumers or self.dynamic_consumers)

    @property
    def all_consumers(self) -> list[str]:
        return list(set(self.static_consumers + self.dynamic_consumers))


def search_consumers(
    item_name: str,
    *,
    static_results: list[str] | None = None,
    dynamic_results: list[str] | None = None,
) -> ConsumerSearchResult:
    """Search for consumers of a public item.

    All removal requires static + dynamic consumer evidence search.
    Only CERTAIN_UNUSED (no static AND no dynamic consumer) items can be removed.
    """
    static = static_results or []
    dynamic = dynamic_results or []
    return ConsumerSearchResult(
        item_name=item_name,
        static_consumers=static,
        dynamic_consumers=dynamic,
        is_certain_unused=not static and not dynamic,
    )


# ---------------------------------------------------------------------------
# ABI freeze
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AbiFreezeEntry:
    """C ABI freeze entry.

    The C ABI is frozen — no new public symbols for private C++ helpers.
    Legacy ABI aliases are deprecated/removed per release compatibility.
    """
    symbol: str
    status: str  # "FROZEN" | "DEPRECATED" | "REMOVED"
    deprecated_in: str = ""  # version
    removal_window: str = ""  # compatibility window
    replacement: str = ""


class AbiFreezeRegistry:
    """Registry of frozen C ABI symbols.

    The C ABI is frozen:
        - No new public symbols for private C++ helpers
        - C++ exported set must match C ABI allowlist
        - Legacy aliases deprecated per release compatibility
    """

    def __init__(self) -> None:
        self._entries: dict[str, AbiFreezeEntry] = {}

    def freeze(self, symbol: str) -> None:
        """Freeze a C ABI symbol."""
        self._entries[symbol] = AbiFreezeEntry(
            symbol=symbol, status="FROZEN",
        )

    def deprecate(
        self,
        symbol: str,
        *,
        deprecated_in: str = "",
        removal_window: str = "",
        replacement: str = "",
    ) -> None:
        """Deprecate a legacy C ABI symbol."""
        self._entries[symbol] = AbiFreezeEntry(
            symbol=symbol, status="DEPRECATED",
            deprecated_in=deprecated_in,
            removal_window=removal_window,
            replacement=replacement,
        )

    def remove(self, symbol: str) -> None:
        """Mark a C ABI symbol as removed (compatibility window expired)."""
        self._entries[symbol] = AbiFreezeEntry(
            symbol=symbol, status="REMOVED",
        )

    def get(self, symbol: str) -> AbiFreezeEntry | None:
        return self._entries.get(symbol)

    def is_frozen(self, symbol: str) -> bool:
        entry = self._entries.get(symbol)
        return entry is not None and entry.status == "FROZEN"

    def is_deprecated(self, symbol: str) -> bool:
        entry = self._entries.get(symbol)
        return entry is not None and entry.status == "DEPRECATED"

    def is_removed(self, symbol: str) -> bool:
        entry = self._entries.get(symbol)
        return entry is not None and entry.status == "REMOVED"

    def all_symbols(self) -> list[str]:
        return list(self._entries.keys())

    def frozen_symbols(self) -> list[str]:
        return [
            s for s, e in self._entries.items()
            if e.status == "FROZEN"
        ]

    def deprecated_symbols(self) -> list[str]:
        return [
            s for s, e in self._entries.items()
            if e.status == "DEPRECATED"
        ]

    def can_add_new_symbol(self, symbol: str) -> bool:
        """Check if a new public symbol can be added.

        C ABI is frozen — no new public symbols for private C++ helpers.
        Only new symbols with cross-language boundary evidence can be added.
        """
        # If already exists, it's not new
        if symbol in self._entries:
            return True
        # New symbols require explicit approval (not automatic)
        return False

    def verify_cpp_export_matches_abi(
        self,
        cpp_exports: list[str],
    ) -> list[str]:
        """Verify C++ exported symbols match C ABI allowlist.

        C++ default hidden; exported set must match C ABI allowlist.
        Returns list of violations (exported but not in ABI).
        """
        abi_symbols = set(self.frozen_symbols())
        violations = [
            s for s in cpp_exports
            if s not in abi_symbols and not s.startswith("_")
        ]
        return violations


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DuplicateGroup:
    """A group of duplicate public items."""
    canonical: str           # the canonical (kept) item
    duplicates: tuple[str, ...]  # duplicate items to merge/remove
    language: SurfaceLanguage


class DuplicateDetector:
    """Detect duplicate public surface items.

    Duplicate configuration keys, build scripts, CLI aliases,
    legacy adapters, unused error classes, and duplicate wrappers.
    """

    def __init__(self) -> None:
        self._groups: list[DuplicateGroup] = []

    def add_group(
        self,
        canonical: str,
        duplicates: list[str],
        language: SurfaceLanguage,
    ) -> None:
        """Register a duplicate group."""
        self._groups.append(DuplicateGroup(
            canonical=canonical,
            duplicates=tuple(duplicates),
            language=language,
        ))

    def all_groups(self) -> list[DuplicateGroup]:
        return list(self._groups)

    def duplicates_for(self, name: str) -> list[str]:
        """Get duplicates for a canonical name."""
        for g in self._groups:
            if g.canonical == name:
                return list(g.duplicates)
        return []

    def is_duplicate(self, name: str) -> bool:
        """Check if a name is a duplicate (not canonical)."""
        for g in self._groups:
            if name in g.duplicates:
                return True
        return False

    def canonical_for(self, name: str) -> str | None:
        """Get the canonical name for a duplicate."""
        for g in self._groups:
            if name in g.duplicates:
                return g.canonical
        return None

    def total_duplicates(self) -> int:
        return sum(len(g.duplicates) for g in self._groups)


# ---------------------------------------------------------------------------
# Surface inventory
# ---------------------------------------------------------------------------

class SurfaceInventory:
    """Final Public Surface Inventory for all six languages + CLI/config/build.

    Every public item must have evidence, else classified for action.
    """

    def __init__(self) -> None:
        self._items: dict[str, SurfaceItem] = {}
        self._abi_freeze = AbiFreezeRegistry()
        self._duplicates = DuplicateDetector()

    def add(self, item: SurfaceItem) -> None:
        """Add a surface item to the inventory."""
        self._items[item.name] = item

    def get(self, name: str) -> SurfaceItem | None:
        return self._items.get(name)

    def all_items(self) -> list[SurfaceItem]:
        return list(self._items.values())

    def items_by_language(self, language: SurfaceLanguage) -> list[SurfaceItem]:
        return [i for i in self._items.values() if i.language == language]

    def items_by_classification(
        self,
        classification: SurfaceClassification,
    ) -> list[SurfaceItem]:
        return [
            i for i in self._items.values()
            if i.classification == classification
        ]

    @property
    def abi_freeze(self) -> AbiFreezeRegistry:
        return self._abi_freeze

    @property
    def duplicates(self) -> DuplicateDetector:
        return self._duplicates

    def classify_all(self) -> None:
        """Classify all items based on their evidence."""
        for name, item in self._items.items():
            classification = classify_item(item)
            self._items[name] = SurfaceItem(
                language=item.language,
                item_type=item.item_type,
                name=item.name,
                file=item.file,
                line=item.line,
                evidence=item.evidence,
                consumers=item.consumers,
                classification=classification,
                notes=item.notes,
            )

    def removal_candidates(self) -> list[SurfaceItem]:
        """Get items classified as REMOVE."""
        return self.items_by_classification(SurfaceClassification.REMOVE)

    def deprecate_candidates(self) -> list[SurfaceItem]:
        """Get items classified as DEPRECATE."""
        return self.items_by_classification(SurfaceClassification.DEPRECATE)

    def make_private_candidates(self) -> list[SurfaceItem]:
        """Get items classified as MAKE_PRIVATE."""
        return self.items_by_classification(SurfaceClassification.MAKE_PRIVATE)

    def merge_candidates(self) -> list[SurfaceItem]:
        """Get items classified as MERGE."""
        return self.items_by_classification(SurfaceClassification.MERGE)

    def keep_public_items(self) -> list[SurfaceItem]:
        """Get items classified as KEEP_PUBLIC."""
        return self.items_by_classification(SurfaceClassification.KEEP_PUBLIC)

    def summary(self) -> dict[str, Any]:
        """Generate inventory summary."""
        by_lang: dict[str, int] = {}
        by_class: dict[str, int] = {}
        for item in self._items.values():
            by_lang[item.language.value] = by_lang.get(item.language.value, 0) + 1
            by_class[item.classification.value] = by_class.get(item.classification.value, 0) + 1
        return {
            "total_items": len(self._items),
            "by_language": by_lang,
            "by_classification": by_class,
            "removal_candidates": len(self.removal_candidates()),
            "deprecate_candidates": len(self.deprecate_candidates()),
            "make_private_candidates": len(self.make_private_candidates()),
            "merge_candidates": len(self.merge_candidates()),
            "keep_public": len(self.keep_public_items()),
            "abi_frozen": len(self._abi_freeze.frozen_symbols()),
            "abi_deprecated": len(self._abi_freeze.deprecated_symbols()),
            "total_duplicates": self._duplicates.total_duplicates(),
        }


# ---------------------------------------------------------------------------
# Language-specific rules
# ---------------------------------------------------------------------------

# Python: remove pass-through wrappers and excessive re-exports
PYTHON_CLEANUP_RULES: tuple[str, ...] = (
    "remove_pass_through_wrappers",
    "remove_excessive_re_exports",
    "remove_unused_error_classes",
    "remove_duplicate_wrappers",
)

# TypeScript: no export * of internal implementation; raw IPC not UI public API
TYPESCRIPT_CLEANUP_RULES: tuple[str, ...] = (
    "no_export_star_internal",
    "raw_ipc_not_ui_public",
    "typed_client_only",
)

# C ABI: frozen
C_ABI_RULES: tuple[str, ...] = (
    "abi_frozen",
    "no_new_public_for_private_cpp",
    "legacy_alias_deprecate_per_compatibility",
)

# C++: default hidden, exported matches ABI allowlist
CPP_RULES: tuple[str, ...] = (
    "default_hidden",
    "exported_matches_abi_allowlist",
    "no_pybind11_second_public_api",
)

# C#: interop helpers → internal/private
CSHARP_RULES: tuple[str, ...] = (
    "interop_helper_internal",
    "win32_mapping_internal",
    "handle_implementation_internal",
)

# SQL: application access via Python repository
SQL_RULES: tuple[str, ...] = (
    "application_access_via_python_repository",
    "raw_schema_not_ui_contract",
)


def get_cleanup_rules(language: SurfaceLanguage) -> tuple[str, ...]:
    """Get language-specific cleanup rules."""
    rules_map = {
        SurfaceLanguage.PYTHON: PYTHON_CLEANUP_RULES,
        SurfaceLanguage.TYPESCRIPT: TYPESCRIPT_CLEANUP_RULES,
        SurfaceLanguage.C: C_ABI_RULES,
        SurfaceLanguage.CPP: CPP_RULES,
        SurfaceLanguage.CSHARP: CSHARP_RULES,
        SurfaceLanguage.SQL: SQL_RULES,
    }
    return rules_map.get(language, ())


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_removal_safety(
    item: SurfaceItem,
    search_result: ConsumerSearchResult,
) -> bool:
    """Verify that removing an item is safe.

    All removal requires static + dynamic consumer evidence search.
    Only CERTAIN_UNUSED or expired compatibility window items can be removed.
    """
    if item.classification != SurfaceClassification.REMOVE:
        return False
    # Must be certain unused (no static AND no dynamic consumer)
    return search_result.is_certain_unused


def verify_deprecation_window(
    item: SurfaceItem,
    *,
    deprecated_in: str,
    current_version: str,
    window: str,
) -> bool:
    """Verify that a deprecation window has expired.

    Legacy aliases can be removed only after the compatibility window
    has expired.
    """
    # Simple version comparison (semantic versioning)
    try:
        dep_parts = [int(x) for x in deprecated_in.split(".")]
        cur_parts = [int(x) for x in current_version.split(".")]
        window_int = int(window)
        if len(dep_parts) >= 2 and len(cur_parts) >= 2:
            # Check if enough major versions have passed
            major_diff = cur_parts[0] - dep_parts[0]
            return major_diff >= window_int
    except (ValueError, IndexError):
        pass
    return False


__all__ = [
    "SURFACE_REDUCTION_VERSION",
    "SurfaceLanguage",
    "SurfaceItemType",
    "SurfaceClassification",
    "ConsumerEvidence",
    "KEEP_EVIDENCE",
    "ACTION_EVIDENCE",
    "SurfaceItem",
    "classify_item",
    "classify_with_evidence",
    "ConsumerSearchResult",
    "search_consumers",
    "AbiFreezeEntry",
    "AbiFreezeRegistry",
    "DuplicateGroup",
    "DuplicateDetector",
    "SurfaceInventory",
    "PYTHON_CLEANUP_RULES",
    "TYPESCRIPT_CLEANUP_RULES",
    "C_ABI_RULES",
    "CPP_RULES",
    "CSHARP_RULES",
    "SQL_RULES",
    "get_cleanup_rules",
    "verify_removal_safety",
    "verify_deprecation_window",
]
