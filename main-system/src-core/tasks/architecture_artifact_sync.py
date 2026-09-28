"""Architecture artifact registry sync driver — governed drift/amend loop.

每個 tick 從權威庫 ``architecture_diagram_artifact_registry`` 讀 active
列，對 ``artifact_path`` 檔案做 byte-level SHA-256 比對；content_hash
或 source_codex_version 漂移時產生 ``codex-amendment-request``（每列
一個 ``update`` action）投遞 convergence intake。落地一律經既有
``codex-amendment-intake`` → 五主權稽核 → ``auto_execute`` 管線——
本 driver 永不直接寫權威庫（同 package-version-sync 模式）。

檔案缺失的 active 列只記入 state（retire 決策屬 governor），不產生
修正案。Kill switch：flow manifest ``enabled=false``（A10）。只經
``AutomationCore.register_flow`` 掛共享 PeriodicScheduler；被拒絕時
不回落私有迴圈。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.architecture_artifact_sync")

FLOW_ID = "architecture-artifact-sync"

_MAIN_SYSTEM_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STATE = (
    _MAIN_SYSTEM_ROOT / "runtime" / "state" / "architecture-artifact-sync.json"
)
_DEFAULT_LOG = (
    _MAIN_SYSTEM_ROOT / "runtime" / "logs" / "architecture-artifact-sync.jsonl"
)
_INTAKE_DIR = "governance_rule/execution/audit/convergence"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ArchitectureArtifactSyncDriver:
    """Periodic governed artifact-hash reconciliation."""

    def __init__(
        self,
        app: Any,
        toolbox_service: Any = None,
        *,
        project_root: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.app = app
        self.toolbox = toolbox_service
        self._project_root = Path(
            project_root or getattr(app, "project_root", "")
        ).resolve()
        self._state_path = state_path or _DEFAULT_STATE
        self._registered = False
        self._last_decision = ""
        self._last_error = ""
        self._last_drift: list[dict[str, Any]] = []
        self._last_missing: list[str] = []

    async def start(self) -> dict[str, Any]:
        core = getattr(self.app, "automation_core", None)
        if core is None:
            _logger.warning(
                "architecture-artifact-sync: no automation core — not "
                "starting (no private loop fallback)"
            )
            return {"status": "no-automation-core"}
        self._registered = core.register_flow(FLOW_ID, self.run_once)
        return {
            "status": "registered" if self._registered else "denied",
            "flow": FLOW_ID,
        }

    async def stop(self) -> None:
        core = getattr(self.app, "automation_core", None)
        if core is not None and self._registered:
            try:
                core.unregister(FLOW_ID)
            except Exception:
                pass
        self._registered = False

    async def run_once(self) -> None:
        """One scheduler tick — never raises into the shared loop."""
        try:
            decision = await self._tick_inner()
            self._last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — audit then isolate
            decision = "error"
            self._last_error = f"{type(error).__name__}: {error}"
            _logger.warning("architecture-artifact-sync tick failed: %s", error)
        self._last_decision = decision
        self._write_state()

    async def _tick_inner(self) -> str:
        drift = await asyncio.to_thread(self._detect_drift)
        self._last_drift = drift["stale"]
        self._last_missing = drift["missing"]
        emitted = ""
        if drift["stale"]:
            emitted = await asyncio.to_thread(
                self._emit_amendment, drift["stale"], drift["meta"]
            )
        return (
            f"active:{drift['active']} stale:{len(drift['stale'])} "
            f"missing:{len(drift['missing'])} {emitted or 'no-amendment'}"
        )

    # ------------------------------------------------------------------
    # drift detection (worker thread)
    # ------------------------------------------------------------------

    def _detect_drift(self) -> dict[str, Any]:
        from governance_rule.execution.codex_repository import (
            codex_readonly_connection,
        )

        with codex_readonly_connection() as conn:
            rows = conn.execute(
                "SELECT artifact_path, content_hash, source_codex_version, "
                "sync_status FROM architecture_diagram_artifact_registry "
                "WHERE status = 'active' ORDER BY artifact_path"
            ).fetchall()
            meta = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT key, value FROM metadata WHERE key IN "
                    "('codex_version','current_version_identity',"
                    "'current_version_epoch')"
                )
            }
            seq, head = conn.execute(
                "SELECT sequence, entry_hash FROM revision_history "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        codex_version = str(meta.get("codex_version") or "")
        stale: list[dict[str, Any]] = []
        missing: list[str] = []
        for path, registered_hash, source_version, sync_status in rows:
            target = self._project_root / str(path)
            if not target.is_file():
                missing.append(str(path))
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            # Drift = content_hash mismatch only.  source_codex_version is
            # the generation the artifact was built against; rebinding it
            # on every codex advance (when bytes are unchanged) would make
            # each execution bump the version → next tick stale again —
            # a self-perpetuating amendment loop.  Hash drift alone
            # invalidates SYNCED_CURRENT; version lag is informational.
            if actual == registered_hash:
                continue
            stale.append(
                {
                    "artifact_path": str(path),
                    "content_hash": actual,
                    "registered_hash": str(registered_hash or ""),
                    "source_codex_version": codex_version,
                    "registered_version": str(source_version or ""),
                    "sync_status": str(sync_status or ""),
                }
            )
        return {
            "stale": stale,
            "missing": missing,
            "active": len(rows),
            "meta": {
                "codex_version": codex_version,
                "version_identity": meta.get("current_version_identity"),
                "version_epoch": int(meta.get("current_version_epoch") or 0),
                "history_head": head,
                "revision_sequence": seq,
            },
        }

    # ------------------------------------------------------------------
    # codex drift → amendment emission (never direct writes)
    # ------------------------------------------------------------------

    def _emit_amendment(
        self, stale: list[dict[str, Any]], meta: dict[str, Any]
    ) -> str:
        date = time.strftime("%Y%m%d", time.gmtime())
        request_id = f"architecture-artifact-sync-{date}"
        intake = self._project_root / _INTAKE_DIR
        intake.mkdir(parents=True, exist_ok=True)
        # in-flight dedupe: identical drift already queued → skip
        suffix = ""
        for n in range(1, 9):
            candidate = intake / (
                f"codex-amendment-request-{request_id}{suffix}.json"
            )
            if not candidate.exists():
                break
            try:
                prior = json.loads(candidate.read_text(encoding="utf-8"))
                prior_hashes = [
                    (s.get("set") or {}).get("content_hash")
                    for s in prior.get("proposed_successors") or []
                    if isinstance(s, dict)
                ]
                if prior_hashes == [d["content_hash"] for d in stale]:
                    return "amendment-already-queued"
            except (OSError, json.JSONDecodeError):
                pass
            suffix = f"-r{n + 1}"
        else:
            return "amendment-queue-saturated"

        request = {
            "artifact": "codex-amendment-request",
            "authority": "request-only",
            "schema": "codex-amendment-request/v1",
            "request_id": f"{request_id}{suffix}",
            "title": (
                "Rebind architecture_diagram_artifact_registry to "
                "regenerated artifacts"
            ),
            "summary": (
                "Automation architecture-artifact-sync detected "
                f"{len(stale)} active artifact rows whose registered "
                "content_hash no longer matches the on-disk file bytes: "
                + ", ".join(d["artifact_path"] for d in stale)
            ),
            "requested_by": "automation-sovereign",
            "origin": (
                "architecture-artifact-sync driver drift detection "
                "(registry SYNCED_CURRENT must match artifact bytes)"
            ),
            "change_class": "clarification",
            "required_review": "five-sovereign-audit-unanimous-pass",
            "flow": "A382/A488-non-disruptive-amendment-flow",
            "not_executed": True,
            "auto_execute": True,
            "predecessor": {
                "codex_version": meta["codex_version"],
                "version_identity": meta["version_identity"],
                "version_epoch": meta["version_epoch"],
                "history_head": meta["history_head"],
                "revision_sequence": meta["revision_sequence"],
            },
            "problem": {
                "summary": (
                    "active registry rows pin stale content_hash / "
                    "source_codex_version; SYNCED_CURRENT no longer "
                    "evidences current sync"
                )
            },
            "proposed_successors": [
                {
                    "registry": "architecture_diagram_artifact_registry",
                    "action": "update",
                    "key": {"artifact_path": d["artifact_path"]},
                    "set": {
                        "content_hash": d["content_hash"],
                        "source_codex_version": d["source_codex_version"],
                        "validated_at_utc": _iso_now(),
                        "sync_status": "SYNCED_CURRENT",
                    },
                }
                for d in stale
            ],
            "verification": {
                "requested_at": _iso_now(),
                "expected_audit_result": (
                    "active rows carry content_hash matching live "
                    "artifact bytes and the current codex version"
                ),
            },
        }
        candidate.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._audit(
            {
                "op": "emit-amendment",
                "request_id": f"{request_id}{suffix}",
                "artifacts": [d["artifact_path"] for d in stale],
            }
        )
        _logger.info(
            "artifact-registry drift amendment emitted: %s (%d rows)",
            f"{request_id}{suffix}",
            len(stale),
        )
        return f"amendment:{request_id}{suffix}"

    # ------------------------------------------------------------------
    # observability
    # ------------------------------------------------------------------

    def _audit(self, record: dict[str, Any]) -> None:
        try:
            _DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _DEFAULT_LOG.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"at": _iso_now(), **record}, ensure_ascii=False
                    )
                    + "\n"
                )
        except OSError:
            pass

    def _write_state(self) -> None:
        # Perf/low-IO: ticks usually repeat the same decision — skip the
        # tmp-write + replace when content (excluding ``written_at``)
        # matches the last write.
        fingerprint = json.dumps(
            {
                "flow": FLOW_ID,
                "last_decision": self._last_decision,
                "last_error": self._last_error,
                "stale": self._last_drift,
                "missing": self._last_missing,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if getattr(self, "_last_state_json", None) == fingerprint:
            return
        payload = {
            "flow": FLOW_ID,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            "stale": self._last_drift,
            "missing": self._last_missing,
            "written_at": _iso_now(),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)
            self._last_state_json = fingerprint
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "flow": FLOW_ID,
            "registered": self._registered,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            "stale": list(self._last_drift),
            "missing": list(self._last_missing),
        }


__all__ = ["ArchitectureArtifactSyncDriver", "FLOW_ID"]
