"""Architecture document sync and completeness diagnostics (A537/A538).

The codex owns the machine-readable topology (``architecture_registry.json``)
and the Chinese codex mirror carries ``architecture-*.md`` diagrams.  A537/
A538 declare that every successor generation synchronizes the architecture
artifacts atomically, but no validator existed for the documents themselves.

This module is the read-only diagnostic half.  It reports, without mutating
anything,

  * document defects (unreadable, missing title, missing mermaid diagram,
    stale registry identifiers referenced by a document),
  * expected per-tool documents (``architecture-tool-<id>.md``) that are
    missing for top-level canonical tool components, and
  * (informational) canonical components that no document references yet.

It is intentionally NOT part of the hard governance audit yet: the current
document set predates the registry, so findings are reported for the
governor/assistant to act on rather than blocking the audit.  Wire it into
``audit_checks`` once the document set is complete.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .architecture_registry import load_registry, registry_path

ARCHITECTURE_DOC_GLOB = "architecture-*.md"
TOOL_DOC_PREFIX = "architecture-tool-"

#: Backticked identifiers that look like registry component ids.
_IDENTIFIER_PATTERN = re.compile(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`")


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def architecture_document_report(root: str | Path) -> dict[str, Any]:
    """Read-only sync/completeness report for the codex architecture docs."""
    project_root = Path(root).resolve()
    audit_dir = project_root / "governance_rule" / "execution" / "audit"
    codex_root = project_root / "governance_rule" / "codex"

    try:
        registry = load_registry(registry_path(audit_dir))
    except Exception as error:  # fail closed, but keep the report shape
        return {
            "ok": False,
            "complete": False,
            "checked_at": _iso_now(),
            "project_root": str(project_root),
            "errors": [f"architecture registry unreadable: {error}"],
            "gaps": [],
            "unreferenced_canonical": [],
            "documents": [],
        }

    components = [
        component
        for component in registry.get("components", [])
        if isinstance(component, dict)
    ]
    component_ids = sorted(
        identifier
        for identifier in (
            str(component.get("component_id") or "") for component in components
        )
        if identifier
    )
    canonical = [
        component
        for component in components
        if component.get("canonical") is True
        and str(component.get("component_id") or "").strip()
    ]

    errors: list[str] = []
    gaps: list[dict[str, str]] = []
    documents: list[dict[str, Any]] = []
    referenced_any: set[str] = set()

    if not codex_root.is_dir():
        errors.append(f"codex root missing: {codex_root}")

    doc_paths = sorted(codex_root.glob(ARCHITECTURE_DOC_GLOB))
    for path in doc_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            errors.append(f"{path.name}: unreadable ({error})")
            continue
        title = _first_heading(text)
        has_mermaid = "```mermaid" in text
        referenced = sorted(
            identifier for identifier in component_ids if identifier in text
        )
        referenced_any.update(referenced)
        stale = sorted(
            identifier
            for identifier in set(_IDENTIFIER_PATTERN.findall(text))
            if identifier not in component_ids
            and identifier not in {"model-dialogue", "xingcheng"}
            and not identifier.startswith(("governance-", "gptbridge-"))
        )
        if not title:
            errors.append(f"{path.name}: missing document title")
        if not has_mermaid:
            errors.append(f"{path.name}: missing mermaid diagram block")
        documents.append(
            {
                "name": path.name,
                "title": title,
                "mermaid": has_mermaid,
                "bytes": len(text.encode("utf-8")),
                "referenced_components": referenced,
                "stale_identifiers": stale,
            }
        )

    # Completeness: every top-level canonical tool owns an architecture doc.
    doc_names = {path.name for path in doc_paths}
    for component in canonical:
        identifier = str(component.get("component_id") or "")
        physical = str(component.get("physical_path") or "")
        parts = physical.split("/")
        top_level_tool = (
            len(parts) == 2
            and parts[0] == "Standalone tools"
            and parts[1] == identifier
        )
        if not top_level_tool:
            continue
        expected = f"{TOOL_DOC_PREFIX}{identifier}.md"
        if expected not in doc_names:
            gaps.append(
                {
                    "component_id": identifier,
                    "physical_path": physical,
                    "reason": f"missing expected document {expected}",
                }
            )

    unreferenced = [
        str(component.get("component_id") or "")
        for component in canonical
        if str(component.get("component_id") or "") not in referenced_any
    ]

    return {
        "ok": not errors,
        "complete": not gaps and not errors,
        "checked_at": _iso_now(),
        "project_root": str(project_root),
        "document_count": len(documents),
        "registry_components": len(components),
        "canonical_components": len(canonical),
        "referenced_canonical": sorted(referenced_any),
        "unreferenced_canonical": unreferenced,
        "documents": documents,
        "errors": errors,
        "gaps": gaps,
    }


__all__ = ["ARCHITECTURE_DOC_GLOB", "architecture_document_report"]
