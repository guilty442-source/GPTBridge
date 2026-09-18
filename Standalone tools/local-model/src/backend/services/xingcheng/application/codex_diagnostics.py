"""Read-only codex/implementation alignment and mirror diagnostics.

两個診斷指令（僅讀，不修改任何檔案／法典）：
  * ``xingcheng_codex_alignment`` — 法典 vs 實作對齊：
      - architecture registry（法典宣告 == registry == permission routes ==
        module manifests == 實體目錄）
      - formal rules 的 machine-enforced 對應
      - 法典版本／主權／條文摘要
  * ``xingcheng_codex_mirror_check`` — 中文法典鏡像與架構圖同步／完整：
      - check_codex_consistency（版本、身分集合、必要表、文字汙染）
      - check_codex_text_integrity（replacement damage）
      - check_codex_mirror_quality（五段鏡像鏈、hash、品質證據）
      - architecture_document_report（architecture-*.md 與 registry 覆蓋）
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from governance_rule.execution.audit.architecture_docs import (
    architecture_document_report,
)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CodexDiagnosticsMixin:
    """Assistant-facing codex alignment / mirror diagnostics (read-only)."""

    def _governance_project_root(self) -> Path:
        configured = str(
            os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT") or ""
        ).strip()
        if configured:
            return Path(configured).resolve()
        return Path(self.tool_root).resolve().parents[1]

    async def _handle_codex_diagnostics(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_codex_alignment":
            return (
                f"{command}_result",
                await asyncio.to_thread(self._codex_alignment_report),
            )
        if command == "xingcheng_codex_mirror_check":
            return (
                f"{command}_result",
                await asyncio.to_thread(self._codex_mirror_report),
            )
        raise PermissionError("PERMISSION_DENIED")

    # -- alignment: codex vs implementation -----------------------------

    def _codex_alignment_report(self) -> dict[str, Any]:
        root = self._governance_project_root()
        registry_errors: list[str] = []
        formal_errors: list[str] = []
        codex_summary: dict[str, Any] = {}
        fatal: list[str] = []

        try:
            from governance_rule.execution.audit.audit_architecture import (
                check_architecture_registry,
            )

            check_architecture_registry(root, registry_errors)
        except Exception as error:
            fatal.append(f"architecture-registry-check-failed: {error}")

        try:
            from governance_rule.execution.audit.audit_formal_rules import (
                check_formal_rules,
            )

            check_formal_rules(root, formal_errors)
        except Exception as error:
            fatal.append(f"formal-rules-check-failed: {error}")

        try:
            from governance_rule.execution.codex_repository import (
                load_governance_codex,
            )

            codex = load_governance_codex()
            codex_summary = {
                "codex_version": str(codex.codex_version),
                "articles": len(codex.articles),
                "principles": len(codex.principles),
                "edicts": len(codex.edicts),
                "sovereigns": [item.id for item in codex.sovereigns],
            }
        except Exception as error:
            fatal.append(f"codex-load-failed: {error}")

        errors = [*fatal, *registry_errors, *formal_errors]
        return {
            "ok": not errors,
            "checked_at": _iso_now(),
            "project_root": str(root),
            "sections": {
                "architecture_registry": {
                    "ok": not registry_errors and not fatal,
                    "errors": registry_errors,
                },
                "formal_rules": {
                    "ok": not formal_errors,
                    "errors": formal_errors,
                },
                "codex": codex_summary,
            },
            "errors": errors,
        }

    # -- mirror + architecture documents --------------------------------

    def _codex_mirror_report(self) -> dict[str, Any]:
        root = self._governance_project_root()
        codex_root = root / "governance_rule" / "codex"
        errors: list[str] = []
        fatal: list[str] = []
        mirror_summary: dict[str, Any] = {}

        for label, import_path in (
            (
                "consistency",
                "governance_rule.execution.audit.audit_artifacts",
            ),
            (
                "text-integrity",
                "governance_rule.execution.audit.audit_codex_integrity",
            ),
            (
                "mirror-quality",
                "governance_rule.execution.audit.audit_codex_integrity",
            ),
        ):
            try:
                module = __import__(import_path, fromlist=["*"])
                if label == "consistency":
                    module.check_codex_consistency(root, errors)
                elif label == "text-integrity":
                    module.check_codex_text_integrity(root, errors)
                else:
                    module.check_codex_mirror_quality(root, errors)
            except Exception as error:
                fatal.append(f"{label}-check-failed: {error}")

        try:
            from governance_rule.execution.chinese_codex_mirror import (
                load_chinese_codex_parts,
            )

            mirror = load_chinese_codex_parts(codex_root)
            tables = mirror.get("tables") or {}
            mirror_summary = {
                "codex_version": str(mirror.get("codex_version") or ""),
                "part_count": 5,
                "tables": len(tables),
                "rows": sum(len(rows) for rows in tables.values()),
            }
        except Exception as error:
            fatal.append(f"chinese-mirror-unreadable: {error}")

        documents: dict[str, Any]
        try:
            documents = architecture_document_report(root)
        except Exception as error:
            documents = {
                "ok": False,
                "complete": False,
                "errors": [f"architecture-document-report-failed: {error}"],
                "gaps": [],
                "documents": [],
            }

        errors = [*fatal, *errors, *list(documents.get("errors") or ())]
        return {
            "ok": not errors,
            "complete": bool(documents.get("complete")) and not errors,
            "checked_at": _iso_now(),
            "project_root": str(root),
            "mirror": mirror_summary,
            "architecture_documents": {
                "ok": bool(documents.get("ok")),
                "complete": bool(documents.get("complete")),
                "document_count": int(documents.get("document_count") or 0),
                "canonical_components": int(
                    documents.get("canonical_components") or 0
                ),
                "gaps": list(documents.get("gaps") or ()),
                "documents": list(documents.get("documents") or ()),
            },
            "errors": errors,
        }


__all__ = ["CodexDiagnosticsMixin"]
