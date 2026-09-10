"""Active certified release pointer — A181/E156 persistence and reset prevention.

Per A181 (certified-hot-update-persistence-and-reset-prevention) and E156
(hot-update-persistence), the active certified release is the **only** baseline
for integrity checks, automatic repair, source repair, startup reconciliation,
watchdog, hot-reload, frontend/backend reload, restart, sync, cache rebuild,
and dependency recovery.

Key invariants enforced by this module:

  * **Single atomic pointer** — a durable, transactional, content-addressed
    pointer to ``release-id + application-version + artifact-root +
    contract-root + certificate-digest + activation-generation``.
  * **Reset prevention** — automatic repair, startup, hot-reload, restart,
    and sync must resolve the pointer first and preserve the active release.
    They must never reset to packaged defaults, Git HEAD, startup snapshots,
    old caches, installers, or prior releases.
  * **Append-only ledger** — every activation and rollback is recorded in an
    append-only activation ledger with from/to/reason/decision-proof/
    certificate/time.
  * **Rollback is not reset** — rollback requires an explicit system decision,
    a new permission-sovereign activation certificate, target validity proof,
    atomic pointer transition, audit, and user notification.

This module is read-only and pure with respect to the codex; it never mutates
the codex and holds no callable enforcement logic.  It provides the data
structures and persistence operations for the active release pointer.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ACTIVE_POINTER_PATH: Final[Path] = (
    _DEFAULT_PROJECT_ROOT / "runtime" / "state"
    / "active-release-pointer.json"
)

_ACTIVATION_LEDGER_PATH: Final[Path] = (
    _DEFAULT_PROJECT_ROOT / "runtime" / "state"
    / "activation-ledger.jsonl"
)

# Public aliases for __all__ export
ACTIVE_POINTER_PATH: Final[Path] = _ACTIVE_POINTER_PATH
ACTIVATION_LEDGER_PATH: Final[Path] = _ACTIVATION_LEDGER_PATH


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActiveReleasePointer:
    """Content-addressed pointer to the current active certified release.

    Per A181: ``ACTIVE-RELEASE-IDENTITY:single-atomic-content-addressed-pointer
    to release-id+application-version+artifact-root+contract-root+
    certificate-digest+activation-generation``.
    """

    release_id: str
    application_version: str
    artifact_root: str
    contract_root: str
    certificate_digest: str
    activation_generation: str
    activated_at: str
    pointer_format: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def content_address(self) -> str:
        """Return a deterministic content-address hash for this pointer."""
        canonical = json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActivationLedgerEntry:
    """Append-only activation ledger record.

    Per A181: ``HISTORY:append-only activation-ledger records-from/to/reason/
    decision-proof/certificate/time``.
    """

    sequence: int
    operation: str  # "activate" | "rollback" | "verify"
    from_release_id: str
    to_release_id: str
    reason: str
    decision_proof: str
    certificate_digest: str
    recorded_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pointer persistence (durable, transactional, restart-safe)
# ---------------------------------------------------------------------------

def _ensure_state_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically so the pointer is crash-safe."""
    _ensure_state_dir(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Append a JSON-Lines entry atomically to the activation ledger."""
    _ensure_state_dir(path)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def publish_active_pointer(
    pointer: ActiveReleasePointer,
    *,
    pointer_path: Path = _ACTIVE_POINTER_PATH,
) -> None:
    """Atomically write the active release pointer (A181: PUBLISH).

    Per A181: ``PUBLISH:A175-atomic-activation writes-artifacts+certificate+
    active-pointer as-one-commit``.  This function writes only the pointer
    portion; the caller is responsible for the atomic commit of artifacts
    and certificate alongside this call.
    """
    _atomic_write_json(pointer_path, pointer.as_dict())


def resolve_active_pointer(
    *,
    pointer_path: Path = _ACTIVE_POINTER_PATH,
) -> ActiveReleasePointer | None:
    """Read the active release pointer before startup/repair/reload/sync.

    Per A181: ``PERSISTENCE:durable+transactional+restart-safe+crash-safe+
    read-before-startup/repair/reload/reconnect/synchronization`` and
    ``STARTUP:resolve-active-release-pointer before-artifact-validation``.

    Returns ``None`` if no pointer exists (first boot or pre-certification).
    """
    if not pointer_path.is_file():
        return None
    try:
        data = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("pointer_format") != 1:
        return None
    required = (
        "release_id",
        "application_version",
        "artifact_root",
        "contract_root",
        "certificate_digest",
        "activation_generation",
        "activated_at",
    )
    if not all(key in data for key in required):
        return None
    return ActiveReleasePointer(**{key: data[key] for key in required})


# ---------------------------------------------------------------------------
# Activation ledger (append-only)
# ---------------------------------------------------------------------------

def _next_ledger_sequence(ledger_path: Path = _ACTIVATION_LEDGER_PATH) -> int:
    """Return the next sequence number for the activation ledger."""
    if not ledger_path.is_file():
        return 1
    count = 0
    try:
        with ledger_path.open("r", encoding="utf-8") as handle:
            for _ in handle:
                count += 1
    except OSError:
        pass
    return count + 1


def record_activation(
    *,
    operation: str,
    from_release_id: str,
    to_release_id: str,
    reason: str,
    decision_proof: str,
    certificate_digest: str,
    ledger_path: Path = _ACTIVATION_LEDGER_PATH,
) -> ActivationLedgerEntry:
    """Append an entry to the activation ledger (A181: HISTORY).

    Per A181: ``HISTORY:append-only activation-ledger records-from/to/reason/
    decision-proof/certificate/time``.
    """
    entry = ActivationLedgerEntry(
        sequence=_next_ledger_sequence(ledger_path),
        operation=operation,
        from_release_id=from_release_id,
        to_release_id=to_release_id,
        reason=reason,
        decision_proof=decision_proof,
        certificate_digest=certificate_digest,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    _atomic_append_jsonl(ledger_path, entry.as_dict())
    return entry


def read_activation_ledger(
    *,
    ledger_path: Path = _ACTIVATION_LEDGER_PATH,
    limit: int | None = None,
) -> list[ActivationLedgerEntry]:
    """Read the activation ledger (newest first when limit is set)."""
    if not ledger_path.is_file():
        return []
    entries: list[ActivationLedgerEntry] = []
    try:
        with ledger_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                entries.append(ActivationLedgerEntry(
                    sequence=data["sequence"],
                    operation=data["operation"],
                    from_release_id=data["from_release_id"],
                    to_release_id=data["to_release_id"],
                    reason=data["reason"],
                    decision_proof=data["decision_proof"],
                    certificate_digest=data["certificate_digest"],
                    recorded_at=data["recorded_at"],
                ))
    except (OSError, json.JSONDecodeError, KeyError):
        return []
    if limit is not None:
        entries = entries[-limit:]
    return entries


# ---------------------------------------------------------------------------
# Verification (A181: VERIFICATION)
# ---------------------------------------------------------------------------

def verify_active_release(
    *,
    artifact_digests: dict[str, str] | None = None,
    pointer_path: Path = _ACTIVE_POINTER_PATH,
) -> dict[str, Any]:
    """Verify the active release pointer and optional artifact digests.

    Per A181: ``VERIFICATION:post-repair/restart/reload release-id+all-roots+
    runtime-generation+frontend-backend-match+stability-window`` and E156:
    ``REPAIR+STARTUP+RELOAD+RESTART+SYNC:resolve-pointer>verify-certificate+
    roots>preserve-release>repair-byte-identical-active-artifacts>verify>
    stability-window``.

    Returns a dict with ``ok``, ``pointer``, ``digest_mismatches``, and
    ``reason``.
    """
    pointer = resolve_active_pointer(pointer_path=pointer_path)
    if pointer is None:
        return {
            "ok": False,
            "pointer": None,
            "digest_mismatches": [],
            "reason": "no-active-release-pointer",
        }
    mismatches: list[str] = []
    if artifact_digests:
        for relative, expected in artifact_digests.items():
            actual = _compute_artifact_digest(relative)
            if actual is None:
                mismatches.append(relative)
            elif actual != expected:
                mismatches.append(relative)
    return {
        "ok": not mismatches,
        "pointer": pointer,
        "digest_mismatches": mismatches,
        "reason": "" if not mismatches else "artifact-digest-mismatch",
    }


def _compute_artifact_digest(relative_path: str) -> str | None:
    """Compute SHA-256 of an artifact relative to the project root."""
    candidate = _DEFAULT_PROJECT_ROOT / relative_path
    if not candidate.is_file():
        return None
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Frontend-backend release match (A181: FRONTEND-BACKEND)
# ---------------------------------------------------------------------------

def frontend_backend_release_match(
    frontend_release_id: str,
    backend_release_id: str,
    *,
    pointer_path: Path = _ACTIVE_POINTER_PATH,
) -> dict[str, Any]:
    """Verify frontend and backend loaded the same active release.

    Per A181: ``FRONTEND-BACKEND:must-load-same-active-release-id+
    certificate-digest+contract-generation before-ready``.
    """
    pointer = resolve_active_pointer(pointer_path=pointer_path)
    active_id = pointer.release_id if pointer else ""
    match = (
        frontend_release_id == backend_release_id
        and (not active_id or frontend_release_id == active_id)
    )
    return {
        "ok": match,
        "frontend_release_id": frontend_release_id,
        "backend_release_id": backend_release_id,
        "active_release_id": active_id,
        "reason": "" if match else "frontend-backend-release-mismatch",
    }


# ---------------------------------------------------------------------------
# Status (for observability and UI)
# ---------------------------------------------------------------------------

def active_release_status(
    *,
    pointer_path: Path = _ACTIVE_POINTER_PATH,
    ledger_path: Path = _ACTIVATION_LEDGER_PATH,
) -> dict[str, Any]:
    """Return the active release status for observability and UI.

    Per A181: ``USER-NOTICE:active-version+operation+preserved-or-rollback-status``
    and ``OBSERVABILITY:uptime+restart-count+...+release-id``.
    """
    pointer = resolve_active_pointer(pointer_path=pointer_path)
    ledger = read_activation_ledger(ledger_path=ledger_path, limit=10)
    last_entry = ledger[-1] if ledger else None
    return {
        "has_active_release": pointer is not None,
        "release_id": pointer.release_id if pointer else "",
        "application_version": pointer.application_version if pointer else "",
        "activation_generation": pointer.activation_generation if pointer else "",
        "certificate_digest": pointer.certificate_digest if pointer else "",
        "activated_at": pointer.activated_at if pointer else "",
        "last_ledger_operation": last_entry.operation if last_entry else "",
        "last_ledger_recorded_at": last_entry.recorded_at if last_entry else "",
        "ledger_entries": len(ledger),
        "authority": "permission-sovereign",
        "basis": "A181/E156",
    }


# ---------------------------------------------------------------------------
# Version namespace separation and mismatch classification (A182/E157)
# ---------------------------------------------------------------------------

# A182: VERSION-NAMESPACES: codex-version + application-version + tool-version +
# release-id + contract-version + runtime-generation are-distinct + typed +
# non-interchangeable.
VERSION_NAMESPACES: Final[tuple[str, ...]] = (
    "codex-version",
    "application-version",
    "tool-version",
    "release-id",
    "contract-version",
    "runtime-generation",
)

# A182: MISMATCH-CLASSIFICATION: unknown | metadata-drift | artifact-drift |
# contract-incompatible | certificate-invalid | release-pointer-invalid.
MISMATCH_CLASSIFICATIONS: Final[tuple[str, ...]] = (
    "unknown",
    "metadata-drift",
    "artifact-drift",
    "contract-incompatible",
    "certificate-invalid",
    "release-pointer-invalid",
)


@dataclass(frozen=True)
class VersionMismatch:
    """A typed version mismatch signal (A182: signal-not-mutation-authority).

    Per A182: ``AUTOMATIC-REPAIR+LEARNING+MODEL+AI-REASONING:may-read-typed-
    version-evidence+may-report-mismatch+may-propose-candidate but-may-not-
    infer/normalize/convert/increment/decrement/select/rewrite/publish/repair/
    version-or-release-identity``.  This data structure is a **signal** only;
    it never carries mutation authority.
    """

    namespace: str
    classification: str
    expected: str
    actual: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_signal_only(self) -> bool:
        """Confirm this mismatch is a signal, not a mutation authority."""
        return True


def classify_version_mismatch(
    namespace: str,
    expected: str,
    actual: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> VersionMismatch:
    """Classify a version mismatch per A182/E157.

    Per A182: ``MISMATCH-CLASSIFICATION:unknown|metadata-drift|artifact-drift|
    contract-incompatible|certificate-invalid|release-pointer-invalid`` and
    ``ON-MISMATCH:freeze-affected-mutation+preserve-active-code+collect-evidence+
    information-layer>maintenance-health-classification>system-decision``.

    This function **classifies** the mismatch; it never repairs, resets, or
    mutates anything.  The caller must route the signal through the
    information layer to the sovereign decision chain.
    """
    if namespace not in VERSION_NAMESPACES:
        classification = "unknown"
    elif namespace == "release-id" or namespace == "runtime-generation":
        classification = "release-pointer-invalid"
    elif namespace == "contract-version":
        classification = "contract-incompatible"
    elif namespace in ("codex-version", "application-version", "tool-version"):
        if expected and actual and expected != actual:
            classification = "metadata-drift"
        else:
            classification = "unknown"
    else:
        classification = "unknown"
    return VersionMismatch(
        namespace=namespace,
        classification=classification,
        expected=expected,
        actual=actual,
        evidence=evidence or {},
    )


def version_mismatch_signal(
    mismatch: VersionMismatch,
) -> dict[str, Any]:
    """Produce an information-layer signal for a version mismatch (A182/E157).

    Per A182: ``ON-MISMATCH:freeze-affected-mutation+preserve-active-code+
    collect-evidence+information-layer>maintenance-health-classification>
    system-decision``.  This function produces the signal payload that must
    be routed through the information layer; it never performs repair, reset,
    or code mutation.

    Per E157: ``VERSION-MISMATCH-RESET:none``.
    """
    return {
        "signal_type": "version-mismatch",
        "authority": "signal-only",
        "basis": "A182/E157",
        "namespace": mismatch.namespace,
        "classification": mismatch.classification,
        "expected": mismatch.expected,
        "actual": mismatch.actual,
        "evidence": mismatch.evidence,
        "action_required": "freeze-mutation+preserve-active-code+route-to-sovereign-decision",
        "repair_reset": False,
        "code_reset": False,
    }


__all__ = [
    "ACTIVE_POINTER_PATH",
    "ACTIVATION_LEDGER_PATH",
    "ActivationLedgerEntry",
    "ActiveReleasePointer",
    "MISMATCH_CLASSIFICATIONS",
    "VERSION_NAMESPACES",
    "VersionMismatch",
    "active_release_status",
    "classify_version_mismatch",
    "frontend_backend_release_match",
    "publish_active_pointer",
    "read_activation_ledger",
    "record_activation",
    "resolve_active_pointer",
    "verify_active_release",
    "version_mismatch_signal",
]
