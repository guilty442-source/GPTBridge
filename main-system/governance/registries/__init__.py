"""Governance Registries — 子主權父子關係與模組指派註冊表（A334）。

法典依據:
- A334: 子主權的單一父級與主要域、可執行模組的單一管理子主權與
  decision/permission/review 權限及精確執行身分，皆以法典 SQLite
  的 ``*_registry`` 表為唯一機器權威。
- A130/A323/A322: 單一來源、單一父級；執行期不得以手寫映射副本
  覆寫或分歧於正式註冊表。

本模組是執行期唯讀投影：所有父子關係、主要域、模組指派與執行
身分一律經由 ``governance-codex://official`` 的受控 bounded 查找
讀取法典註冊表（A435 BOUNDED_MACHINE_LOOKUP），不維護任何手寫
映射，也不直接開啟 SQLite。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from governance_rule.execution.codex_reconcile import bounded_lookup


# ---------------------------------------------------------------------------
# Codex-backed registry access (A334 machine authority, official entry)
# ---------------------------------------------------------------------------

# A435 bounded-codex-proxy actor for this governed component.
_ACTOR = "governance-registries"


@lru_cache(maxsize=1)
def _registries() -> dict[str, tuple[dict[str, str], ...]]:
    """Load every ``*_registry`` table through the official entry.

    The cache holds only typed non-content registry rows plus metadata —
    permitted by A435 (caches must not carry codex rule text).
    """
    try:
        return bounded_lookup(
            _ACTOR,
            purpose="coordination",
            scope=("registry:*",),
            reader=lambda ctx: {
                name: ctx.registry(name) for name in ctx.registry_names()
            },
        )
    except PermissionError:
        return {}


def _registry(name: str) -> tuple[dict[str, str], ...]:
    return _registries().get(name, ())


def sovereign_hierarchy_registry() -> tuple[dict[str, str], ...]:
    """All sub-sovereign parent/domain assignments (A334)."""
    return _registry("sovereign_hierarchy_registry")


def module_assignment_registry() -> tuple[dict[str, str], ...]:
    """All executable-module managing/authority assignments (A334)."""
    return _registry("module_assignment_registry")


def supersession_registry() -> tuple[dict[str, str], ...]:
    """Retired-identity supersession records."""
    return _registry("supersession_registry")


def _active_hierarchy_rows() -> tuple[dict[str, str], ...]:
    """Active single-parent rows only (abolished identities excluded)."""
    return tuple(
        row
        for row in sovereign_hierarchy_registry()
        if row.get("status") == "active"
    )


# ---------------------------------------------------------------------------
# Hierarchy helpers — projections of the codex machine authority
# ---------------------------------------------------------------------------


def children_of(parent_id: str) -> tuple[str, ...]:
    """返回指定父主權下所有 active 子主權 ID 元組（A334）。"""
    return tuple(
        row.get("child_identity", "")
        for row in _active_hierarchy_rows()
        if row.get("parent_identity") == parent_id
    )


def parent_of(child_id: str) -> str | None:
    """返回指定子主權的唯一 active 父主權 ID（A334 fail-closed 語義）。"""
    for row in _active_hierarchy_rows():
        if row.get("child_identity") == child_id:
            return row.get("parent_identity")
    return None


def primary_domain_of(sovereign_id: str) -> str:
    """返回註冊表登錄的主要管轄域（未登錄時為 ``unknown``）。"""
    for row in _active_hierarchy_rows():
        if row.get("child_identity") == sovereign_id:
            return row.get("primary_domain") or "unknown"
    return "unknown"


def validate_child_parent(child_id: str, expected_parent_id: str) -> bool:
    """A334 fail-closed: 子主權的單一註冊父級必須符合預期。"""
    return parent_of(child_id) == expected_parent_id


def all_children() -> dict[str, tuple[str, ...]]:
    """完整（active）父 -> 子映射，直接由註冊表導出。"""
    result: dict[str, list[str]] = {}
    for row in _active_hierarchy_rows():
        result.setdefault(row.get("parent_identity", ""), []).append(
            row.get("child_identity", "")
        )
    return {parent: tuple(children) for parent, children in result.items()}


def all_parents() -> dict[str, str]:
    """完整（active）子 -> 父映射，直接由註冊表導出。"""
    return {
        row.get("child_identity", ""): row.get("parent_identity", "")
        for row in _active_hierarchy_rows()
    }


# ---------------------------------------------------------------------------
# Module assignment + execution identity (A334)
# ---------------------------------------------------------------------------


def module_assignment(module_architecture_code: str) -> dict[str, str] | None:
    """Resolve an executable module's registered assignment entry."""
    for row in module_assignment_registry():
        if (
            row.get("module_architecture_code") == module_architecture_code
            and row.get("status") == "active"
        ):
            return row
    return None


def validate_execution_identity(
    module_architecture_code: str, execution_identity: str
) -> bool:
    """A334: the presented identity must equal the registered execution identity.

    The caller must present the executing individual's *attested* identity;
    execution gates refuse requests without an attested identity so this
    check never degrades into same-value self-attestation of the requested
    module code (see ``ExecutionBase._attested_execution_identity``).
    """
    row = module_assignment(module_architecture_code)
    return row is not None and row.get("execution_identity") == execution_identity


def successor_of(predecessor_identity: str) -> str | None:
    """Resolve a retired identity to its current superseding identity."""
    for row in supersession_registry():
        if (
            row.get("predecessor_identity") == predecessor_identity
            and row.get("status") == "active"
        ):
            return row.get("successor_identity")
    return None


# Sovereign identities whose app attribute name differs from the raw
# identity (e.g. the non-ASCII 星澄 identity maps to ``xingcheng_sovereign``).
_SOVEREIGN_ATTR_ALIASES = {
    "星澄": "xingcheng_sovereign",
    # Codex renamed synchronization-sovereign to automation-sovereign
    # (97e8a34); the code object keeps the synchronization class/attr name.
    "automation-sovereign": "synchronization_sovereign",
}


def resolve_sovereign(app: Any, sovereign_id: str) -> Any | None:
    """Resolve a sovereign identity to its materialized in-process instance.

    Top-level sovereigns are app attributes named after the identity
    (``decision-sovereign`` -> ``app.decision_sovereign``); sub-sovereigns
    resolve through their registered single parent's child registry (A334).
    Returns ``None`` when the sovereign is not materialized.
    """
    if app is None or not sovereign_id:
        return None
    direct = getattr(
        app,
        _SOVEREIGN_ATTR_ALIASES.get(sovereign_id, sovereign_id.replace("-", "_")),
        None,
    )
    if direct is not None:
        return direct
    parent_id = parent_of(sovereign_id)
    if parent_id is None:
        return None
    parent = getattr(
        app,
        _SOVEREIGN_ATTR_ALIASES.get(parent_id, parent_id.replace("-", "_")),
        None,
    )
    if parent is None:
        return None
    return getattr(parent, "_sub_sovereigns", {}).get(sovereign_id)


def hierarchy_status() -> dict[str, Any]:
    """Read-only registry surface for status/reporting."""
    hierarchy = sovereign_hierarchy_registry()
    assignments = module_assignment_registry()
    supersessions = supersession_registry()
    active = _active_hierarchy_rows()
    return {
        "authority": "codex-A334",
        "hierarchy_entries": len(active),
        "hierarchy_total_entries": len(hierarchy),
        "module_assignments": len(assignments),
        "supersession_entries": len(supersessions),
        "parents": sorted({row.get("parent_identity", "") for row in active}),
    }


__all__ = [
    "children_of",
    "parent_of",
    "primary_domain_of",
    "validate_child_parent",
    "all_children",
    "all_parents",
    "hierarchy_status",
    "module_assignment",
    "module_assignment_registry",
    "resolve_sovereign",
    "sovereign_hierarchy_registry",
    "successor_of",
    "supersession_registry",
    "validate_execution_identity",
]
