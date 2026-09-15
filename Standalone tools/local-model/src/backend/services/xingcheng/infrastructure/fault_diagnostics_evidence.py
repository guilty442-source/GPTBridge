from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .fault_diagnostics_data import (
    _BOOT_CORE_KEYS,
    _BOOT_FAULT_STATUSES,
    _IPC_STATE_KEYS,
    _MAX_MANUALS,
    _MAX_MATCHED_CODES,
    _MAX_OUTBOX_EVENTS,
    _MAX_PENDING_ACTIONS,
    _MAX_REPAIR_REQUESTS,
    _PENDING_ACTION_KEYS,
    _READINESS_FLAG_LOCATIONS,
    _READINESS_KEYS,
    _RUNTIME_STATE_FILES,
    _STATE_KEY_WHITELISTS,
    _SWITCH_KEYS,
    _UPPER_SNAKE_TOKEN,
    _WATCHER_KEYS,
    _read_json_bounded,
)


class FaultDiagnosticsEvidenceMixin:

    def _codex_read(
        self,
        scope: tuple[str, ...],
        reader: "Any",
    ) -> Any:
        """One bounded official-entry codex read (A435).

        Direct SQLite access to the codex is denied: fault diagnostics
        enters through ``governance-codex://official`` as the governed
        ``xingcheng-fault-diagnostics`` actor with purpose ``diagnostics``
        and a least scope of registered non-content directories/registries.
        """
        from governance_rule.execution.codex_reconcile import bounded_lookup

        return bounded_lookup(
            "xingcheng-fault-diagnostics",
            purpose="diagnostics",
            scope=scope,
            reader=reader,
        )

    def fault_code_directory(self) -> list[dict[str, Any]]:
        """All registered fault codes from the authoritative directory."""
        if not self.codex_path.is_file():
            return []
        try:
            rows = self._codex_read(
                ("directory:fault_code_directory",),
                lambda ctx: ctx.directory("fault_code_directory"),
            )
            return [
                {
                    "fault_code": str(row.get("fault_code") or ""),
                    "canonical_name": str(row.get("canonical_name") or ""),
                    "domain": str(row.get("domain") or ""),
                    "severity": str(row.get("severity") or ""),
                    "meaning": str(row.get("meaning") or ""),
                    "trigger": str(row.get("trigger") or ""),
                    "retryability": str(row.get("retryability") or ""),
                    "remediation": str(row.get("remediation") or ""),
                }
                for row in rows
            ]
        except (sqlite3.Error, PermissionError, KeyError, TypeError):
            return []

    def maintenance_manuals(self) -> list[dict[str, Any]]:
        """All governed maintenance-manual rows."""
        if not self.codex_path.is_file():
            return []
        try:
            rows = self._codex_read(
                ("directory:maintenance_manual_directory",),
                lambda ctx: ctx.directory("maintenance_manual_directory"),
            )
            return [
                {
                    "manual_code": str(row.get("manual_code") or ""),
                    "target_entity": str(row.get("target_entity") or ""),
                    "applicable_fault_codes": [
                        code
                        for code in str(
                            row.get("applicable_fault_codes") or ""
                        ).split("|")
                        if code
                    ],
                    "diagnosis": str(row.get("diagnosis") or ""),
                    "preconditions": str(row.get("preconditions") or ""),
                    "ordered_steps": str(row.get("ordered_steps") or ""),
                    "verification": str(row.get("verification") or ""),
                    "rollback": str(row.get("rollback") or ""),
                    "stop_conditions": str(row.get("stop_conditions") or ""),
                    "risk_level": str(row.get("risk_level") or ""),
                    "required_permission": str(
                        row.get("required_permission") or ""
                    ),
                }
                for row in rows
            ]
        except (sqlite3.Error, PermissionError, KeyError, TypeError):
            return []

    def match_fault_codes(
        self,
        symptom: str,
        *,
        limit: int = _MAX_MATCHED_CODES,
    ) -> list[dict[str, Any]]:
        """Score directory rows against a free-text symptom description."""
        directory = self.fault_code_directory()
        if not directory or not str(symptom or "").strip():
            return []
        text = str(symptom)
        lowered = text.casefold()
        upper_tokens = set(_UPPER_SNAKE_TOKEN.findall(text))
        # Word-ish tokens for substring matching against canonical names
        # and trigger descriptions.
        text_tokens = {
            token
            for token in re.split(r"[^0-9A-Za-z一-鿿]+", lowered)
            if len(token) >= 2
        }

        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in directory:
            score = 0.0
            if entry["fault_code"] in upper_tokens:
                score += 10.0
            canonical_words = set(entry["canonical_name"].split("-"))
            score += 1.5 * len(canonical_words & text_tokens)
            for field in ("meaning", "trigger", "domain"):
                value = str(entry[field]).casefold()
                if not value:
                    continue
                if value in lowered:
                    score += 2.0
                    continue
                score += 0.5 * len(
                    {tok for tok in value.split() if tok} & text_tokens
                )
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1]["fault_code"]))
        return [entry for _score, entry in scored[: max(1, limit)]]

    def directory_entries_by_code(
        self, fault_codes: list[str]
    ) -> list[dict[str, Any]]:
        """Directory rows for exact fault codes (e.g. learned signatures)."""
        wanted = {str(code).strip() for code in fault_codes if str(code).strip()}
        if not wanted:
            return []
        return [
            entry for entry in self.fault_code_directory()
            if entry["fault_code"] in wanted
        ]

    def manuals_for(
        self,
        fault_codes: list[str],
        *,
        limit: int = _MAX_MANUALS,
    ) -> list[dict[str, Any]]:
        """Maintenance-manual rows whose applicable codes intersect."""
        wanted = {str(code) for code in fault_codes if str(code).strip()}
        if not wanted:
            return []
        return [
            manual
            for manual in self.maintenance_manuals()
            if wanted & set(manual["applicable_fault_codes"])
        ][: max(1, limit)]

    def runtime_state_evidence(self) -> dict[str, Any]:
        """Bounded snapshots of main-system runtime state files."""
        return {
            name: self._state_file_snapshot(name)
            for name in _RUNTIME_STATE_FILES
        }

    def _state_file_snapshot(self, name: str) -> Any:
        path = self.state_dir / name
        if not path.is_file():
            return {"available": False}
        data = _read_json_bounded(path)
        if data is None:
            return {"available": False, "error": "unreadable"}
        # Live state payloads nest under "snapshot" — flatten one level
        # so key whitelists match the actual schema.
        if isinstance(data, dict) and isinstance(data.get("snapshot"), dict):
            data = {**data["snapshot"], "updated_at": data.get("updated_at")}
        return self._project_state_payload(name, data)

    def _project_state_payload(self, name: str, data: Any) -> Any:
        if name == "repair-requests.json":
            if isinstance(data, list):
                return {"recent": data[-_MAX_REPAIR_REQUESTS:]}
            return data
        if name == "pending-actions.json" and isinstance(data, list):
            recent = [
                self._pending_action_projection(item)
                for item in data
                if isinstance(item, dict)
            ][-_MAX_PENDING_ACTIONS:]
            return {
                "recent": recent,
                "awaiting_count": sum(
                    1
                    for item in recent
                    if item.get("status") == "awaiting-confirmation"
                ),
            }
        if name == "startup-generation.json" and isinstance(data, dict):
            return self._startup_projection(data)
        keys = _STATE_KEY_WHITELISTS.get(name)
        if keys and isinstance(data, dict):
            return {key: data[key] for key in keys if key in data} or {
                "available": True
            }
        return data if isinstance(data, dict) else {"available": True}

    @staticmethod
    def _pending_action_projection(item: dict[str, Any]) -> dict[str, Any]:
        projected = {
            key: item[key] for key in _PENDING_ACTION_KEYS if key in item
        }
        detail = item.get("detail")
        if isinstance(detail, dict):
            projected["detail"] = {
                key: detail[key]
                for key in ("failure_code", "owner")
                if key in detail
            }
        return projected

    @staticmethod
    def _startup_projection(data: dict[str, Any]) -> dict[str, Any]:
        """Project startup-generation state to the fault-relevant fields."""
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        phases = result.get("phases")
        failed_phases = [
            str(phase.get("phase_id") or "")
            for phase in phases
            if isinstance(phase, dict) and phase.get("ok") is False
        ] if isinstance(phases, list) else []
        return {
            "role": str(data.get("role") or ""),
            "generation_id": str(result.get("generation_id") or ""),
            "ok": result.get("ok"),
            "failed_phases": failed_phases,
            "updated_at": data.get("updated_at"),
        }

    def outbox_tail(
        self,
        *,
        limit: int = _MAX_OUTBOX_EVENTS,
    ) -> list[dict[str, Any]]:
        """Recent committed outbox events (A195) — newest first."""
        path = self.state_dir / "state-outbox.sqlite3"
        if not path.is_file():
            return []
        try:
            db = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True
            )
            try:
                rows = db.execute(
                    "SELECT sequence, entity_type, entity_id, operation, "
                    "authoritative_revision, committed_at "
                    "FROM outbox_events ORDER BY sequence DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            finally:
                db.close()
            return [
                {
                    "sequence": int(row[0]),
                    "entity_type": str(row[1] or ""),
                    "entity_id": str(row[2] or ""),
                    "operation": str(row[3] or ""),
                    "authoritative_revision": int(row[4] or 0),
                    "committed_at": str(row[5] or ""),
                }
                for row in rows
            ]
        except sqlite3.Error:
            return []

    def _evidence_anomalies(
        self,
        evidence: dict[str, Any],
        aux: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate live runtime evidence for abnormal signals.

        Each anomaly carries the governed entity it localizes to and a
        weight reflecting how directly it indicates a current fault.
        ``aux`` carries the auxiliary evidence pack produced by
        :meth:`auxiliary_evidence` (pending actions, watcher, startup,
        quarantine, repair-learning).
        """
        anomalies: list[dict[str, Any]] = []

        def add(source: str, signal: str, entity: str, weight: float) -> None:
            anomalies.append(
                {
                    "source": source,
                    "signal": signal,
                    "entity": entity,
                    "weight": weight,
                }
            )

        self._readiness_anomalies(evidence.get("runtime-readiness.json"), add)
        self._boot_anomalies(evidence.get("boot-core.json"), add)
        self._ipc_anomalies(evidence.get("ipc-connection-state.json"), add)
        self._repair_request_anomalies(
            evidence.get("repair-requests.json"), add
        )
        if aux:
            self._aux_anomalies(aux, add)
        return anomalies

    @staticmethod
    def _readiness_anomalies(readiness: Any, add: Any) -> None:
        if isinstance(readiness, dict) and readiness.get("available", True) is not False:
            if readiness.get("startup_dead") is True:
                add("runtime-readiness.json", "startup_dead=true",
                    "startup-pipeline", 6.0)
            if readiness.get("overall_ready") is False:
                add("runtime-readiness.json", "overall_ready=false",
                    "main-system", 2.0)
                for flag, entity in _READINESS_FLAG_LOCATIONS.items():
                    if readiness.get(flag) is False:
                        add("runtime-readiness.json", f"{flag}=false",
                            entity, 5.0)
        elif isinstance(readiness, dict) and readiness.get("available") is False:
            add("runtime-readiness.json", "readiness-state-unavailable",
                "main-backend", 1.5)

    @staticmethod
    def _boot_anomalies(boot: Any, add: Any) -> None:
        if isinstance(boot, dict) and boot and boot.get("available", True) is not False:
            status = str(boot.get("status") or boot.get("state") or "")
            if status in _BOOT_FAULT_STATUSES:
                add("boot-core.json", f"boot_status={status}",
                    "main-backend", 6.0)
            elif status in ("stopped", "backend-stopped-clean"):
                add("boot-core.json", f"boot_status={status}",
                    "main-backend", 3.0)
            try:
                restarts = int(boot.get("restarts") or boot.get("restart_count") or 0)
            except (TypeError, ValueError):
                restarts = 0
            if restarts > 0:
                add("boot-core.json", f"restarts={restarts}",
                    "main-backend", min(2.0 + 0.5 * restarts, 5.0))
            if boot.get("backend_healthy") is False:
                add("boot-core.json", "backend_healthy=false",
                    "main-backend", 4.5)
            last_error = str(boot.get("last_error") or "").strip()
            if last_error:
                add("boot-core.json", f"last_error={last_error[:120]}",
                    "main-backend", 4.0)
            last_exit = boot.get("last_exit")
            if isinstance(last_exit, dict):
                try:
                    exit_code = int(last_exit.get("code") or 0)
                except (TypeError, ValueError):
                    exit_code = 0
                if exit_code != 0:
                    reason = str(last_exit.get("reason") or "nonzero-exit")
                    add("boot-core.json",
                        f"last_exit.code={exit_code} ({reason[:80]})",
                        "main-backend", 2.5)
        elif isinstance(boot, dict) and boot.get("available") is False:
            add("boot-core.json", "boot-state-unavailable",
                "main-backend", 1.5)

    @staticmethod
    def _ipc_anomalies(ipc: Any, add: Any) -> None:
        if isinstance(ipc, dict) and ipc and ipc.get("available", True) is not False:
            overall = str(ipc.get("overall_state") or "").casefold()
            if overall and overall not in ("connected", "healthy", "ready"):
                add("ipc-connection-state.json",
                    f"overall_state={overall}", "ipc-channel", 5.0)
            if ipc.get("frontend_connected") is False:
                add("ipc-connection-state.json", "frontend_connected=false",
                    "ipc-channel", 4.0)
            if ipc.get("backend_process_alive") is False:
                add("ipc-connection-state.json",
                    "backend_process_alive=false", "main-backend", 5.0)
            if ipc.get("backend_http_healthy") is False:
                add("ipc-connection-state.json",
                    "backend_http_healthy=false", "main-backend", 4.0)
            try:
                dead = int(ipc.get("consecutive_dead") or 0)
            except (TypeError, ValueError):
                dead = 0
            if dead > 0:
                add("ipc-connection-state.json",
                    f"consecutive_dead={dead}", "ipc-channel",
                    min(2.0 + 0.5 * dead, 5.0))

    @staticmethod
    def _repair_request_anomalies(requests: Any, add: Any) -> None:
        if isinstance(requests, dict):
            recent = requests.get("recent")
            if isinstance(recent, list):
                for req in recent[:_MAX_REPAIR_REQUESTS]:
                    if isinstance(req, dict):
                        entity = str(
                            req.get("target_entity") or "repair-target"
                        )
                        status = str(req.get("status") or "").casefold()
                        live = status in (
                            "", "pending", "in_progress", "queued",
                            "requested", "open",
                        )
                        weight = 3.0 if live else 1.0
                        add("repair-requests.json",
                            f"repair[{status or 'pending'}] \u2192 {entity}",
                            entity, weight)
