"""Git governance version manifest — loader, integrity and write guard.

A318/A319/A320: the git governance plane has exactly one version source.
``git_governance_manifest.json`` (same directory) declares the governance
version, every schema version the plane reads or writes, the supported Git
executable range and the governed timing defaults.  Its SHA-256 digest is
computed over the *canonical* JSON of the payload (sorted keys, compact
separators, the ``integrity`` field itself and the ``baseline`` binding
excluded — the same construction the audit chain uses for ``record_hash``)
and is stored twice:

  * inside the manifest as ``integrity.digest`` (canonical field), and
  * beside it as ``git_governance_manifest.sha256`` (sidecar).

The loader verifies the manifest *before* anything is read from it.  When
the manifest is missing, malformed, tampered with or declares a version the
runtime does not support it fails closed:

    UPGRADE_REQUIRED  manifest absent or older than this runtime
    READ_ONLY         present but unreadable / tampered / unsupported

``assert_write_allowed()`` is the guard write entry points (self-commit,
coordinator, automation supervisor, workspace synchronizer, merge queue)
call; it is a no-op while the manifest verifies as ACTIVE, so normal
operation is unchanged.

The ``baseline`` section is a *binding*, not part of the manifest identity:
it registers the golden Git baseline digest inside the manifest.  Because
the baseline itself records the manifest digest it was captured against
(``baseline.governance_manifest.digest``), leaving the binding inside the
canonical payload would create a digest cycle.  ``canonical_payload``
therefore excludes both ``integrity`` and ``baseline``: (re)binding the
baseline never moves the manifest digest, so the digest recorded in the
baseline stays verifiable after every bind (see
``baseline.bind_to_governance_manifest``).

Hardcoded-value inventory (spec §318-320 scan of ``git_tiers``): the values
below were found hardcoded and are now manifest-bound (module -> key):

    git_repository.py      DEFAULT_TIMEOUT, _SNAPSHOT_TTL
    git_cache.py           DEFAULT_TTL
    claims.py              DEFAULT_LEASE_SECONDS
    git_control_plane.py   DEFAULT_PROPOSAL_TTL_SECONDS,
                           DEFAULT_LOCK_STALL_SECONDS
    self_commit.py         watch interval / debounce / adaptive bounds
    commit_policy.py       CommitBatchPolicy defaults
    worker_pool_types.py   PoolConfig lease/retention/backpressure
    automation_supervisor(_loop).py  sync/health/watch/debounce cadences
    workspace_sync.py      --interval default

Still hardcoded and recorded as TODO in the manifest ``hardcoded_inventory``
section (deliberately not rewired yet — behaviour must not change blind):

    disaster_recovery.py   bundle/fsck timeouts (60/300/600 s)
    automation_supervisor_loop.py  restart backoff / jitter / health floors
    git_maintenance.py     DEFAULT_THRESHOLDS, commit-graph thresholds
    pool_metrics.py        busy/backpressure/degraded thresholds
    git_control_plane.py   backpressure depth, busy fraction, latency warn
    worker_pool_types.py   pool capacity / parallelism limits
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

MANIFEST_FILENAME = "git_governance_manifest.json"
DIGEST_FILENAME = "git_governance_manifest.sha256"
MANIFEST_DIR_ENV = "GPTBRIDGE_GIT_GOVERNANCE_MANIFEST_DIR"
DEFAULT_MANIFEST_DIR = Path(__file__).resolve().parent

INTEGRITY_FIELD = "integrity"
BASELINE_FIELD = "baseline"
SUPPORTED_MANIFEST_SCHEMA_MAJOR = 1
SUPPORTED_GOVERNANCE_MAJOR = 2

REQUIRED_FIELDS: tuple[str, ...] = (
    "manifest_id",
    "manifest_schema_version",
    "governance_version",
    "policy_version",
    "registry_schema_version",
    "audit_schema_version",
    "queue_schema_version",
    "worker_schema_version",
    "hook_version",
    "snapshot_schema_version",
    "recovery_schema_version",
    "control_plane_version",
    "minimum_git_version",
    "maximum_tested_git_version",
)

_VERSION_RE = re.compile(r"(\d+)")


class RuntimeMode(str, Enum):
    ACTIVE = "ACTIVE"
    READ_ONLY = "READ_ONLY"
    UPGRADE_REQUIRED = "UPGRADE_REQUIRED"


class GovernanceWriteBlocked(RuntimeError):
    """Raised when a governed write is attempted outside ACTIVE mode."""

    def __init__(self, mode: str, reason: str, operation: str = "") -> None:
        self.mode = mode
        self.reason = reason
        self.operation = operation
        detail = f"governance-{mode.lower()}:{reason}"
        if operation:
            detail = f"{operation}:{detail}"
        super().__init__(detail)


@dataclass(frozen=True)
class GovernanceStatus:
    mode: RuntimeMode
    reason: str
    manifest_path: str
    digest: str = ""
    payload: Optional[Mapping[str, Any]] = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.mode is RuntimeMode.ACTIVE

    def as_tuple(self) -> tuple[str, str]:
        return self.mode.value, self.reason


def parse_version(text: Any) -> tuple[int, int, int]:
    """Best-effort numeric version tuple; missing components become 0."""
    numbers = [int(part) for part in _VERSION_RE.findall(str(text or ""))]
    numbers = (numbers + [0, 0, 0])[:3]
    return numbers[0], numbers[1], numbers[2]


def _version_major(text: Any) -> int:
    return parse_version(text)[0]


def canonical_payload(payload: Mapping[str, Any]) -> str:
    """Canonical JSON over the manifest minus integrity and baseline binding.

    ``integrity`` carries the digest itself and ``baseline`` binds the
    golden baseline digest (which in turn records this manifest's digest),
    so both are excluded to keep the manifest digest cycle-free: writing or
    changing the baseline binding never moves the manifest identity.
    """
    body = {
        key: value
        for key, value in payload.items()
        if key not in (INTEGRITY_FIELD, BASELINE_FIELD)
    }
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def compute_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()


def build_integrity(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "algorithm": "sha256",
        "digest": compute_digest(payload),
        "scope": "canonical-json-without-integrity-and-baseline",
    }


def write_manifest(
    payload: Mapping[str, Any], directory: str | Path
) -> GovernanceStatus:
    """(Re)write manifest + sidecar digest atomically; integrity is derived."""
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    body = {key: value for key, value in payload.items() if key != INTEGRITY_FIELD}
    integrity = build_integrity(body)
    body[INTEGRITY_FIELD] = integrity
    manifest_path = target_dir / MANIFEST_FILENAME
    digest_path = target_dir / DIGEST_FILENAME
    text = json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = manifest_path.with_name(
        manifest_path.name + f".{os.getpid()}.tmp"
    )
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, manifest_path)
    digest_tmp = digest_path.with_name(
        digest_path.name + f".{os.getpid()}.tmp"
    )
    digest_tmp.write_text(integrity["digest"] + "\n", encoding="ascii")
    os.replace(digest_tmp, digest_path)
    return GovernanceStatus(
        mode=RuntimeMode.ACTIVE,
        reason="ok",
        manifest_path=str(manifest_path),
        digest=integrity["digest"],
        payload=body,
    )


def _resolve_paths(
    manifest_path: str | Path | None = None,
    digest_path: str | Path | None = None,
    directory: str | Path | None = None,
) -> tuple[Path, Path]:
    if manifest_path is None:
        env_dir = os.environ.get(MANIFEST_DIR_ENV, "").strip()
        base = Path(env_dir) if env_dir else (
            Path(directory) if directory else DEFAULT_MANIFEST_DIR
        )
        manifest_path = base / MANIFEST_FILENAME
    manifest = Path(manifest_path)
    digest = Path(digest_path) if digest_path else (
        manifest.with_name(DIGEST_FILENAME)
    )
    return manifest, digest


def _read_digest_file(path: Path) -> tuple[bool, str]:
    """Returns (present, digest-or-reason)."""
    if not path.is_file():
        return False, ""
    try:
        raw = path.read_text(encoding="ascii").strip()
    except OSError:
        return True, ""
    return True, raw.split()[0] if raw else ""


def governance_status(
    manifest_path: str | Path | None = None,
    digest_path: str | Path | None = None,
    *,
    directory: str | Path | None = None,
) -> GovernanceStatus:
    """Verify manifest + digest; return the runtime mode and reason."""
    manifest, sidecar = _resolve_paths(manifest_path, digest_path, directory)
    if not manifest.is_file():
        return GovernanceStatus(
            RuntimeMode.UPGRADE_REQUIRED,
            f"manifest-missing:{manifest}",
            str(manifest),
        )
    try:
        raw = manifest.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except OSError as exc:
        return GovernanceStatus(
            RuntimeMode.READ_ONLY,
            f"manifest-read-error:{type(exc).__name__}",
            str(manifest),
        )
    except json.JSONDecodeError as exc:
        return GovernanceStatus(
            RuntimeMode.READ_ONLY, f"manifest-invalid-json:{exc.msg}", str(manifest)
        )
    if not isinstance(payload, dict):
        return GovernanceStatus(
            RuntimeMode.READ_ONLY, "manifest-not-object", str(manifest)
        )

    for name in REQUIRED_FIELDS:
        if payload.get(name) in (None, ""):
            return GovernanceStatus(
                RuntimeMode.READ_ONLY,
                f"manifest-format:missing-field:{name}",
                str(manifest),
            )

    manifest_schema_major = _version_major(payload.get("manifest_schema_version"))
    if manifest_schema_major < SUPPORTED_MANIFEST_SCHEMA_MAJOR:
        return GovernanceStatus(
            RuntimeMode.UPGRADE_REQUIRED,
            f"manifest-schema-too-old:{payload.get('manifest_schema_version')}",
            str(manifest),
        )
    if manifest_schema_major > SUPPORTED_MANIFEST_SCHEMA_MAJOR:
        return GovernanceStatus(
            RuntimeMode.READ_ONLY,
            f"manifest-schema-unsupported:{payload.get('manifest_schema_version')}",
            str(manifest),
        )

    governance_major = _version_major(payload.get("governance_version"))
    if governance_major < SUPPORTED_GOVERNANCE_MAJOR:
        return GovernanceStatus(
            RuntimeMode.UPGRADE_REQUIRED,
            f"governance-version-too-old:{payload.get('governance_version')}",
            str(manifest),
        )
    if governance_major > SUPPORTED_GOVERNANCE_MAJOR:
        return GovernanceStatus(
            RuntimeMode.READ_ONLY,
            f"governance-version-too-new:{payload.get('governance_version')}",
            str(manifest),
        )

    embedded = ""
    integrity = payload.get(INTEGRITY_FIELD)
    if isinstance(integrity, Mapping):
        embedded = str(integrity.get("digest") or "")
    present, sidecar_digest = _read_digest_file(sidecar)
    if present and not sidecar_digest:
        return GovernanceStatus(
            RuntimeMode.READ_ONLY, "manifest-digest-unreadable", str(manifest)
        )
    candidates = [value for value in (sidecar_digest, embedded) if value]
    if not candidates:
        return GovernanceStatus(
            RuntimeMode.READ_ONLY, "manifest-digest-missing", str(manifest)
        )
    if len(set(value.lower() for value in candidates)) > 1:
        return GovernanceStatus(
            RuntimeMode.READ_ONLY, "manifest-digest-conflict", str(manifest)
        )
    expected = candidates[0].lower()
    actual = compute_digest(payload)
    if actual != expected:
        return GovernanceStatus(
            RuntimeMode.READ_ONLY,
            "manifest-digest-mismatch",
            str(manifest),
            digest=actual,
        )
    return GovernanceStatus(
        RuntimeMode.ACTIVE,
        "ok",
        str(manifest),
        digest=actual,
        payload=payload,
    )


def governance_runtime_mode(
    manifest_path: str | Path | None = None,
    digest_path: str | Path | None = None,
    *,
    directory: str | Path | None = None,
) -> tuple[str, str]:
    """Return ``(mode, reason)`` — ACTIVE / READ_ONLY / UPGRADE_REQUIRED."""
    return governance_status(
        manifest_path, digest_path, directory=directory
    ).as_tuple()


def is_write_allowed(**kwargs: Any) -> bool:
    return governance_status(**kwargs).ok


def assert_write_allowed(operation: str = "", **kwargs: Any) -> None:
    """Fail closed when the manifest is not verified ACTIVE."""
    status = governance_status(**kwargs)
    if not status.ok:
        raise GovernanceWriteBlocked(
            status.mode.value, status.reason, operation
        )


def manifest_field(name: str, default: Any = "") -> Any:
    """Read one scalar field from the verified manifest (fallback on doubt)."""
    status = governance_status()
    if not status.ok or status.payload is None:
        return default
    value = status.payload.get(name, default)
    return default if value in (None, "") else value


def timing(name: str, default: float) -> float:
    """Read one governed timing; tampered/missing manifest -> default."""
    status = governance_status()
    if not status.ok or status.payload is None:
        return float(default)
    timings = status.payload.get("timings")
    if not isinstance(timings, Mapping) or name not in timings:
        return float(default)
    try:
        return float(timings[name])
    except (TypeError, ValueError):
        return float(default)


def schema_version(name: str, default: str) -> str:
    """Read one schema component version; fallback when unverified."""
    return str(manifest_field(name, default))


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Git governance manifest integrity utility."
    )
    parser.add_argument("--status", action="store_true", help="print runtime mode")
    parser.add_argument("--check", action="store_true", help="exit 1 unless ACTIVE")
    parser.add_argument(
        "--regenerate-digest",
        action="store_true",
        help="rewrite manifest + sidecar from the on-disk payload",
    )
    parser.add_argument(
        "--dir", default="", help="manifest directory override"
    )
    args = parser.parse_args(argv)
    directory = Path(args.dir) if args.dir else None

    if args.regenerate_digest:
        manifest, _ = _resolve_paths(directory=directory)
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[FAIL] manifest unreadable: {exc}", file=sys.stderr)
            return 1
        if not isinstance(payload, dict):
            print("[FAIL] manifest is not an object", file=sys.stderr)
            return 1
        status = write_manifest(payload, directory or manifest.parent)
        print(f"[OK] digest {status.digest}")
        return 0

    status = governance_status(directory=directory)
    print(f"{status.mode.value} {status.reason} digest={status.digest[:12]}")
    if args.check:
        return 0 if status.ok else 1
    return 0


__all__ = [
    "BASELINE_FIELD",
    "DEFAULT_MANIFEST_DIR",
    "DIGEST_FILENAME",
    "GovernanceStatus",
    "GovernanceWriteBlocked",
    "MANIFEST_DIR_ENV",
    "MANIFEST_FILENAME",
    "REQUIRED_FIELDS",
    "RuntimeMode",
    "SUPPORTED_GOVERNANCE_MAJOR",
    "SUPPORTED_MANIFEST_SCHEMA_MAJOR",
    "assert_write_allowed",
    "build_integrity",
    "canonical_payload",
    "cli_main",
    "compute_digest",
    "governance_runtime_mode",
    "governance_status",
    "is_write_allowed",
    "manifest_field",
    "parse_version",
    "schema_version",
    "timing",
    "write_manifest",
]


if __name__ == "__main__":
    raise SystemExit(cli_main())
