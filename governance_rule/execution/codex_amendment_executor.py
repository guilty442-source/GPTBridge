"""Governed codex amendment executor (governor-invoked; A382/A488, A104).

Consumes a staged amendment request plus a prepared successor database and
runs the automatic update pipeline (isolate -> change -> wire -> release ->
frontend refresh).

Fail-closed contract:

* ``apply=True`` requires a **unanimous five-sovereign audit result**
  (``AmendmentAuditResult.ok is True`` with a certificate).  This module never
  fabricates receipts, never runs the gate on behalf of a sovereign.
* Seal roots, revision/lineage/certification rows and external signatures
  remain human-governor authority (A104) and are explicitly out of scope; the
  published generation stays ``sealed-pending-external-signatures`` until the
  governor seals it.
* ``apply=False`` (default) rehearses the change in isolation only.

CLI::

    python -m governance_rule.execution.codex_amendment_executor \
        --request main-system/runtime/state/codex-amendment-request-*.json \
        --prepared <successor.sqlite3> --staging <dir> [--apply] \
        [--audit-result <audit.json>]
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from governance_rule.execution.codex_update_pipeline import (
    AutoUpdateResult,
    run_auto_update,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STAGING = PROJECT_ROOT / "main-system" / "runtime" / "temp" / "codex-amendment-stage"


class CodexAmendmentDenied(RuntimeError):
    """Fail-closed denial of an amendment execution attempt."""

    failure_code = "CODEX_AMENDMENT_DENIED"


@dataclass(frozen=True)
class AmendmentExecutionResult:
    ok: bool
    applied: bool
    amendment_id: str
    version: str
    reason: str = ""
    phases: tuple[Mapping[str, Any], ...] = ()
    seal_state: str = "sealed-pending-external-signatures"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applied": self.applied,
            "amendment_id": self.amendment_id,
            "version": self.version,
            "reason": self.reason,
            "phases": [dict(phase) for phase in self.phases],
            "seal_state": self.seal_state,
        }


def _load_request(request_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CodexAmendmentDenied(f"AMENDMENT_REQUEST_UNREADABLE:{error}") from error
    if not isinstance(payload, dict):
        raise CodexAmendmentDenied("AMENDMENT_REQUEST_NOT_AN_OBJECT")
    if not str(payload.get("request_id") or "").strip():
        raise CodexAmendmentDenied("AMENDMENT_REQUEST_ID_REQUIRED")
    return payload


def _load_audit_result(
    audit_result: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if not isinstance(audit_result, Mapping):
        raise CodexAmendmentDenied("FIVE_SOVEREIGN_AUDIT_RESULT_REQUIRED")
    if audit_result.get("ok") is not True:
        raise CodexAmendmentDenied(
            "FIVE_SOVEREIGN_AUDIT_NOT_PASSED:"
            f"{audit_result.get('reason') or 'unknown'}"
        )
    if not audit_result.get("certificate"):
        raise CodexAmendmentDenied("AMENDMENT_CERTIFICATE_REQUIRED")
    return audit_result


def execute_amendment(
    *,
    request_path: str | Path,
    prepared_database: str | Path | None = None,
    audit_result: Mapping[str, Any] | None = None,
    apply: bool = False,
    staging_root: str | Path | None = None,
    codex_root: str | Path | None = None,
) -> AmendmentExecutionResult:
    """Run the governed amendment pipeline; fail closed without the audit gate."""
    request = _load_request(Path(request_path))
    amendment_id = str(request.get("request_id"))
    prepared = Path(prepared_database).resolve() if prepared_database else None
    if prepared is not None and not prepared.is_file():
        raise CodexAmendmentDenied(f"PREPARED_DATABASE_MISSING:{prepared}")
    if apply:
        _load_audit_result(audit_result)

    root = Path(codex_root).resolve() if codex_root else PROJECT_ROOT / "governance_rule" / "codex"
    staging = Path(staging_root).resolve() if staging_root else DEFAULT_STAGING
    result: AutoUpdateResult = run_auto_update(
        root,
        staging,
        prepared_database=prepared,
        apply=apply,
    )
    phases = tuple(
        {"phase": phase.phase, "ok": phase.ok, "detail": phase.detail}
        for phase in result.phases
    )
    reason = "" if result.ok else "update-pipeline-rejected"
    return AmendmentExecutionResult(
        ok=result.ok,
        applied=result.applied,
        amendment_id=amendment_id,
        version=result.version,
        reason=reason,
        phases=phases,
    )


def _read_json(path: str | None) -> Mapping[str, Any] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Governed codex amendment executor.")
    parser.add_argument("--request", required=True)
    parser.add_argument("--prepared", default="")
    parser.add_argument("--staging", default="")
    parser.add_argument("--audit-result", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = execute_amendment(
            request_path=args.request,
            prepared_database=args.prepared or None,
            audit_result=_read_json(args.audit_result),
            apply=bool(args.apply),
            staging_root=args.staging or None,
        )
    except CodexAmendmentDenied as error:
        print(json.dumps({"ok": False, "error_code": error.failure_code, "reason": str(error)}))
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(cli_main())


__all__ = [
    "AmendmentExecutionResult",
    "CodexAmendmentDenied",
    "execute_amendment",
]
