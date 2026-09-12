"""Fault diagnostics — governed read-only evidence collection for 星澄.

Provides Xingcheng with grounded fault-investigation capability:

  - Matches a symptom against the authoritative ``fault_code_directory``
    and ``maintenance_manual_directory`` in the sealed governance codex
    (opened read-only/immutable; xingcheng is a registered read audience).
  - Reads main-system runtime state files (boot core, readiness, IPC
    connection state, repair coordination) as read-only evidence.
  - Reads the A195 transactional outbox tail for recent committed state
    transitions.

Boundary: diagnosis is read-only and advisory.  Repair execution stays
with main-system central repair through the governed channel; Xingcheng
reports findings and governed remediation steps only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Final

_CODEX_RELATIVE: Final[tuple[str, ...]] = (
    "governance_rule", "codex", "data", "governance_codex.sqlite3",
)
_STATE_RELATIVE: Final[tuple[str, ...]] = (
    "main-system", "runtime", "state",
)

# Bounded evidence reads — diagnostics must stay cheap enough to run inside
# a chat turn.
_MAX_STATE_JSON_BYTES: Final[int] = 64_000
_MAX_REPAIR_REQUESTS: Final[int] = 5
_MAX_OUTBOX_EVENTS: Final[int] = 10
_MAX_MATCHED_CODES: Final[int] = 5
_MAX_MANUALS: Final[int] = 4

_FAULT_KEYWORDS: Final[tuple[str, ...]] = (
    "故障", "錯誤", "失敗", "異常", "當機", "斷線", "連不上", "連線中斷",
    "啟動失敗", "無法啟動", "啟動不了", "開不起來", "打不開", "不能動",
    "壞掉", "掛掉", "卡住", "沒有回應", "沒反應", "無回應", "查故障",
    "找錯", "排錯", "除錯", "為什麼", "怎麼壞", "哪裡壞", "出問題",
    "error", "failed", "failure", "crash", "down", "cannot connect",
    "not responding", "timeout", "timed out", "disconnect",
)
_UPPER_SNAKE_TOKEN: Final = re.compile(r"[A-Z][A-Z0-9_]{3,}")

# JSON state files Xingcheng may read as diagnostic evidence
# (other_module_data: read-only per access policy).
_RUNTIME_STATE_FILES: Final[tuple[str, ...]] = (
    "boot-core.json",
    "runtime-readiness.json",
    "ipc-connection-state.json",
    "repair-coordination.json",
    "repair-requests.json",
)

# Keys surfaced per state file — bounded, no raw secrets.  State files
# nest their live payload under a top-level "snapshot" object; extraction
# flattens one level so these names match the actual schema.
_BOOT_CORE_KEYS: Final[tuple[str, ...]] = (
    "pid", "backend_pid", "status", "state", "restarts",
    "restart_count", "last_error", "last_exit", "backend_healthy",
    "updated_at",
)
_READINESS_KEYS: Final[tuple[str, ...]] = (
    "overall_ready", "runtime_state", "backend_runtime_ready",
    "governance_ready", "dependencies_ready", "authenticated_ipc_connected",
    "startup_dead", "updated_at",
)
_IPC_STATE_KEYS: Final[tuple[str, ...]] = (
    "backend_process_alive", "backend_http_healthy", "frontend_connected",
    "overall_state", "consecutive_dead", "probe_count", "last_change_at",
    "updated_at",
)

# ------------------------------------------------------------------
# Fault localization vocabulary
# ------------------------------------------------------------------

_MAX_SUSPECTS: Final[int] = 5

# Governed layer names → symptom aliases.  Aliases stay specific enough
# to avoid over-triggering on generic fault keywords.
_LOCATION_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "ipc-channel": (
        "ipc", "websocket", "socket", "連線通道", "通訊通道",
        "連不上", "連線中斷", "斷線", "無法連線",
    ),
    "main-backend": (
        "main-system", "main.py", "後端", "主系統", "backend",
        "主程式", "母工具",
    ),
    "boot-core": (
        "boot-core", "boot_core", "啟動核心", "開機核心",
    ),
    "startup-pipeline": (
        "startup", "啟動管線", "啟動流程", "啟動失敗",
        "無法啟動", "啟動不了", "開不起來", "打不開",
    ),
    "governance": (
        "governance", "法典", "治理", "codex", "治理層",
    ),
    "frontend": (
        "electron", "視窗", "前端", "畫面", "介面", "視窗關閉",
    ),
}

# runtime-readiness.json flag → governed location entity.
_READINESS_FLAG_LOCATIONS: Final[dict[str, str]] = {
    "governance_ready": "governance",
    "dependencies_ready": "dependency-set",
    "authenticated_ipc_connected": "ipc-channel",
    "backend_runtime_ready": "main-backend",
}

# boot-core status values that are themselves fault evidence.
_BOOT_FAULT_STATUSES: Final[frozenset[str]] = frozenset({
    "backend-restarting",
    "restart-budget-exhausted",
    "startup-phase-blocked",
    "spawn-failed",
    "failed",
    "error",
})


def _read_json_bounded(path: Path) -> Any:
    try:
        raw = path.read_bytes()[:_MAX_STATE_JSON_BYTES]
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


class FaultDiagnostics:
    """Read-only fault-diagnosis evidence collector for Xingcheng."""

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self.codex_path = self.project_root.joinpath(*_CODEX_RELATIVE)
        self.state_dir = self.project_root.joinpath(*_STATE_RELATIVE)

    # ------------------------------------------------------------------
    # Fault-question detection
    # ------------------------------------------------------------------

    @staticmethod
    def looks_like_fault(text: str) -> bool:
        """Heuristic gate: does this question ask about a system fault?"""
        candidate = str(text or "")
        if not candidate.strip():
            return False
        lowered = candidate.casefold()
        return any(keyword in lowered or keyword in candidate
                   for keyword in _FAULT_KEYWORDS)

    # ------------------------------------------------------------------
    # Governed directory reads (read-only codex)
    # ------------------------------------------------------------------

    def _codex_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"file:{self.codex_path.as_posix()}?mode=ro&immutable=1",
            uri=True,
        )

    def fault_code_directory(self) -> list[dict[str, Any]]:
        """All registered fault codes from the authoritative directory."""
        if not self.codex_path.is_file():
            return []
        try:
            with self._codex_connection() as db:
                rows = db.execute(
                    "SELECT fault_code, canonical_name, domain, severity, "
                    "meaning, trigger, retryability, remediation "
                    "FROM fault_code_directory"
                ).fetchall()
            return [
                {
                    "fault_code": str(row[0]),
                    "canonical_name": str(row[1] or ""),
                    "domain": str(row[2] or ""),
                    "severity": str(row[3] or ""),
                    "meaning": str(row[4] or ""),
                    "trigger": str(row[5] or ""),
                    "retryability": str(row[6] or ""),
                    "remediation": str(row[7] or ""),
                }
                for row in rows
            ]
        except sqlite3.Error:
            return []

    def maintenance_manuals(self) -> list[dict[str, Any]]:
        """All governed maintenance-manual rows."""
        if not self.codex_path.is_file():
            return []
        try:
            with self._codex_connection() as db:
                rows = db.execute(
                    "SELECT manual_code, target_entity, applicable_fault_codes, "
                    "diagnosis, preconditions, ordered_steps, verification, "
                    "rollback, stop_conditions, risk_level, required_permission "
                    "FROM maintenance_manual_directory"
                ).fetchall()
            return [
                {
                    "manual_code": str(row[0]),
                    "target_entity": str(row[1] or ""),
                    "applicable_fault_codes": [
                        code
                        for code in str(row[2] or "").split("|")
                        if code
                    ],
                    "diagnosis": str(row[3] or ""),
                    "preconditions": str(row[4] or ""),
                    "ordered_steps": str(row[5] or ""),
                    "verification": str(row[6] or ""),
                    "rollback": str(row[7] or ""),
                    "stop_conditions": str(row[8] or ""),
                    "risk_level": str(row[9] or ""),
                    "required_permission": str(row[10] or ""),
                }
                for row in rows
            ]
        except sqlite3.Error:
            return []

    # ------------------------------------------------------------------
    # Symptom → fault-code matching
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Runtime evidence (read-only state files + outbox tail)
    # ------------------------------------------------------------------

    def runtime_state_evidence(self) -> dict[str, Any]:
        """Bounded snapshots of main-system runtime state files."""
        evidence: dict[str, Any] = {}
        for name in _RUNTIME_STATE_FILES:
            path = self.state_dir / name
            if not path.is_file():
                evidence[name] = {"available": False}
                continue
            data = _read_json_bounded(path)
            if data is None:
                evidence[name] = {"available": False, "error": "unreadable"}
                continue
            # Live state payloads nest under "snapshot" — flatten one level
            # so key whitelists match the actual schema.
            if isinstance(data, dict) and isinstance(data.get("snapshot"), dict):
                data = {**data["snapshot"], "updated_at": data.get("updated_at")}
            if name == "boot-core.json" and isinstance(data, dict):
                evidence[name] = {
                    key: data[key] for key in _BOOT_CORE_KEYS if key in data
                } or {"available": True}
            elif name == "runtime-readiness.json" and isinstance(data, dict):
                evidence[name] = {
                    key: data[key] for key in _READINESS_KEYS if key in data
                } or {"available": True}
            elif name == "ipc-connection-state.json" and isinstance(data, dict):
                evidence[name] = {
                    key: data[key] for key in _IPC_STATE_KEYS if key in data
                } or {"available": True}
            elif name == "repair-requests.json":
                if isinstance(data, list):
                    evidence[name] = {"recent": data[-_MAX_REPAIR_REQUESTS:]}
                else:
                    evidence[name] = data
            else:
                evidence[name] = data if isinstance(data, dict) else {
                    "available": True
                }
        return evidence

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

    # ------------------------------------------------------------------
    # Fault localization
    # ------------------------------------------------------------------

    def _module_owner_index(self) -> dict[str, dict[str, str]]:
        """A334 module_assignment_registry → module ownership index."""
        if not self.codex_path.is_file():
            return {}
        try:
            with self._codex_connection() as db:
                rows = db.execute(
                    "SELECT module_architecture_code, managing_sub_sovereign, "
                    "decision_authority FROM module_assignment_registry "
                    "WHERE status = 'active'"
                ).fetchall()
            return {
                str(row[0]): {
                    "managing_sub_sovereign": str(row[1] or ""),
                    "decision_authority": str(row[2] or ""),
                }
                for row in rows
            }
        except sqlite3.Error:
            return {}

    def _symptom_entities(self, text: str) -> dict[str, list[str]]:
        """Extract governed location entities mentioned in the symptom."""
        lowered = str(text or "").casefold()
        found: dict[str, list[str]] = {}
        for entity, aliases in _LOCATION_ALIASES.items():
            hits = [alias for alias in aliases if alias.casefold() in lowered]
            if hits:
                found[entity] = hits
        return found

    def _module_mentions(
        self, text: str, index: dict[str, dict[str, str]]
    ) -> dict[str, str]:
        """UPPER_SNAKE tokens in the symptom that are registered modules."""
        tokens = set(_UPPER_SNAKE_TOKEN.findall(str(text or "")))
        return {token: token for token in tokens if token in index}

    def _evidence_anomalies(
        self, evidence: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Evaluate live runtime evidence for abnormal signals.

        Each anomaly carries the governed entity it localizes to and a
        weight reflecting how directly it indicates a current fault.
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

        readiness = evidence.get("runtime-readiness.json")
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

        boot = evidence.get("boot-core.json")
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

        ipc = evidence.get("ipc-connection-state.json")
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

        requests = evidence.get("repair-requests.json")
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
                            f"repair[{status or 'pending'}] → {entity}",
                            entity, weight)

        return anomalies

    @staticmethod
    def _entity_for_domain(domain: str) -> str | None:
        """Map a fault-code domain string to a governed location entity."""
        lowered = str(domain or "").casefold()
        if not lowered:
            return None
        for entity, aliases in _LOCATION_ALIASES.items():
            if lowered == entity or any(
                lowered == alias.casefold() for alias in aliases
            ):
                return entity
        return None

    def localize_fault(
        self,
        symptom: str,
        *,
        evidence: dict[str, Any] | None = None,
        matched: list[dict[str, Any]] | None = None,
        manuals: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Rank governed suspect locations for a fault symptom.

        Combines four evidence classes into a scored suspect list:

          1. symptom entity mentions (aliases + registered module codes)
          2. live runtime anomalies (readiness/boot-core/IPC/repair state)
          3. matched fault-code domains
          4. maintenance-manual target entities

        Module mentions are resolved through A334
        ``module_assignment_registry`` to their managing sub-sovereign —
        localizing not just *where* the fault is but *which governed
        owner* is responsible for that module.
        """
        if evidence is None:
            evidence = self.runtime_state_evidence()
        if matched is None:
            matched = self.match_fault_codes(str(symptom or ""))
        if manuals is None:
            manuals = self.manuals_for(
                [entry["fault_code"] for entry in matched]
            )

        index = self._module_owner_index()
        anomalies = self._evidence_anomalies(evidence)
        suspects: dict[str, dict[str, Any]] = {}

        def bump(
            entity: str,
            weight: float,
            source: str,
            signal: str,
        ) -> dict[str, Any]:
            slot = suspects.setdefault(
                entity,
                {"entity": entity, "score": 0.0, "evidence": []},
            )
            slot["score"] += weight
            slot["evidence"].append({"source": source, "signal": signal})
            return slot

        # 1. Symptom entity mentions.
        for entity, hits in self._symptom_entities(symptom).items():
            bump(entity, 4.0, "symptom", f"entity-mentioned: {hits[0]}")
        # Registered module codes resolve to their A334 owner.
        for module_code in self._module_mentions(symptom, index):
            owner = index.get(module_code, {})
            slot = bump(
                f"module:{module_code}", 5.0, "symptom",
                f"module-mentioned: {module_code}",
            )
            slot["managing_sub_sovereign"] = owner.get(
                "managing_sub_sovereign", ""
            )
            slot["decision_authority"] = owner.get("decision_authority", "")

        # 2. Live anomalies — strongest signal: something is wrong NOW.
        for anomaly in anomalies:
            bump(
                str(anomaly["entity"]), float(anomaly["weight"]),
                str(anomaly["source"]), str(anomaly["signal"]),
            )

        # 3. Matched fault-code domains.
        for entry in matched:
            entity = self._entity_for_domain(str(entry.get("domain") or ""))
            if entity is None:
                continue
            bump(entity, 2.0, "fault-code-directory",
                 f"{entry['fault_code']} domain={entry.get('domain')}")
            suspects[entity].setdefault("matched_fault_codes", []).append(
                entry["fault_code"]
            )

        # 4. Manual target entities.
        for manual in manuals:
            target = str(manual.get("target_entity") or "").strip()
            if not target:
                continue
            slot = bump(target, 1.5, "maintenance-manual",
                        f"{manual['manual_code']} target={target}")
            slot.setdefault("manual_codes", []).append(manual["manual_code"])

        ranked = sorted(
            suspects.values(),
            key=lambda item: (-item["score"], item["entity"]),
        )[:_MAX_SUSPECTS]
        for slot in ranked:
            slot["score"] = round(slot["score"], 2)
            slot["evidence"] = slot["evidence"][:6]

        top_score = ranked[0]["score"] if ranked else 0.0
        if top_score >= 8.0:
            confidence = "high"
        elif top_score >= 4.0:
            confidence = "medium"
        elif ranked:
            confidence = "low"
        else:
            confidence = "none"

        primary = ranked[0] if ranked else None
        return {
            "primary_suspect": (
                {
                    "entity": primary["entity"],
                    "score": primary["score"],
                    "confidence": confidence,
                }
                if primary
                else None
            ),
            "suspect_locations": ranked,
            "anomalies": anomalies,
            "confidence": confidence,
        }

    # ------------------------------------------------------------------
    # Full diagnosis
    # ------------------------------------------------------------------

    def diagnose(self, symptom: str) -> dict[str, Any]:
        """Produce a governed, read-only diagnosis evidence pack.

        The pack grounds Xingcheng's answer in the authoritative fault-code
        directory and governed maintenance manuals, plus bounded runtime
        evidence — never guesses and never executes repairs.
        """
        text = str(symptom or "").strip()[:2_000]
        matched = self.match_fault_codes(text)
        manuals = self.manuals_for(
            [entry["fault_code"] for entry in matched]
        )
        evidence = self.runtime_state_evidence()
        localization = self.localize_fault(
            text, evidence=evidence, matched=matched, manuals=manuals
        )
        return {
            "ok": True,
            "schema": "xingcheng-fault-diagnosis/v1",
            "symptom": text,
            "localization": localization,
            "matched_fault_codes": matched,
            "matched_fault_code_ids": [m["fault_code"] for m in matched],
            "maintenance_manuals": manuals,
            "suggested_ordered_steps": [
                {
                    "manual_code": manual["manual_code"],
                    "target_entity": manual["target_entity"],
                    "diagnosis": manual["diagnosis"],
                    "preconditions": manual["preconditions"],
                    "ordered_steps": manual["ordered_steps"],
                    "verification": manual["verification"],
                    "stop_conditions": manual["stop_conditions"],
                    "risk_level": manual["risk_level"],
                    "required_permission": manual["required_permission"],
                }
                for manual in manuals
            ],
            "runtime_evidence": evidence,
            "recent_state_events": self.outbox_tail(),
            "authority": {
                "mode": "diagnosis-read-only",
                "execution": False,
                "repair_owner": "main-system-central-repair",
                "repair_channel": "governed-execution-channel",
                "governance_source_access": "direct-read-only-authoritative",
            },
        }


__all__ = ["FaultDiagnostics"]
