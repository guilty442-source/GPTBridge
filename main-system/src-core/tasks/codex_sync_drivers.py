"""Codex convergence sync drivers — one module, two governed flows.

``ArchitectureArtifactSyncDriver``：每個 tick 從權威庫
``architecture_diagram_artifact_registry`` 讀 active 列，對檔案做
byte-level SHA-256 比對；content_hash 漂移時產生 request-only
``codex-amendment-request`` 投遞 convergence intake，落地一律經既有
``codex-amendment-intake`` → 五主權稽核 → ``auto_execute`` 管線——
永不直接寫權威庫。

``CodexPinSyncDriver``：讀 PostgreSQL ``authority_state``，與
``shared-layer/release-dependencies.json`` 的 ``governance_references``
pin 比對；漂移時外科式改寫該區塊兩欄位（實作面契約檔，非權威寫入），
落盤由 self-commit 收檔。

Kill switch：各 flow manifest ``enabled=false``（A10）。只經
``AutomationCore.register_flow`` 掛共享 PeriodicScheduler；被拒絕時
不回落私有迴圈。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.codex_sync")

FLOW_PIN = "codex-pin-sync"
FLOW_ARTIFACT = "architecture-artifact-sync"

_MAIN_SYSTEM_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONTRACT = _MAIN_SYSTEM_ROOT.parent / "shared-layer" / "release-dependencies.json"
_INTAKE_DIR = "governance_rule/execution/audit/convergence"

_BLOCK_RE = re.compile(
    r'"governance_references"\s*:\s*\{(?P<body>.*?)\n  \}', re.S
)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class _SyncDriverBase:
    """Shared scheduler/observability scaffolding for sync drivers."""

    FLOW_ID = ""
    _STATE_NAME = ""
    _LOG_NAME = ""

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
        self._state_path = state_path or (
            _MAIN_SYSTEM_ROOT / "runtime" / "state" / f"{self._STATE_NAME}.json"
        )
        self._log_path = (
            _MAIN_SYSTEM_ROOT / "runtime" / "logs" / f"{self._LOG_NAME}.jsonl"
        )
        self._registered = False
        self._last_decision = ""
        self._last_error = ""

    async def start(self) -> dict[str, Any]:
        core = getattr(self.app, "automation_core", None)
        if core is None:
            _logger.warning(
                "%s: no automation core — not starting "
                "(no private loop fallback)",
                self.FLOW_ID,
            )
            return {"status": "no-automation-core"}
        self._registered = core.register_flow(self.FLOW_ID, self.run_once)
        return {
            "status": "registered" if self._registered else "denied",
            "flow": self.FLOW_ID,
        }

    async def stop(self) -> None:
        core = getattr(self.app, "automation_core", None)
        if core is not None and self._registered:
            try:
                core.unregister(self.FLOW_ID)
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
            _logger.warning("%s tick failed: %s", self.FLOW_ID, error)
        self._last_decision = decision
        self._write_state()

    async def _tick_inner(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _state_fields(self) -> dict[str, Any]:
        return {}

    def _audit(self, record: dict[str, Any]) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps({"at": _iso_now(), **record}, ensure_ascii=False)
                    + "\n"
                )
        except OSError:
            pass

    def _write_state(self) -> None:
        # Perf/low-IO: ticks usually repeat the same decision — skip the
        # tmp-write + replace when content (excluding ``written_at``)
        # matches the last write.
        fields = self._state_fields()
        fingerprint = json.dumps(
            {
                "flow": self.FLOW_ID,
                "last_decision": self._last_decision,
                "last_error": self._last_error,
                **fields,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if getattr(self, "_last_state_json", None) == fingerprint:
            return
        payload = {
            "flow": self.FLOW_ID,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            **fields,
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
            "flow": self.FLOW_ID,
            "registered": self._registered,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            **self._state_fields(),
        }


class CodexPinSyncDriver(_SyncDriverBase):
    """Release-contract codex pin follows the live authority."""

    FLOW_ID = FLOW_PIN
    _STATE_NAME = "codex-pin-sync"
    _LOG_NAME = "codex-pin-sync"

    def __init__(self, *args: Any, contract_path: Path | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._contract_path = contract_path or _DEFAULT_CONTRACT
        self._last_pinned = ""

    def _state_fields(self) -> dict[str, Any]:
        return {"last_pinned": self._last_pinned}

    async def _tick_inner(self) -> str:
        return await asyncio.to_thread(self._sync_once)

    def _sync_once(self) -> str:
        from governance_rule.execution.codex_postgresql import (
            authority_state,
        )

        state = authority_state()
        live_version = str(state.get("codex_version") or "")
        live_sha = str(state.get("source_sha256") or "")
        if not live_version or not live_sha:
            return "authority-empty"

        text = self._contract_path.read_text(encoding="utf-8")
        match = _BLOCK_RE.search(text)
        if match is None:
            self._last_error = "governance_references block not found"
            return "contract-block-missing"
        body = match.group("body")
        ver_m = re.search(r'"codex_version"\s*:\s*"([^"]+)"', body)
        sha_m = re.search(r'"codex_sha256"\s*:\s*"([^"]+)"', body)
        if ver_m is None or sha_m is None:
            self._last_error = "codex pin keys not found"
            return "contract-keys-missing"
        pinned_version, pinned_sha = ver_m.group(1), sha_m.group(1)
        self._last_pinned = pinned_version
        if pinned_version == live_version and pinned_sha == live_sha:
            return f"in-sync:{live_version}"

        new_body = body.replace(
            f'"codex_version": "{pinned_version}"',
            f'"codex_version": "{live_version}"',
            1,
        ).replace(
            f'"codex_sha256": "{pinned_sha}"',
            f'"codex_sha256": "{live_sha}"',
            1,
        )
        new_text = text[: match.start("body")] + new_body + text[match.end("body") :]
        tmp = self._contract_path.with_suffix(".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, self._contract_path)
        self._audit(
            {
                "op": "repin",
                "from": pinned_version,
                "to": live_version,
                "sha256": live_sha,
            }
        )
        _logger.info(
            "codex pin synced: %s -> %s", pinned_version, live_version
        )
        return f"repinned:{pinned_version}->{live_version}"


class ArchitectureArtifactSyncDriver(_SyncDriverBase):
    """Periodic governed artifact-hash reconciliation."""

    FLOW_ID = FLOW_ARTIFACT
    _STATE_NAME = "architecture-artifact-sync"
    _LOG_NAME = "architecture-artifact-sync"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_drift: list[dict[str, Any]] = []
        self._last_missing: list[str] = []

    def _state_fields(self) -> dict[str, Any]:
        return {
            "stale": self._last_drift,
            "missing": self._last_missing,
        }

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


__all__ = [
    "ArchitectureArtifactSyncDriver",
    "CodexPinSyncDriver",
    "FLOW_ARTIFACT",
    "FLOW_PIN",
]
