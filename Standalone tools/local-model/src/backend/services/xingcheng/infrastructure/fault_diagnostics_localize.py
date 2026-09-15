from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .fault_diagnostics_data import (
    _LOCATION_ALIASES,
    _MAX_SUSPECTS,
    _UPPER_SNAKE_TOKEN,
)


class FaultDiagnosticsLocalizeMixin:

    def _module_owner_index(self) -> dict[str, dict[str, str]]:
        """A334 module_assignment_registry → module ownership index."""
        if not self.codex_path.is_file():
            return {}
        try:
            rows = self._codex_read(
                ("registry:module_assignment_registry",),
                lambda ctx: ctx.registry("module_assignment_registry"),
            )
            return {
                str(row.get("module_architecture_code") or ""): {
                    "managing_sub_sovereign": str(
                        row.get("managing_sub_sovereign") or ""
                    ),
                    "decision_authority": str(
                        row.get("decision_authority") or ""
                    ),
                }
                for row in rows
                if row.get("status") == "active"
            }
        except (sqlite3.Error, PermissionError, KeyError, TypeError):
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
        aux: dict[str, Any] | None = None,
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
        anomalies = self._evidence_anomalies(evidence, aux)
        ranked, confidence = self._rank_suspects(
            symptom, index, anomalies, matched, manuals
        )

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

    def _rank_suspects(
        self,
        symptom: str,
        index: dict[str, dict[str, str]],
        anomalies: list[dict[str, Any]],
        matched: list[dict[str, Any]],
        manuals: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
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

        self._bump_symptom_mentions(symptom, index, bump)
        self._bump_evidence(anomalies, matched, manuals, bump, suspects)
        return self._finalize_suspects(suspects)

    def _bump_symptom_mentions(
        self,
        symptom: str,
        index: dict[str, dict[str, str]],
        bump: Any,
    ) -> None:
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

    def _bump_evidence(
        self,
        anomalies: list[dict[str, Any]],
        matched: list[dict[str, Any]],
        manuals: list[dict[str, Any]],
        bump: Any,
        suspects: dict[str, dict[str, Any]],
    ) -> None:
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

    @staticmethod
    def _finalize_suspects(
        suspects: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
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
        return ranked, confidence

    def diagnose(self, symptom: str) -> dict[str, Any]:
        """Produce a governed, read-only diagnosis evidence pack.

        The pack grounds Xingcheng's answer in the authoritative fault-code
        directory and governed maintenance manuals, plus bounded runtime
        evidence — never guesses and never executes repairs.
        """
        text = str(symptom or "").strip()[:2_000]
        evidence = self.runtime_state_evidence()
        aux = self._auxiliary_evidence(evidence)
        matched = self.match_fault_codes(text)
        # The backend log tail carries the actual emitted error text —
        # UPPER_SNAKE tokens there are concrete fault-code evidence, not
        # symptom guesses, so they merge into the directory match.
        log_codes = self.match_fault_codes(
            " ".join(self.log_fault_tokens(aux.get("boot_log") or {}))
        )
        # Recurring learned signatures are already concrete fault codes —
        # pull their directory rows directly rather than fuzzy-matching.
        learning = aux.get("repair_learning") or {}
        learned_codes = self.directory_entries_by_code(
            [
                str(s.get("failure_code") or "")
                for s in learning.get("recurring_signatures") or []
                if isinstance(s, dict)
            ]
        )
        for entry in learned_codes:
            entry["match_source"] = "repair-learning"
        matched = self._merge_matches(matched, log_codes + learned_codes)
        manuals = self.manuals_for(
            [entry["fault_code"] for entry in matched]
        )
        events = self.outbox_tail()
        localization = self.localize_fault(
            text, evidence=evidence, matched=matched, manuals=manuals,
            aux=aux,
        )
        return self._diagnosis_pack(
            text, matched, manuals, evidence, events, aux, localization
        )

    def _diagnosis_pack(
        self,
        text: str,
        matched: list[dict[str, Any]],
        manuals: list[dict[str, Any]],
        evidence: dict[str, Any],
        events: list[dict[str, Any]],
        aux: dict[str, Any],
        localization: dict[str, Any],
    ) -> dict[str, Any]:
        learning = aux.get("repair_learning") or {}
        permission_chain = [
            {
                "manual_code": manual["manual_code"],
                "required_permission": manual["required_permission"],
                "stop_conditions": manual["stop_conditions"],
            }
            for manual in manuals
            if manual.get("required_permission")
        ]
        return {
            "ok": True,
            "schema": "xingcheng-fault-diagnosis/v1",
            "symptom": text,
            "localization": localization,
            "matched_fault_codes": matched,
            "matched_fault_code_ids": [m["fault_code"] for m in matched],
            "maintenance_manuals": manuals,
            "permission_chain": permission_chain,
            "evidence_chain": self._diagnosis_evidence_chain(
                matched, manuals, evidence, events
            ),
            "suggested_ordered_steps": [
                self._manual_ordered_step(manual) for manual in manuals
            ],
            "runtime_evidence": evidence,
            "recent_state_events": events,
            "log_errors": (aux.get("boot_log") or {}).get("lines") or [],
            "quarantined_tools": aux.get("quarantined_tools") or [],
            "recurring_fault_signatures": (
                learning.get("recurring_signatures") or []
            ),
            "recent_repair_failures": learning.get("recent_failures") or [],
            "automation_context": self._automation_context(evidence),
            "authority": self._diagnosis_authority(),
        }

    @staticmethod
    def _diagnosis_authority() -> dict[str, Any]:
        return {
            "mode": "diagnosis-read-only",
            "execution": False,
            "repair_owner": "main-system-central-repair",
            "repair_channel": "governed-execution-channel",
            "governance_source_access": "direct-read-only-authoritative",
        }

    def _auxiliary_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]:
        """Auxiliary live evidence pack for anomaly evaluation."""
        pending = evidence.get("pending-actions.json")
        pending_recent = (
            pending.get("recent")
            if isinstance(pending, dict)
            else None
        )
        return {
            "boot_log": self.boot_log_tail(),
            "quarantined_tools": self.quarantined_tools(),
            "repair_learning": self.repair_learning_tail(),
            "pending_actions": pending_recent if isinstance(pending_recent, list) else [],
            "watcher": evidence.get("hot-reload-watcher.json"),
            "startup": evidence.get("startup-generation.json"),
        }

    @staticmethod
    def _merge_matches(
        symptom_matches: list[dict[str, Any]],
        log_matches: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge log-derived directory matches after symptom matches."""
        merged = list(symptom_matches)
        seen = {entry["fault_code"] for entry in merged}
        for entry in log_matches:
            if entry["fault_code"] not in seen:
                merged.append(
                    {**entry, "match_source": entry.get("match_source") or "boot-output.log"}
                )
                seen.add(entry["fault_code"])
        for entry in merged:
            entry.setdefault("match_source", "symptom")
        return merged

    @staticmethod
    def _automation_context(evidence: dict[str, Any]) -> dict[str, Any]:
        """Whether automatic repair/update is frozen — explains why a
        known fault may be waiting rather than self-healing."""
        switches = evidence.get("automation-switches.json")
        if not isinstance(switches, dict) or switches.get("available") is False:
            return {}
        return {
            "automatic_repair_enabled": switches.get("automatic_repair_enabled"),
            "automatic_update_enabled": switches.get("automatic_update_enabled"),
            "pending_awaiting_count": (
                evidence.get("pending-actions.json") or {}
            ).get("awaiting_count", 0)
            if isinstance(evidence.get("pending-actions.json"), dict)
            else 0,
        }

    @staticmethod
    def _diagnosis_evidence_chain(
        matched: list[dict[str, Any]],
        manuals: list[dict[str, Any]],
        evidence: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {"kind": "fault-code", "ref": entry["fault_code"], "source": "fault_code_directory"}
            for entry in matched
        ] + [
            {"kind": "manual", "ref": manual["manual_code"], "source": "maintenance_manual_directory"}
            for manual in manuals
        ] + [
            {
                "kind": "runtime-state",
                "ref": name,
                "source": "runtime_state_evidence",
                "available": bool(payload.get("available", True)),
            }
            for name, payload in evidence.items()
        ] + [
            {
                "kind": "state-event",
                "ref": "recent_state_events",
                "source": "outbox_tail",
                "count": len(events),
            }
        ]

    @staticmethod
    def _manual_ordered_step(manual: dict[str, Any]) -> dict[str, Any]:
        return {
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
