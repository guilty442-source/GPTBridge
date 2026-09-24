"""Codex amendment pipeline driver — intake → successor build → five-sovereign audit.

This module is the missing orchestration between a staged request artifact
and the governor's publication decision:

    staged request file → CodexAmendmentRequestLedger.begin (lineage lock)
        → build_successor (authority export → candidate sqlite + manifest)
        → run_five_sovereign_audit (unanimous receipts + certificate)
        → ready-for-governor

Stop line: this driver never seals, signs or publishes.  The unanimous
audit certificate only advances a request to ``ready-for-governor``;
publication remains governor-invoked through
``codex_amendment_executor --apply`` (A382).

Authority source: the live PostgreSQL codex schema is authoritative
(A173).  When the canonical ``codex/data/governance_codex.sqlite3`` file
is absent the driver exports the PostgreSQL authority into a
non-authoritative scratch copy via ``export_postgresql_codex`` — the same
contract ``isolate_generation`` uses in the update pipeline.

``codex.amend`` channel submissions are notifications only: the staged
request artifact file (``artifact=codex-amendment-request``) is the
authoritative intake, and ``scan_requests`` discovers staged files in the
canonical intake directories.  A channel submission without a staged file
advances nothing.

The five sovereign checks supplied here are deterministic standard-charter
verifications (the same duties every amendment receives).  Xingcheng's
receipt additionally requires a governed web-search callable; when none is
wired the check denies fail-closed (``NETWORK_AUDIT_UNAVAILABLE``) rather
than inventing external evidence.

CLI::

    python -m governance_rule.execution.codex_amendment_driver --scan
    python -m governance_rule.execution.codex_amendment_driver --request <path>
    python -m governance_rule.execution.codex_amendment_driver --all
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from governance_rule.execution.codex_amendment import (
    ACTIVE_SOVEREIGN_REQUESTERS,
    AMENDMENT_FLOW,
    CHANGE_CLASSES,
    REQUIRED_GATE,
    SOVEREIGN_ALIASES,
)
from governance_rule.execution.codex_amendment_audit_gate import (
    build_xingcheng_network_check,
)
from governance_rule.execution.codex_amendment_audit_runner import (
    run_five_sovereign_audit,
)
from governance_rule.execution.codex_amendment_lifecycle import (
    STATE_AUDIT_PASSED,
    STATE_AUDITING,
    STATE_EXECUTED,
    STATE_READY_FOR_GOVERNOR,
    STATE_REJECTED,
    STATE_SUBMITTED,
    STATE_SUCCESSOR_BUILT,
    STATE_UNDER_REVIEW,
    STATE_WITHDRAWN,
    TERMINAL_STATES,
    AmendmentLifecycleError,
    CodexAmendmentRequestLedger,
    load_amendment_request,
)
from governance_rule.execution.codex_successor_builder import build_successor

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CANONICAL_CODEX_ROOT: Final[Path] = PROJECT_ROOT / "governance_rule" / "codex"
CANONICAL_DATABASE: Final[Path] = (
    CANONICAL_CODEX_ROOT / "data" / "governance_codex.sqlite3"
)
STATE_DIR: Final[Path] = PROJECT_ROOT / "main-system" / "runtime" / "state"
CONVERGENCE_DIR: Final[Path] = (
    PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "convergence"
)
INTAKE_GLOB: Final[str] = "codex-amendment-request-*.json"
INTAKE_DIRS: Final[tuple[Path, ...]] = (STATE_DIR, CONVERGENCE_DIR)
EXPORT_DIR: Final[Path] = (
    PROJECT_ROOT / "main-system" / "runtime" / "temp" / "codex-amendment-intake"
)
CANDIDATES_DIRNAME: Final[str] = "candidates"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_table_names(database: Path) -> frozenset[str] | None:
    """Table names of a readable sqlite database; None when unreadable."""
    try:
        connection = sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro", uri=True
        )
    except (OSError, sqlite3.Error):
        return None
    try:
        return frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        )
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _authority_source_database() -> Path:
    """Non-authoritative working copy of the live codex authority."""
    if CANONICAL_DATABASE.is_file():
        return CANONICAL_DATABASE
    from governance_rule.execution.codex_postgresql import (
        export_postgresql_codex,
    )

    target = EXPORT_DIR / "authority-export.sqlite3"
    if target.exists():
        # export_postgresql_codex creates tables verbatim; a stale export
        # must never masquerade as the live authority, so refresh it.
        target.unlink()
    return export_postgresql_codex(target)


def _live_authority_identity() -> tuple[str | None, int | None]:
    """Live codex version + revision sequence for lineage staleness checks."""
    try:
        from governance_rule.execution.codex_postgresql import authority_state

        state = authority_state()
    except Exception:
        return None, None
    version = str(
        state.get("codex_version") or state.get("version") or ""
    ).strip() or None
    sequence = state.get("revision_sequence")
    try:
        sequence = int(sequence) if sequence is not None else None
    except (TypeError, ValueError):
        sequence = None
    return version, sequence


def _lineage_conflicts(
    ledger: CodexAmendmentRequestLedger, request: Any
) -> list[str]:
    """Other non-terminal requests holding the same predecessor lineage."""
    conflicts: list[str] = []
    if not ledger.records_dir.is_dir():
        return conflicts
    for path in sorted(ledger.records_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, Mapping):
            continue
        if str(record.get("request_id") or "") == request.request_id:
            continue
        if str(record.get("state") or "") in TERMINAL_STATES:
            continue
        if str(record.get("lineage_key") or "") == request.lineage_key:
            conflicts.append(str(record.get("request_id") or path.stem))
    return conflicts


def _scope_tables(request: Any) -> list[str]:
    tables: list[str] = []
    for entry in request.scope:
        for prefix in ("table:", "registry:"):
            if entry.startswith(prefix):
                name = entry.split(":", 1)[1].strip()
                if name and name not in tables:
                    tables.append(name)
    return tables


def _decision_check(
    request_path: Path, ledger: CodexAmendmentRequestLedger
) -> Callable[[], Mapping[str, Any]]:
    def check() -> Mapping[str, Any]:
        request = load_amendment_request(request_path)
        payload = request.payload
        change_class = str(payload.get("change_class") or "").strip()
        class_ok = change_class in CHANGE_CLASSES
        predecessor = request.predecessor
        basis = {
            "codex_version": str(predecessor.get("codex_version") or ""),
            "history_head": str(predecessor.get("history_head") or ""),
            "revision_sequence": predecessor.get("revision_sequence"),
        }
        basis_ok = bool(basis["codex_version"]) and bool(
            basis["history_head"]
        )
        conflicts = _lineage_conflicts(ledger, request)
        ok = class_ok and basis_ok and not conflicts
        findings = [
            f"change_class:{change_class or 'missing'}",
            f"basis:{'complete' if basis_ok else 'incomplete'}",
            (
                f"lineage:{'unique' if not conflicts else 'conflict:'}"
                + ("" if not conflicts else ",".join(conflicts))
            ),
        ]
        return {
            "ok": ok,
            "method": "decision-basis-verification",
            "findings": findings,
            "evidence": {
                "amendment_class": change_class,
                "basis_references": basis,
                "successor_scope_unique": (
                    "unique-lineage"
                    if not conflicts
                    else "conflict:" + ",".join(conflicts)
                ),
            },
            "error": "" if ok else "DECISION_BASIS_FAILED",
        }

    return check


def _permission_check(
    request_path: Path, source_database: Path | None
) -> Callable[[], Mapping[str, Any]]:
    def check() -> Mapping[str, Any]:
        request = load_amendment_request(request_path)
        payload = request.payload
        raw_requester = str(payload.get("requested_by") or "").strip()
        requester = SOVEREIGN_ALIASES.get(raw_requester, raw_requester)
        roster_ok = requester in ACTIVE_SOVEREIGN_REQUESTERS
        review = str(payload.get("required_review") or "").strip()
        review_ok = review == REQUIRED_GATE
        scope_tables = _scope_tables(request)
        missing: list[str] = []
        unverifiable = False
        if scope_tables:
            names = (
                _sqlite_table_names(source_database)
                if source_database is not None
                else None
            )
            if names is None:
                unverifiable = True
            else:
                missing = [name for name in scope_tables if name not in names]
        ok = roster_ok and review_ok and not missing and not unverifiable
        findings = [
            f"requester:{requester or 'missing'}:{'roster' if roster_ok else 'not-in-roster'}",
            f"required_review:{review or 'missing'}",
            f"scope_tables:{len(scope_tables)}",
        ]
        if missing:
            findings.append("unresolvable:" + ",".join(missing))
        if unverifiable:
            findings.append("scope-tables-unverifiable")
        return {
            "ok": ok,
            "method": "directory-parity-diff",
            "findings": findings,
            "evidence": {
                "directory_rows": {
                    "requested_by": requester,
                    "scope_tables": scope_tables,
                    "unresolvable": missing,
                },
                "identity_lifecycle_parity": (
                    "requester-active" if roster_ok else "requester-not-in-roster"
                ),
                "testflow_references": review,
            },
            "error": "" if ok else "DIRECTORY_PARITY_FAILED",
        }

    return check


def _runtime_check(
    request_path: Path, source_database: Path | None
) -> Callable[[], Mapping[str, Any]]:
    def check() -> Mapping[str, Any]:
        request = load_amendment_request(request_path)
        payload = request.payload
        flow = str(payload.get("flow") or "").strip()
        flow_ok = flow == AMENDMENT_FLOW
        started = time.perf_counter()
        names = (
            _sqlite_table_names(source_database)
            if source_database is not None
            else None
        )
        read_ms = int((time.perf_counter() - started) * 1000)
        live_ok = names is not None and "metadata" in names
        ok = flow_ok and live_ok
        findings = [
            f"flow:{flow or 'missing'}",
            f"predecessor_readable:{live_ok}",
            f"predecessor_read_ms:{read_ms}",
        ]
        return {
            "ok": ok,
            "method": "reader-generation-plan",
            "findings": findings,
            "evidence": {
                "reader_generation_plan": flow,
                "channel_continuity": (
                    "predecessor-readable-during-staging"
                    if live_ok
                    else "predecessor-unreadable"
                ),
                "health_window": {"predecessor_read_ms": read_ms},
            },
            "error": "" if ok else "RUNTIME_CONTINUITY_FAILED",
        }

    return check


def _automation_check(
    manifest_path: Path | None,
    candidate_path: Path | None,
    ledger: CodexAmendmentRequestLedger,
) -> Callable[[], Mapping[str, Any]]:
    def check() -> Mapping[str, Any]:
        if (
            manifest_path is None
            or candidate_path is None
            or not Path(manifest_path).is_file()
            or not Path(candidate_path).is_file()
        ):
            return {
                "ok": False,
                "method": "staging-seal-mirror-mechanics",
                "error": "STAGED_CANDIDATE_MISSING",
                "evidence": {
                    "staging_isolation": "",
                    "seal_roots": "",
                    "mirror_chain": "",
                    "version_identity": "",
                    "rollback_pointer": "",
                },
            }
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        candidate = Path(candidate_path)
        manifest_path_real = Path(manifest_path)
        ledger_root = ledger.root
        try:
            isolation_ok = (
                candidate.resolve().is_relative_to(ledger_root)
                and not candidate.resolve().is_relative_to(
                    CANONICAL_CODEX_ROOT
                )
            )
        except OSError:
            isolation_ok = False
        digest = _sha256(candidate)
        chain_ok = digest == str(manifest.get("candidate_sha256") or "")
        seal = manifest.get("seal_preview")
        seal_ok = isinstance(seal, Mapping) and bool(seal)
        version = str(manifest.get("successor_version") or "").strip()
        if not version:
            names_connection = sqlite3.connect(
                f"file:{candidate.as_posix()}?mode=ro", uri=True
            )
            try:
                row = names_connection.execute(
                    "SELECT value FROM metadata WHERE key='codex_version'"
                ).fetchone()
                version = str(row[0]).strip() if row else ""
            finally:
                names_connection.close()
        predecessor_version = str(
            (manifest.get("predecessor") or {}).get("codex_version") or ""
        ).strip()
        version_ok = bool(version) and version != predecessor_version
        rollback_ok = bool(predecessor_version)
        ok = isolation_ok and chain_ok and seal_ok and version_ok and rollback_ok
        findings = [
            f"candidate:{candidate.name}",
            f"sha256:{'match' if chain_ok else 'mismatch'}",
            f"seal_preview:{'present' if seal_ok else 'missing'}",
            f"version:{version or 'missing'}",
        ]
        return {
            "ok": ok,
            "method": "staging-seal-mirror-mechanics",
            "findings": findings,
            "evidence": {
                "staging_isolation": (
                    f"{ledger_root.name}/{CANDIDATES_DIRNAME}"
                    if isolation_ok
                    else "outside-ledger-root"
                ),
                "seal_roots": "seal-preview-present" if seal_ok else "",
                "mirror_chain": "sha256-verified" if chain_ok else "",
                "version_identity": version,
                "rollback_pointer": predecessor_version,
            },
            "error": "" if ok else "STAGING_MECHANICS_FAILED",
        }

    return check


def _manifest_path_for(
    ledger: CodexAmendmentRequestLedger, request_id: str
) -> Path | None:
    """Locate the candidate manifest recorded by the build stage."""
    record = ledger.load_record(request_id) or {}
    for entry in reversed(record.get("history") or ()):
        if not isinstance(entry, Mapping):
            continue
        evidence = entry.get("evidence")
        if isinstance(evidence, Mapping) and evidence.get("manifest_path"):
            path = Path(str(evidence["manifest_path"]))
            if path.is_file():
                return path
    fallback = (
        ledger.root
        / CANDIDATES_DIRNAME
        / f"{request_id}.candidate-manifest.json"
    )
    return fallback if fallback.is_file() else None


def xingcheng_governed_search(
    request_client: Any,
    *,
    tool_id: str = "local-model",
    command: str = "xingcheng_web_search",
    timeout_seconds: float = 30.0,
) -> Callable[[str], Mapping[str, Any]]:
    """Synchronous governed-channel search callable for the audit gate.

    Wraps ``shared_layer.request_client.GovernedRequestClient`` (or any
    object exposing the same ``request_sync`` contract) so the Xingcheng
    audit receipt's external-evidence check travels the governed channel
    to the running ``local-model`` tool — never a direct socket or HTTP
    client (A177).  The callable is synchronous and may block up to
    ``timeout_seconds``; callers should run ``advance_*`` from a worker
    thread when invoked inside a live event loop.
    """

    def search(query: str) -> Mapping[str, Any]:
        try:
            response = request_client.request_sync(
                tool_id,
                command,
                {"query": query},
                timeout_seconds=timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 — fail closed, type only
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": f"{type(error).__name__}: {error}",
            }
        if not isinstance(response, Mapping):
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": "NON_MAPPING_RESPONSE",
            }
        return response

    return search


def _default_queries(request_path: Path) -> list[str]:
    try:
        request = load_amendment_request(request_path)
    except AmendmentLifecycleError:
        return [str(request_path)]
    payload = request.payload
    queries = [request.request_id]
    titles: list[str] = []
    delta = payload.get("proposed_delta")
    if isinstance(delta, Mapping):
        successors = delta.get("successors")
        if isinstance(successors, Sequence) and not isinstance(
            successors, (str, bytes)
        ):
            for item in successors:
                if isinstance(item, Mapping) and item.get("working_title"):
                    titles.append(str(item["working_title"]))
    queries.extend(titles[:3])
    return queries


def default_sovereign_checks(
    request_path: str | Path,
    *,
    ledger: CodexAmendmentRequestLedger,
    manifest_path: str | Path | None = None,
    candidate_path: str | Path | None = None,
    source_database: str | Path | None = None,
    search: Callable[[str], Any] | None = None,
    queries: Sequence[str] | None = None,
) -> dict[str, Callable[[], Mapping[str, Any]]]:
    """The standard five-sovereign check callables for one request.

    The four governance checks are deterministic verifications of the
    staged request/candidate.  Xingcheng's check requires a governed
    web-search callable; ``search=None`` denies that receipt fail-closed.
    """
    request_path = Path(request_path)
    source = Path(source_database) if source_database else None
    if manifest_path is None:
        request_id = ""
        try:
            request_id = load_amendment_request(request_path).request_id
        except AmendmentLifecycleError:
            pass
        manifest = _manifest_path_for(ledger, request_id) if request_id else None
    else:
        manifest = Path(manifest_path)
    candidate = (
        Path(candidate_path)
        if candidate_path
        else (
            manifest.parent / f"{manifest.stem.replace('.candidate-manifest', '')}.sqlite3"
            if manifest is not None
            else None
        )
    )
    if candidate is not None and not str(candidate).endswith(".sqlite3"):
        candidate = None
    resolved_queries = (
        [str(query) for query in queries]
        if queries
        else _default_queries(request_path)
    )
    return {
        "decision-sovereign": _decision_check(request_path, ledger),
        "permission-sovereign": _permission_check(request_path, source),
        "system-runtime-sovereign": _runtime_check(request_path, source),
        "automation-sovereign": _automation_check(manifest, candidate, ledger),
        "xingcheng": build_xingcheng_network_check(search, resolved_queries),
    }


def scan_requests(
    intake_dirs: Sequence[str | Path] | None = None,
    *,
    ledger: CodexAmendmentRequestLedger | None = None,
) -> list[dict[str, Any]]:
    """Discover staged request artifacts and their ledger states."""
    ledger = ledger or CodexAmendmentRequestLedger()
    directories = (
        tuple(Path(d) for d in intake_dirs) if intake_dirs else INTAKE_DIRS
    )
    found: dict[str, dict[str, Any]] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(INTAKE_GLOB)):
            try:
                request = load_amendment_request(path)
            except AmendmentLifecycleError as error:
                found[str(path)] = {
                    "path": str(path),
                    "request_id": path.stem,
                    "valid": False,
                    "error": str(error),
                    "state": "",
                }
                continue
            record = ledger.load_record(request.request_id)
            found[request.request_id] = {
                "path": str(path),
                "request_id": request.request_id,
                "valid": True,
                "state": str(record.get("state") or "") if record else "",
                "scope": list(request.scope),
            }
    return sorted(found.values(), key=lambda item: item["request_id"])


async def advance_request(
    request_path: str | Path,
    *,
    ledger: CodexAmendmentRequestLedger | None = None,
    source_database: str | Path | None = None,
    search: Callable[[str], Any] | None = None,
    successor_version: str | None = None,
    gate: Any = None,
) -> dict[str, Any]:
    """Advance one staged request through build and audit.

    Stops at ``ready-for-governor`` — publication stays governor-invoked.
    """
    ledger = ledger or CodexAmendmentRequestLedger()
    request_path = Path(request_path)
    result: dict[str, Any] = {"request_path": str(request_path)}
    try:
        request = load_amendment_request(request_path)
    except AmendmentLifecycleError as error:
        result.update(
            ok=False, stage="intake", state=STATE_REJECTED, error=str(error)
        )
        return result
    request_id = request.request_id
    result["request_id"] = request_id
    record = ledger.load_record(request_id)
    state = str(record.get("state") or "") if record else ""
    if state in TERMINAL_STATES:
        result.update(
            ok=state == STATE_EXECUTED,
            stage="terminal",
            state=state,
        )
        return result
    if state == STATE_AUDITING:
        # A certificate can only exist after audit-passed; an ``auditing``
        # record is a crashed cycle — rewind so the gate can re-run.
        ledger.transition(
            request_id,
            STATE_SUCCESSOR_BUILT,
            evidence={"recovery": "auditing-rewind"},
        )
        state = STATE_SUCCESSOR_BUILT
    if state in {"", STATE_SUBMITTED, STATE_UNDER_REVIEW}:
        caller_supplied_source = source_database is not None
        if source_database is None:
            try:
                source_database = _authority_source_database()
            except Exception as error:
                result.update(
                    ok=False,
                    stage="export",
                    state=state or STATE_SUBMITTED,
                    error=f"AUTHORITY_EXPORT_FAILED:{type(error).__name__}",
                )
                return result
        source = Path(source_database)
        candidates = ledger.root / CANDIDATES_DIRNAME
        candidate = candidates / f"{request_id}.sqlite3"
        # Staleness is only checked when the driver derived the authority
        # itself; a caller-supplied source is validated as-is.
        if caller_supplied_source:
            current_version, revision_sequence = None, None
        else:
            current_version, revision_sequence = _live_authority_identity()
        if not successor_version:
            # A candidate must carry a version distinct from its
            # predecessor or the staging-mechanics check denies it;
            # the version axis stamps the next authoritative UTC second.
            successor_version = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if successor_version == str(
                request.predecessor.get("codex_version") or ""
            ):
                successor_version = (
                    datetime.now(timezone.utc) + timedelta(seconds=1)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
        build = build_successor(
            request_path,
            source,
            candidate,
            ledger=ledger,
            successor_version=successor_version,
            expected_current_version=current_version,
            expected_revision_sequence=revision_sequence,
        )
        result["build"] = {
            "ok": build.ok,
            "candidate_path": build.output_database,
            "manifest_path": build.manifest_path,
            "applied": list(build.applied),
            "deferred": len(build.deferred),
            "errors": list(build.errors),
        }
        if not build.ok:
            result.update(ok=False, stage="build", state=STATE_REJECTED)
            return result
        state = STATE_SUCCESSOR_BUILT
        source_database = source
    if state == STATE_SUCCESSOR_BUILT:
        source = source_database
        if source is None:
            try:
                source = _authority_source_database()
            except Exception:
                source = None
        if source is None:
            # Same deferral contract as the search callable below: a
            # transient authority-export failure must NOT reach the gate
            # as a permanent sovereign denial (observed 2026-09-24:
            # export error inside the intake task -> checks denied on
            # missing evidence -> request rejected).
            result.update(
                ok=False,
                stage="audit",
                state=STATE_SUCCESSOR_BUILT,
                error="AUTHORITY_EXPORT_UNAVAILABLE",
            )
            return result
        if search is None:
            # Xingcheng's receipt requires the governed web-search path.
            # Running the gate without it would permanently REJECT the
            # request for a transient tool outage — defer instead: the
            # request stays ``successor-built`` until the search callable
            # is wired (fail-closed, non-destructive).
            result.update(
                ok=False,
                stage="audit",
                state=STATE_SUCCESSOR_BUILT,
                error="XINGCHENG_SEARCH_UNAVAILABLE",
            )
            return result
        manifest = _manifest_path_for(ledger, request_id)
        checks = default_sovereign_checks(
            request_path,
            ledger=ledger,
            manifest_path=manifest,
            source_database=source,
            search=search,
        )
        run = await run_five_sovereign_audit(
            request_path,
            checks,
            ledger=ledger,
            gate=gate,
            requester=str(request.payload.get("requested_by") or ""),
        )
        result["audit"] = run.as_dict()
        result.update(ok=run.ok, stage="audit", state=run.state)
        return result
    # audit-passed or ready-for-governor: nothing left for the driver.
    result.update(ok=True, stage="audit", state=state)
    return result


async def advance_all(
    *,
    intake_dirs: Sequence[str | Path] | None = None,
    ledger: CodexAmendmentRequestLedger | None = None,
    source_database: str | Path | None = None,
    search: Callable[[str], Any] | None = None,
    successor_version: str | None = None,
    gate: Any = None,
) -> list[dict[str, Any]]:
    """Advance every staged non-terminal request once."""
    ledger = ledger or CodexAmendmentRequestLedger()
    results: list[dict[str, Any]] = []
    for item in scan_requests(intake_dirs, ledger=ledger):
        if not item.get("valid"):
            results.append(
                {
                    "request_id": item.get("request_id"),
                    "ok": False,
                    "stage": "intake",
                    "state": "",
                    "error": item.get("error"),
                }
            )
            continue
        if item.get("state") in TERMINAL_STATES:
            continue
        results.append(
            await advance_request(
                item["path"],
                ledger=ledger,
                source_database=source_database,
                search=search,
                successor_version=successor_version,
                gate=gate,
            )
        )
    return results


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Advance staged codex amendment requests to ready-for-governor."
    )
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--request", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--source", default="", help="predecessor sqlite export")
    args = parser.parse_args(argv)
    if args.scan:
        print(json.dumps(scan_requests(), ensure_ascii=False, indent=2))
        return 0
    if args.request:
        outcome = asyncio.run(
            advance_request(
                args.request, source_database=args.source or None
            )
        )
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 0 if outcome.get("ok") else 1
    if args.all:
        outcomes = asyncio.run(
            advance_all(source_database=args.source or None)
        )
        print(json.dumps(outcomes, ensure_ascii=False, indent=2))
        return 0 if all(item.get("ok") for item in outcomes) else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(cli_main())


__all__ = [
    "CANONICAL_DATABASE",
    "CANONICAL_CODEX_ROOT",
    "INTAKE_DIRS",
    "INTAKE_GLOB",
    "advance_all",
    "advance_request",
    "cli_main",
    "default_sovereign_checks",
    "scan_requests",
    "xingcheng_governed_search",
]
