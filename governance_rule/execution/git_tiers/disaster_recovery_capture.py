"""Git disaster-recovery capture mixin (A185 split).

Extracted from ``disaster_recovery.py`` (source-size contract): bounded
git plumbing (Tier-1 reads; Tier-2 sanctioned captures only — Tier-3
remains forbidden), recovery-point capture/verify
(refs ``refs/gptbridge/recovery/<id>`` are anchors, not release
authority) and bundle backup/verify/storage-health.  Mixed into
``disaster_recovery.GitDisasterRecovery``.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import TIER3_OPS, classify
from . import audit_chain
from .disaster_recovery_types import (
    BUNDLE_KINDS,
    AUTOMATION_STATE_RELATIVE,
    BundleManifest,
    DisasterRecoveryError,
    RECOVERY_REF_PREFIX,
    RecoveryPoint,
    _sha256_file,
    _sha256_text,
)


class DRCaptureMixin:
    """Recovery-point and bundle capture surface."""

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


__all__ = ["DRCaptureMixin"]
