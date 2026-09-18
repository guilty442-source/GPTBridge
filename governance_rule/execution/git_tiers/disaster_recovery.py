"""Git Disaster Recovery Manager (Git layer only).

Evidence, diagnosis, backup, recovery planning and verification — nothing
else.  This module never touches SQL, RAG, LLM runtime or application data,
and it NEVER executes high-risk operations:

    no reset --hard, no force push, no history rewrite, no ref deletion,
    no reflog expiry, no aggressive prune.

Recovery refs (``refs/gptbridge/recovery/<id>``) are anchors, not release
authority.  Tier-3 actions appear only as *proposals* inside a recovery plan
and require governance authority approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from . import TIER3_OPS, audit_log, classify
from . import audit_chain
from .paths import BACKUP_ROOT_RELATIVE, contained

RECOVERY_REF_PREFIX: Final[str] = "refs/gptbridge/recovery/"
AUTOMATION_STATE_RELATIVE: Final[str] = "gptbridge-automation"
#: Backup root, relative to the repository root (A201 containment).
DEFAULT_BACKUP_ROOT: Final[str] = BACKUP_ROOT_RELATIVE.as_posix()

BUNDLE_KINDS: Final[frozenset[str]] = frozenset(
    {"release", "pre-migration", "governance-structure", "ref-maintenance", "manual"}
)

EMERGENCY_MODES: Final[tuple[str, ...]] = (
    "NORMAL",
    "DEGRADED",
    "READ_ONLY",
    "RECOVERY",
    "STOPPED",
)

READ_ONLY_ALLOWED: Final[frozenset[str]] = frozenset(
    {"status", "log", "diff", "show", "bundle verify", "fsck", "rev-parse", "cat-file", "for-each-ref"}
)

WORKTREE_STATES: Final[tuple[str, ...]] = (
    "HEALTHY",
    "MISSING_PATH",
    "BROKEN_GITFILE",
    "BROKEN_HEAD",
    "BROKEN_INDEX",
    "BRANCH_MISMATCH",
    "ORPHANED",
)

_INTEGRITY_STATES: Final[tuple[str, ...]] = ("HEALTHY", "WARN", "CORRUPT", "UNKNOWN")

_FSCK_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("CORRUPT", re.compile(r"(missing|corrupt|invalid|bad) (blob|tree|commit|tag|object)", re.I)),
    ("CORRUPT", re.compile(r"error: (object file|loose object|unable)", re.I)),
    ("CORRUPT", re.compile(r"broken (link|ref)", re.I)),
    ("WARN", re.compile(r"dangling (blob|tree|commit|tag)", re.I)),
    ("WARN", re.compile(r"unreachable (blob|tree|commit|tag)", re.I)),
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DisasterRecoveryError(RuntimeError):
    """Raised when a DR action violates its bounded contract."""


@dataclass
class RecoveryPoint:
    recovery_id: str
    timestamp: str
    main_revision: str
    origin_main_revision: str
    queue_id: str
    source_branch: str
    source_revision: str
    audit_sequence: int
    worktree_inventory_digest: str
    branch_inventory_digest: str
    git_config_digest: str
    hook_digest: str
    reason: str
    ref_name: str = ""


@dataclass
class BundleManifest:
    bundle_name: str
    bundle_sha256: str
    created_at: str
    source_repository: str
    main_revision: str
    origin_revision: str
    included_refs: list[str]
    audit_sequence: int
    git_version: str
    verified: bool
    state: str = "CREATING"
    kind: str = "manual"
    path: str = ""


@dataclass
class RepositoryDiagnosis:
    state: str
    dangling: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)
    broken_refs: list[str] = field(default_factory=list)
    raw_excerpt: list[str] = field(default_factory=list)


@dataclass
class WorktreeIntegrity:
    path: str
    state: str
    detail: str = ""
    branch: str = ""
    head: str = ""
    dirty: bool = False


@dataclass
class RecoveryPlanOption:
    option_id: str
    summary: str
    git_commands: list[str]
    risk_tier: int
    expected_result: str
    rollback_anchor: str
    required_approval: str


@dataclass
class RecoveryPlan:
    incident_id: str
    detected_at: str
    severity: str
    repository_state: str
    known_good_revision: str
    current_revision: str
    origin_revision: str
    recovery_points: list[str]
    bundle_available: list[str]
    affected_worktrees: list[str]
    affected_branches: list[str]
    audit_status: str
    options: list[RecoveryPlanOption] = field(default_factory=list)
    classification: str = ""


class GitDisasterRecovery:
    """Bounded Git-only recovery manager."""

    def __init__(
        self,
        root: str | Path = r"E:\GPTBridge",
        *,
        backup_root: str | Path | None = None,
        enable_audit: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        configured = backup_root or os.environ.get("GPTBRIDGE_GIT_BACKUP_ROOT")
        self.backup_root = contained(
            self.root,
            configured or DEFAULT_BACKUP_ROOT,
            purpose="backup-root",
        )
        self.enable_audit = enable_audit

    # ------------------------------------------------------------------
    # git plumbing (Tier-1 reads; Tier-2 sanctioned captures only)
    # ------------------------------------------------------------------

    # TODO(inventory): wire recovery timeouts (60/300/600 s) to manifest
    # timings.disaster_recovery_* (git_governance_manifest.json).
    def _git(
        self,
        args: list[str],
        *,
        confirmed: bool = False,
        authorized: bool = False,
        timeout: float = 60.0,
    ):
        """Run one bounded command.

        ``confirmed`` (deprecated) and ``authorized`` both request the governed
        Tier-2 path, which issues a ``SYSTEM_SAFE_AUTOMATION`` capability and
        executes through the capability gate.  Tier-1 commands run through the
        gateway directly; Tier-3 remains forbidden.
        """
        command = " ".join(args)
        from .git_repository import GitRepository, _extended_tier

        extended = _extended_tier(command)
        tier = extended if extended is not None else classify(command)
        if tier >= 3:
            raise DisasterRecoveryError(f"TIER3_OPERATION_FORBIDDEN:{command}")
        actor = "governance/git-disaster-recovery"
        if tier == 2 and (authorized or confirmed):
            from .capability_gate import execute_system_safe

            gate = execute_system_safe(
                args, actor=actor, repo_path=self.root, timeout=timeout,
            )
            if gate.allowed is False or gate.execution_result is None:
                raise PermissionError(f"{gate.code}: {gate.detail}")
            return gate.execution_result
        return GitRepository(self.root).run(
            args, actor=actor, timeout=timeout,
        )

    def _out(self, args: list[str], *, authorized: bool = False, timeout: float = 60.0) -> str:
        result = self._git(args, authorized=authorized, timeout=timeout)
        if result.returncode != 0:
            raise DisasterRecoveryError(
                f"GIT_COMMAND_FAILED:{' '.join(args)}:{result.returncode}:{result.stderr[:200]}"
            )
        return result.stdout

    def _try(self, args: list[str], *, authorized: bool = False) -> str:
        try:
            return self._out(args, authorized=authorized)
        except (DisasterRecoveryError, PermissionError):
            return ""

    def _common_git_dir(self) -> Path:
        value = self._try(["rev-parse", "--git-common-dir"]).strip()
        if not value:
            return self.root / ".git"
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (self.root / candidate).resolve()

    def recovery_dir(self) -> Path:
        return self._common_git_dir() / AUTOMATION_STATE_RELATIVE / "recovery"

    def ref_value(self, ref: str) -> str:
        return self._try(["rev-parse", "--verify", "-q", ref]).strip()

    # ------------------------------------------------------------------
    # recovery points (116 / 117 / 134 / 135)
    # ------------------------------------------------------------------

    def _worktree_inventory_digest(self) -> str:
        return _sha256_text(self._try(["worktree", "list", "--porcelain"]))

    def _branch_inventory_digest(self) -> str:
        return _sha256_text(
            self._try(["for-each-ref", "refs/heads", "--format=%(refname) %(objectname)"])
        )

    def git_config_snapshot(self) -> dict[str, str]:
        """Sanitized config: no credential helpers, no userinfo URLs."""
        raw = self._try(["config", "--list", "--local"])
        sanitized: dict[str, str] = {}
        for line in raw.splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            lowered = key.casefold()
            keep = (
                lowered.startswith(("core.", "remote.", "branch.", "gc.", "maintenance."))
                or lowered == "extensions.objectformat"
            )
            if not keep:
                continue
            if "credential" in lowered or "url" in lowered and "@" in value:
                value = "<redacted>"
            sanitized[key] = value
        return sanitized

    def git_config_digest(self) -> str:
        snapshot = self.git_config_snapshot()
        return _sha256_text(json.dumps(snapshot, sort_keys=True))

    def hook_digest(self) -> str:
        hooks_dir = self._common_git_dir() / "hooks"
        digests: dict[str, str] = {}
        for name in ("pre-commit", "pre-merge-commit", "pre-push", "pre-receive", "post-receive"):
            path = hooks_dir / name
            if path.is_file():
                digests[name] = _sha256_file(path)
        return _sha256_text(json.dumps(digests, sort_keys=True))

    def audit_sequence(self) -> int:
        state_path = Path(audit_chain._chain_dir()) / "chain_state.json"
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            return int(payload.get("next_sequence", 1)) - 1
        except (OSError, ValueError):
            return 0

    def tier_policy_snapshot(self) -> dict[str, Any]:
        from . import TIER1_OPS, TIER2_OPS

        return {
            "tier_policy_version": 1,
            "tier1_count": len(TIER1_OPS),
            "tier2_count": len(TIER2_OPS),
            "tier3_count": len(TIER3_OPS),
            "tier3_digest": _sha256_text("\n".join(sorted(TIER3_OPS))),
        }

    def capture_recovery_point(
        self,
        reason: str,
        *,
        queue_id: str = "",
        source_branch: str = "",
        source_revision: str = "",
        create_ref: bool = True,
    ) -> RecoveryPoint:
        if not reason:
            raise DisasterRecoveryError("RECOVERY_POINT_REASON_REQUIRED")
        main = self.ref_value("refs/heads/main") or self._try(["rev-parse", "HEAD"]).strip()
        if not main:
            raise DisasterRecoveryError("RECOVERY_POINT_MAIN_UNRESOLVED")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        recovery_id = f"{stamp}-{main[:8]}"
        point = RecoveryPoint(
            recovery_id=recovery_id,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            main_revision=main,
            origin_main_revision=self.ref_value("refs/remotes/origin/main"),
            queue_id=queue_id,
            source_branch=source_branch,
            source_revision=source_revision,
            audit_sequence=self.audit_sequence(),
            worktree_inventory_digest=self._worktree_inventory_digest(),
            branch_inventory_digest=self._branch_inventory_digest(),
            git_config_digest=self.git_config_digest(),
            hook_digest=self.hook_digest(),
            reason=reason,
        )
        if create_ref:
            point.ref_name = self.create_recovery_ref(recovery_id, main)
        self.recovery_dir().mkdir(parents=True, exist_ok=True)
        path = self.recovery_dir() / f"{recovery_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(point), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        self._audit(1, "recovery-point-capture", f"id={recovery_id} sha={main[:12]} reason={reason}")
        return point

    def create_recovery_ref(self, recovery_id: str, sha: str) -> str:
        """Create ``refs/gptbridge/recovery/<id>`` (Tier-2 sanctioned anchor)."""
        ref = f"{RECOVERY_REF_PREFIX}{recovery_id}"
        existing = self.ref_value(ref)
        if existing:
            if existing != sha:
                raise DisasterRecoveryError(f"RECOVERY_REF_EXISTS_WITH_OTHER_SHA:{ref}")
            return ref
        self._out(["update-ref", ref, sha], authorized=True)
        return ref

    def list_recovery_points(self) -> list[RecoveryPoint]:
        points: list[RecoveryPoint] = []
        valid_fields = set(RecoveryPoint.__dataclass_fields__)
        required = {"recovery_id", "main_revision", "reason"}
        for path in sorted(self.recovery_dir().glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict) or not required.issubset(payload):
                continue
            points.append(RecoveryPoint(**{k: v for k, v in payload.items() if k in valid_fields}))
        return points

    def latest_recovery_point(self) -> RecoveryPoint | None:
        points = self.list_recovery_points()
        return points[-1] if points else None

    def verify_recovery_point(self, recovery_id: str) -> list[str]:
        errors: list[str] = []
        path = self.recovery_dir() / f"{recovery_id}.json"
        if not path.is_file():
            return [f"recovery point file missing: {recovery_id}"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        sha = str(payload.get("main_revision") or "")
        if not sha or not self._commit_exists(sha):
            errors.append(f"recovery point main revision is not resolvable: {recovery_id}")
        ref = str(payload.get("ref_name") or "")
        if ref:
            ref_sha = self.ref_value(ref)
            if not ref_sha:
                errors.append(f"recovery ref missing: {ref}")
            elif ref_sha != sha:
                errors.append(f"recovery ref sha mismatch: {ref}")
        if not str(payload.get("reason") or ""):
            errors.append(f"recovery point has no reason: {recovery_id}")
        return errors

    def _commit_exists(self, sha: str) -> bool:
        result = self._git(["cat-file", "-e", f"{sha}^{{commit}}"])
        return result.returncode == 0

    # ------------------------------------------------------------------
    # bundles (119-121, 157-158)
    # ------------------------------------------------------------------

    def create_bundle_backup(self, kind: str = "manual", *, verify: bool = True) -> BundleManifest:
        if kind not in BUNDLE_KINDS:
            raise DisasterRecoveryError(f"BUNDLE_KIND_INVALID:{kind}")
        main = self.ref_value("refs/heads/main") or self._try(["rev-parse", "HEAD"]).strip()
        self.backup_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        name = f"GPTBridge-main-{stamp}-{main[:8]}.bundle"
        path = self.backup_root / name
        git_version = (self._try(["--version"]).strip() or "git")
        manifest = BundleManifest(
            bundle_name=name,
            bundle_sha256="",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            source_repository=str(self.root),
            main_revision=main,
            origin_revision=self.ref_value("refs/remotes/origin/main"),
            included_refs=self._included_refs(),
            audit_sequence=self.audit_sequence(),
            git_version=git_version,
            verified=False,
            state="CREATING",
            kind=kind,
            path=str(path),
        )
        try:
            self._out(["bundle", "create", str(path), "--all"], authorized=True, timeout=300.0)
            manifest.state = "VERIFYING"
            manifest.bundle_sha256 = _sha256_file(path) if path.is_file() else ""
            if verify:
                ok, detail = self.verify_bundle(path)
                if not ok:
                    manifest.state = "FAILED"
                    self._write_bundle_manifest(manifest)
                    raise DisasterRecoveryError(f"BUNDLE_VERIFY_FAILED:{detail}")
                manifest.verified = True
            manifest.state = "READY"
        except DisasterRecoveryError:
            manifest.state = "FAILED"
            self._write_bundle_manifest(manifest)
            raise
        except PermissionError as error:
            manifest.state = "FAILED"
            self._write_bundle_manifest(manifest)
            raise DisasterRecoveryError(f"BUNDLE_PERMISSION_DENIED:{error}") from error
        self._write_bundle_manifest(manifest)
        self._audit(2, "bundle-create", f"name={name} kind={kind} sha256={manifest.bundle_sha256[:12]}")
        return manifest

    def _included_refs(self) -> list[str]:
        raw = self._try(["for-each-ref", "refs/heads", "refs/remotes", "--format=%(refname)"])
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _write_bundle_manifest(self, manifest: BundleManifest) -> None:
        payload = asdict(manifest)
        path = self.backup_root / f"{manifest.bundle_name}.manifest.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def verify_bundle(self, bundle_path: str | Path) -> tuple[bool, str]:
        path = Path(bundle_path)
        if not path.is_file():
            return False, "bundle-missing"
        result = self._git(["bundle", "verify", str(path)], timeout=300.0)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout).strip()[:300]
        return True, "ok"

    def list_bundles(self) -> list[dict[str, Any]]:
        bundles: list[dict[str, Any]] = []
        if not self.backup_root.is_dir():
            return bundles
        for path in sorted(self.backup_root.glob("*.bundle")):
            manifest_path = self.backup_root / f"{path.name}.manifest.json"
            entry: dict[str, Any] = {"name": path.name, "size": path.stat().st_size}
            try:
                entry.update(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                entry["manifest"] = "missing"
            bundles.append(entry)
        return bundles

    def backup_storage_health(self) -> dict[str, Any]:
        health: dict[str, Any] = {
            "backup_path_exists": self.backup_root.is_dir(),
            "available_space": 0,
            "last_successful_bundle": "",
            "last_verified_bundle": "",
            "bundle_age_hours": None,
            "bundle_count": 0,
            "state": "UNKNOWN",
        }
        if not self.backup_root.is_dir():
            health["state"] = "UNAVAILABLE"
            return health
        try:
            import shutil

            health["available_space"] = shutil.disk_usage(self.backup_root).free
        except OSError:
            pass
        bundles = self.list_bundles()
        health["bundle_count"] = len(bundles)
        verified = [entry for entry in bundles if entry.get("verified")]
        if bundles:
            newest = bundles[-1]
            health["last_successful_bundle"] = str(newest.get("name") or "")
            try:
                created = Path(newest["path"]).stat().st_mtime
                health["bundle_age_hours"] = round((time.time() - created) / 3600.0, 2)
            except (KeyError, OSError):
                pass
        if verified:
            health["last_verified_bundle"] = str(verified[-1].get("name") or "")
        health["state"] = "HEALTHY" if verified else ("WARN" if bundles else "WARN")
        if health["available_space"] and health["available_space"] < 2 * 1024**3:
            health["state"] = "BACKUP_STORAGE_PRESSURE"
        return health

    # ------------------------------------------------------------------
    # integrity diagnosis (122-124)
    # ------------------------------------------------------------------

    def diagnose_repository(self, *, deep: bool = False) -> RepositoryDiagnosis:
        if not deep:
            return RepositoryDiagnosis(state="UNKNOWN", raw_excerpt=["deep=false (fast path never runs fsck)"])
        result = self._git(["fsck", "--no-progress", "--connectivity-only"], timeout=600.0)
        output = f"{result.stdout}\n{result.stderr}"
        diagnosis = RepositoryDiagnosis(state="HEALTHY")
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            matched = False
            for bucket, pattern in _FSCK_PATTERNS:
                if pattern.search(stripped):
                    matched = True
                    if bucket == "CORRUPT":
                        diagnosis.state = "CORRUPT"
                        if "broken" in stripped.casefold():
                            diagnosis.broken_refs.append(stripped)
                        elif "missing" in stripped.casefold():
                            diagnosis.missing.append(stripped)
                        else:
                            diagnosis.corrupt.append(stripped)
                    elif diagnosis.state != "CORRUPT":
                        diagnosis.state = "WARN"
                        if "dangling" in stripped.casefold():
                            diagnosis.dangling.append(stripped)
                        else:
                            diagnosis.unreachable.append(stripped)
                    break
            if not matched and len(diagnosis.raw_excerpt) < 40:
                diagnosis.raw_excerpt.append(stripped)
        if result.returncode != 0 and diagnosis.state == "HEALTHY":
            diagnosis.state = "UNKNOWN"
        return diagnosis

    def revision_matrix(self) -> dict[str, Any]:
        local = self.ref_value("refs/heads/main")
        origin = self.ref_value("refs/remotes/origin/main")
        matrix: dict[str, Any] = {
            "local": local,
            "origin": origin,
            "classification": "UNKNOWN",
            "ancestry": {},
            "missing_commits": {},
        }
        refs = {"local": local, "origin": origin}
        for name, sha in refs.items():
            matrix["ancestry"][name] = {
                other: bool(sha and other_sha and sha != other_sha and self._is_ancestor(sha, other_sha))
                for other, other_sha in refs.items()
                if other != name
            }
        present = {name: sha for name, sha in refs.items() if sha}
        if len(present) < len(refs):
            # incomplete evidence: never guess divergence from a missing ref
            matrix["classification"] = "UNKNOWN"
        elif len(set(present.values())) == 1:
            matrix["classification"] = "ALL_EQUAL"
        elif self._is_ancestor(local, origin):
            matrix["classification"] = "ORIGIN_AHEAD"
        elif self._is_ancestor(origin, local):
            matrix["classification"] = "LOCAL_AHEAD"
        else:
            matrix["classification"] = "LOCAL_DIVERGED"
        for name, sha in present.items():
            counts = self._try(["rev-list", "--count", f"refs/heads/main..{sha}"])
            matrix["missing_commits"][name] = int(counts or 0) if counts.isdigit() else 0
        return matrix

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._git(["merge-base", "--is-ancestor", ancestor, descendant])
        return result.returncode == 0

    # ------------------------------------------------------------------
    # interrupted operations (127-129, 149-150)
    # ------------------------------------------------------------------

    def interrupted_state(self) -> dict[str, Any]:
        git_dir = self._worktree_git_dir()
        state = {
            "index_lock": (git_dir / "index.lock").exists(),
            "index_lock_age_seconds": None,
            "merge_head": (git_dir / "MERGE_HEAD").exists(),
            "cherry_pick_head": (git_dir / "CHERRY_PICK_HEAD").exists(),
            "revert_head": (git_dir / "REVERT_HEAD").exists(),
            "rebase_merge": (git_dir / "rebase-merge").exists(),
            "rebase_apply": (git_dir / "rebase-apply").exists(),
            "commit_editmsg": (git_dir / "COMMIT_EDITMSG").exists(),
        }
        lock = git_dir / "index.lock"
        if lock.exists():
            try:
                state["index_lock_age_seconds"] = round(time.time() - lock.stat().st_mtime, 1)
            except OSError:
                pass
        state["state"] = (
            "INTERRUPTED" if any(
                state[key]
                for key in ("merge_head", "cherry_pick_head", "revert_head", "rebase_merge", "rebase_apply")
            ) else ("STALE_INDEX_LOCK_CANDIDATE" if state["index_lock"] else "CLEAN")
        )
        state["note"] = "never auto-remove index.lock; governance decides (§128)"
        return state

    def _worktree_git_dir(self) -> Path:
        dot_git = self.root / ".git"
        if dot_git.is_file():
            try:
                raw = dot_git.read_text(encoding="utf-8").strip()
                if raw.startswith("gitdir:"):
                    return Path(raw[7:].strip())
            except OSError:
                pass
        return dot_git

    def audit_ledger_health(self) -> dict[str, Any]:
        """Detect a partial trailing record without truncating anything."""
        path = Path(audit_chain._chain_dir()) / "chain_state.json"
        health: dict[str, Any] = {"ledger": str(path), "state": "UNKNOWN", "last_complete_sequence": 0}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            health["last_complete_sequence"] = int(payload.get("next_sequence", 1)) - 1
            health["last_hash"] = str(payload.get("last_hash") or "")
            health["state"] = "HEALTHY"
        except FileNotFoundError:
            health["state"] = "UNKNOWN"
        except (OSError, ValueError):
            health["state"] = "CORRUPT"
        return health

    def registry_health(self) -> dict[str, Any]:
        path = self._common_git_dir() / AUTOMATION_STATE_RELATIVE / "registry.json"
        health: dict[str, Any] = {"path": str(path), "state": "UNKNOWN"}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            health["state"] = "HEALTHY"
            health["children"] = len(payload.get("children") or [])
        except FileNotFoundError:
            health["state"] = "MISSING"
        except (OSError, ValueError):
            health["state"] = "CORRUPT"
        return health

    def hook_integrity(self) -> dict[str, Any]:
        current = self.hook_digest()
        state_path = self._common_git_dir() / AUTOMATION_STATE_RELATIVE / "hook-integrity.json"
        previous: dict[str, Any] = {}
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        expected = str(previous.get("hook_digest") or "")
        state = "HEALTHY"
        if expected and expected != current:
            state = "FAILED"
        elif not expected:
            state = "UNKNOWN"
        return {"state": state, "hook_digest": current, "expected": expected}

    # ------------------------------------------------------------------
    # worktrees (130-131)
    # ------------------------------------------------------------------

    def worktree_integrity(self) -> list[WorktreeIntegrity]:
        results: list[WorktreeIntegrity] = []
        porcelain = self._try(["worktree", "list", "--porcelain"])
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in porcelain.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key.strip()] = value.strip()
        if current:
            entries.append(current)
        for entry in entries:
            path = entry.get("worktree", "")
            integrity = self._inspect_worktree(path, entry)
            results.append(integrity)
        return results
    def _inspect_worktree(self, path: str, entry: dict[str, str]) -> WorktreeIntegrity:
        if not path:
            return WorktreeIntegrity(path="", state="ORPHANED", detail="worktree path missing in listing")
        target = Path(path)
        path = str(target)
        if not target.exists():
            return WorktreeIntegrity(path=path, state="MISSING_PATH")
        if entry.get("bare") == "bare" or entry.get("detached") == "detached":
            # bare common repo / detached worktree: only basic checks
            pass
        dot_git = target / ".git"
        if not dot_git.exists():
            return WorktreeIntegrity(path=path, state="BROKEN_GITFILE", detail=".git missing")
        if dot_git.is_file():
            raw = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            if not raw.startswith("gitdir:"):
                return WorktreeIntegrity(path=path, state="BROKEN_GITFILE", detail="gitdir pointer invalid")
            git_dir = Path(raw[7:].strip())
            if not git_dir.exists():
                return WorktreeIntegrity(path=path, state="BROKEN_GITFILE", detail="gitdir target missing")
        dirty = False
        from .git_repository import GitRepository

        inspector = GitRepository(target)
        try:
            status = inspector.run(["status", "--porcelain"], timeout=30.0)
            if status.returncode == 0:
                dirty = bool(status.stdout.strip())
        except (OSError, PermissionError):
            return WorktreeIntegrity(path=path, state="BROKEN_INDEX", detail="git status failed")
        head = ""
        branch = entry.get("branch", "").replace("refs/heads/", "")
        try:
            resolve = inspector.run(["rev-parse", "HEAD"], timeout=30.0)
            if resolve.returncode != 0:
                return WorktreeIntegrity(path=path, state="BROKEN_HEAD", detail="HEAD unresolved")
            head = resolve.stdout.strip()
        except (OSError, PermissionError):
            return WorktreeIntegrity(path=path, state="BROKEN_HEAD", detail="HEAD check failed")
        return WorktreeIntegrity(path=path, state="HEALTHY", branch=branch, head=head, dirty=dirty)

    # ------------------------------------------------------------------
    # emergency modes (151-153)
    # ------------------------------------------------------------------

    def emergency_mode(self) -> str:
        path = self._common_git_dir() / AUTOMATION_STATE_RELATIVE / "emergency-mode.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            mode = str(payload.get("mode") or "NORMAL")
            return mode if mode in EMERGENCY_MODES else "UNKNOWN"
        except (OSError, ValueError):
            return "NORMAL"

    def set_emergency_mode(self, mode: str, reason: str, *, actor: str = "git-disaster-recovery") -> None:
        if mode not in EMERGENCY_MODES:
            raise DisasterRecoveryError(f"EMERGENCY_MODE_INVALID:{mode}")
        path = self._common_git_dir() / AUTOMATION_STATE_RELATIVE / "emergency-mode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": mode,
            "reason": reason,
            "actor": actor,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        self._audit(2, "emergency-mode", f"mode={mode} reason={reason}")

    def read_only_triggers(self) -> list[str]:
        triggers: list[str] = []
        diagnosis = self.diagnose_repository(deep=True)
        if diagnosis.state == "CORRUPT":
            triggers.append("object-corruption")
        if self.audit_ledger_health()["state"] == "CORRUPT":
            triggers.append("audit-corruption")
        if self.registry_health()["state"] == "CORRUPT":
            triggers.append("registry-corruption")
        if self.hook_integrity()["state"] == "FAILED":
            triggers.append("hook-integrity-failure")
        matrix = self.revision_matrix()
        if matrix["classification"] in ("MULTI_DIVERGED", "LOCAL_DIVERGED"):
            triggers.append("remote-divergence-unknown")
        return triggers

    def evaluate_emergency_mode(self, *, auto_apply: bool = False) -> dict[str, Any]:
        triggers = self.read_only_triggers()
        proposal = "READ_ONLY" if triggers else "NORMAL"
        if auto_apply and proposal == "READ_ONLY":
            self.set_emergency_mode("READ_ONLY", ",".join(triggers))
        return {"current": self.emergency_mode(), "proposed": proposal, "triggers": triggers}

    # ------------------------------------------------------------------
    # recovery plan (126, 155) + verification (127, 132-133)
    # ------------------------------------------------------------------

    def prepare_recovery_plan(self, incident_id: str, severity: str = "high") -> RecoveryPlan:
        main = self.ref_value("refs/heads/main")
        matrix = self.revision_matrix()
        points = self.list_recovery_points()
        bundles = self.list_bundles()
        worktrees = self.worktree_integrity()
        diagnosis = self.diagnose_repository(deep=True)
        interrupted = self.interrupted_state()
        plan = RecoveryPlan(
            incident_id=incident_id,
            detected_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            severity=severity,
            repository_state=diagnosis.state,
            known_good_revision=points[-1].main_revision if points else "",
            current_revision=main,
            origin_revision=matrix["origin"],
            recovery_points=[point.recovery_id for point in points][-10:],
            bundle_available=[str(entry.get("name") or "") for entry in bundles][-5:],
            affected_worktrees=[item.path for item in worktrees if item.state != "HEALTHY"],
            affected_branches=[],
            audit_status=self.audit_ledger_health()["state"],
            classification=matrix["classification"],
        )
        anchor = plan.known_good_revision or main
        plan.options = [
            RecoveryPlanOption(
                option_id="OPTION_A",
                summary="現地修正：以新 commit 修復合併內容（不改寫歷史）",
                git_commands=[
                    "# (governed) restore/repair working tree, then:",
                    "git add <repaired paths>",
                    "git commit -m 'repair: <incident>'",
                ],
                risk_tier=2,
                expected_result="main 前進一個修復 commit；不重寫既有歷史",
                rollback_anchor=anchor,
                required_approval="coordinator + governance audit PASS",
            ),
            RecoveryPlanOption(
                option_id="OPTION_B",
                summary="受治理 revert 合併 commit（保留原始歷史）",
                git_commands=[
                    f"git revert -m 1 <merge-sha>  # 需治理批准;HEAD={main[:12]}",
                ],
                risk_tier=3,
                expected_result="產生反向 commit；歷史保留",
                rollback_anchor=anchor,
                required_approval="governance authority explicit approval",
            ),
            RecoveryPlanOption(
                option_id="OPTION_C",
                summary="從 bundle/中央/遠端證據重建（僅在 object 損壞時）",
                git_commands=[
                    "git bundle verify <bundle>",
                    f"git clone <bundle> <temp>  # 於暫存目錄驗證;anchor={anchor[:12]}",
                ],
                risk_tier=3,
                expected_result="以最近 verified bundle 重建；需人工驗證後切換",
                rollback_anchor=anchor,
                required_approval="governance authority + release certification",
            ),
        ]
        if interrupted["state"] == "INTERRUPTED":
            plan.options.insert(
                0,
                RecoveryPlanOption(
                    option_id="OPTION_0",
                    summary="完成/中止中斷中的 Git 操作（需 transaction metadata）",
                    git_commands=[
                        "git status  # 僅允許診斷;不得自動 continue/abort",
                        "git merge --abort  # 僅 coordinator 且需完整 transaction metadata",
                    ],
                    risk_tier=2,
                    expected_result="回到 pre-merge HEAD 並驗證 index/worktree/merge metadata",
                    rollback_anchor=anchor,
                    required_approval="coordinator with recorded transaction_id",
                ),
            )
        return plan

    def verify_recovered_state(self, pre_merge_head: str) -> list[str]:
        """Post-abort verification: never trust only the exit code."""
        errors: list[str] = []
        head = self.ref_value("refs/heads/main") or self._try(["rev-parse", "HEAD"]).strip()
        if pre_merge_head and head != pre_merge_head:
            errors.append(f"HEAD_MISMATCH:expected={pre_merge_head[:12]}:actual={head[:12]}")
        state = self.interrupted_state()
        for key in ("merge_head", "cherry_pick_head", "revert_head", "rebase_merge", "rebase_apply"):
            if state[key]:
                errors.append(f"INTERRUPTED_STATE_REMAINS:{key}")
        if state["index_lock"]:
            errors.append("INDEX_LOCK_REMAINS")
        return errors

    # ------------------------------------------------------------------
    # metrics (156)
    # ------------------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        point = self.latest_recovery_point()
        bundles = self.list_bundles()
        age = None
        if point:
            try:
                created = time.mktime(time.strptime(point.timestamp[:19], "%Y-%m-%dT%H:%M:%S"))
                age = round((time.time() - created) / 3600.0, 2)
            except ValueError:
                age = None
        return {
            "last_recovery_point": point.recovery_id if point else "",
            "recovery_point_age_hours": age,
            "last_verified_bundle": next(
                (entry.get("name") for entry in reversed(bundles) if entry.get("verified")), ""
            ),
            "bundle_count": len(bundles),
            "audit_sequence": self.audit_sequence(),
            "mode": self.emergency_mode(),
        }

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    def _audit(self, tier: int, operation: str, detail: str) -> None:
        if not self.enable_audit:
            return
        try:
            entry = audit_log(
                tier,
                f"dr:{operation}",
                "governance/git-disaster-recovery",
                True,
                detail,
                operation="dr:" + operation,
                phase="result",
                result="succeeded",
            )
            audit_chain.append_audit(dict(entry))
        except Exception:
            pass


__all__ = [
    "AUTOMATION_STATE_RELATIVE",
    "BUNDLE_KINDS",
    "DEFAULT_BACKUP_ROOT",
    "DisasterRecoveryError",
    "EMERGENCY_MODES",
    "GitDisasterRecovery",
    "READ_ONLY_ALLOWED",
    "RECOVERY_REF_PREFIX",
    "BundleManifest",
    "RecoveryPlan",
    "RecoveryPlanOption",
    "RecoveryPoint",
    "RepositoryDiagnosis",
    "WorktreeIntegrity",
]
