from __future__ import annotations

import hashlib
import os
import pprint
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .module_registry import StarModuleRegistry


class StarCapabilityComposer:
    """Build governed capability blueprints from Star's existing modules."""

    ALLOWED_KINDS = frozenset({"workflow", "module", "specialist"})
    ALLOWED_INTENTS = frozenset(
        {
            "analysis",
            "calculation",
            "capabilities",
            "coding",
            "data_organization",
            "reading",
            "reasoning",
            "risk",
            "search",
            "self_upgrade",
            "statistics",
        }
    )
    KEYWORD_INTENTS = {
        "程式": "coding",
        "編程": "coding",
        "code": "coding",
        "文件": "reading",
        "閱讀": "reading",
        "資料": "data_organization",
        "統計": "statistics",
        "計算": "calculation",
        "推理": "reasoning",
        "分析": "analysis",
        "風險": "risk",
        "搜尋": "search",
        "查詢": "search",
    }
    VOTER_ROLES = (
        "composition-and-general-feasibility",
        "quality-training-and-evaluation",
        "collaboration-dependencies-and-risk",
    )

    def __init__(self, tool_root: Path, modules: StarModuleRegistry) -> None:
        self._tool_root = Path(tool_root).resolve()
        self._modules = modules

    @staticmethod
    def _text(value: Any, maximum: int) -> str:
        return str(value or "").strip()[:maximum]

    @staticmethod
    def _identifier(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return normalized[:48] or "composed-capability"

    def _intents(self, objective: str, requested: Any) -> list[str]:
        intents: list[str] = []
        if isinstance(requested, list):
            intents.extend(
                str(item).strip().casefold()
                for item in requested[:12]
                if str(item).strip().casefold() in self.ALLOWED_INTENTS
            )
        normalized = objective.casefold()
        intents.extend(
            intent
            for keyword, intent in self.KEYWORD_INTENTS.items()
            if keyword in normalized
        )
        if not intents:
            intents.extend(("capabilities", "reasoning"))
        return list(dict.fromkeys(intents))

    def compose(
        self,
        payload: dict[str, Any],
        *,
        coordinator_model: str,
        coding_expert_model: str,
        mathematical_expert_model: str,
        release_reviewer_model: str,
        training_coordinator_model: str,
        collaboration_coordinator_model: str,
        data_coordinator_model: str,
        votes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        name = self._text(payload.get("capability_name"), 120)
        objective = self._text(payload.get("objective"), 8_000)
        kind = self._text(payload.get("capability_kind"), 32).casefold() or "workflow"
        constraints = self._text(payload.get("constraints"), 4_000)
        if not name or not objective:
            return {
                "ok": False,
                "error_code": "CAPABILITY_SPEC_REQUIRED",
                "message": "能力名稱與目標不可空白。",
            }
        if kind not in self.ALLOWED_KINDS:
            return {
                "ok": False,
                "error_code": "CAPABILITY_KIND_NOT_ALLOWED",
                "allowed_kinds": sorted(self.ALLOWED_KINDS),
            }

        intents = self._intents(objective, payload.get("required_intents"))
        module_plan = self._modules.plan(
            [*intents, "self_upgrade" if kind != "workflow" else "capabilities"],
            coordinator_model=coordinator_model,
        )
        digest = hashlib.sha256(
            f"{name}\0{kind}\0{objective}\0{constraints}".encode("utf-8")
        ).hexdigest()
        capability_id = f"star-capability-{self._identifier(name)}-{digest[:12]}"
        implementation_required = kind in {"module", "specialist"}
        voter_models = (
            coordinator_model,
            training_coordinator_model,
            collaboration_coordinator_model,
        )
        supplied_votes = {
            str(item.get("model") or ""): item
            for item in list(votes or [])
            if isinstance(item, dict)
        }
        normalized_votes: list[dict[str, Any]] = []
        for model, role in zip(voter_models, self.VOTER_ROLES, strict=True):
            supplied = supplied_votes.get(model, {})
            approved = supplied.get("vote") == "approve"
            normalized_votes.append(
                {
                    "model": model,
                    "role": role,
                    "vote": "approve" if approved else "reject",
                    "reason": self._text(supplied.get("reason"), 2_000)
                    or "模型討論未完成，採安全否決。",
                    "one_model_one_vote": True,
                }
            )
        approve_count = sum(item["vote"] == "approve" for item in normalized_votes)
        reject_count = len(normalized_votes) - approve_count
        majority_passed = approve_count >= 2
        return {
            "ok": True,
            "schema": "star-capability-composition/v1",
            "composition_id": capability_id,
            "status": (
                "implementation-proposal-required"
                if majority_passed and implementation_required
                else "blueprint-ready"
                if majority_passed
                else "vote-rejected"
            ),
            "capability": {
                "name": name,
                "kind": kind,
                "objective": objective,
                "constraints": constraints,
                "intents": intents,
                "owner": "xingcheng",
                "version": "1.0",
            },
            "module_plan": module_plan,
            "model_assignments": {
                "composition_owner": "three-model-majority-vote",
                "coordinator": coordinator_model,
                "coding_expert": coding_expert_model,
                "quality_and_training": training_coordinator_model,
                "collaboration": collaboration_coordinator_model,
                "data": data_coordinator_model,
            },
            "model_discussion": {
                "rule": "one-model-one-vote-simple-majority",
                "voters": normalized_votes,
                "approve_count": approve_count,
                "reject_count": reject_count,
                "required_approvals": 2,
                "majority_passed": majority_passed,
                "inspection_gates": [
                    {
                        "model": mathematical_expert_model,
                        "role": "numeric-condition-and-logic-consistency",
                    },
                    {
                        "model": coding_expert_model,
                        "role": "syntax-type-dependency-and-code-security",
                    },
                    {
                        "model": release_reviewer_model,
                        "role": "specification-boundary-regression-and-release",
                    },
                ],
                "inspection_gates_must_pass": True,
                "governance_veto": True,
            },
            "implementation_required": implementation_required,
            "implementation_target": (
                "src/backend/services/xingcheng/application/composed_capabilities/"
                f"composed_{self._identifier(name).replace('-', '_')}.py"
                if implementation_required
                else ""
            ),
            "acceptance_gates": [
                "bounded-input-output-contract",
                "dependency-validation",
                "syntax-and-static-security-scan",
                "isolated-tests",
                "held-out-capability-evaluation",
                "governance-audit",
                "recoverable-versioned-release",
            ],
            "authority": {
                "source_write_performed": False,
                "source_write_scope": "project-source-excluding-governance-rule",
                "governance_rule_mutable": False,
                "direct_weight_write": False,
                "database_write_performed": False,
                "database_write_allowed_tools": [
                    "ai-collaboration",
                    "ai-assistant",
                ],
                "external_provider_direct_database_write": False,
                "publish_authority": "governance-versioned-release-only",
                "rollback_required": True,
                "write_requires_majority_vote": True,
                "write_requires_owner_approval": True,
            },
        }

    @staticmethod
    def _module_source(blueprint: dict[str, Any]) -> str:
        capability = dict(blueprint["capability"])
        module_plan = dict(blueprint["module_plan"])
        specification = {
            "schema": blueprint["schema"],
            "composition_id": blueprint["composition_id"],
            "capability": capability,
            "module_sequence": [
                str(item.get("module_id") or "")
                for item in list(module_plan.get("modules") or [])
                if isinstance(item, dict) and str(item.get("module_id") or "")
            ],
            "decision_rule": blueprint["model_assignments"]["composition_owner"],
            "vote_result": blueprint["model_discussion"],
            "version": "1.0",
        }
        encoded = pprint.pformat(specification, sort_dicts=True, width=100)
        return (
            '"""Governed Star-composed capability. Generated from an owner-approved blueprint."""\n\n'
            "from __future__ import annotations\n\n"
            "from typing import Any\n\n\n"
            f"CAPABILITY_SPEC: dict[str, Any] = {encoded}\n\n\n"
            "def execute(payload: dict[str, Any]) -> dict[str, Any]:\n"
            "    bounded_payload = dict(list(dict(payload or {}).items())[:64])\n"
            "    return {\n"
            "        'ok': True,\n"
            "        'composition_id': CAPABILITY_SPEC['composition_id'],\n"
            "        'capability': dict(CAPABILITY_SPEC['capability']),\n"
            "        'module_sequence': list(CAPABILITY_SPEC['module_sequence']),\n"
            "        'input': bounded_payload,\n"
            "        'source_write_authority': 'owner-approved-governed-xingcheng-only',\n"
            "    }\n"
        )

    def apply(self, blueprint: dict[str, Any]) -> dict[str, Any]:
        if blueprint.get("ok") is not True:
            return blueprint
        discussion = blueprint.get("model_discussion")
        if not isinstance(discussion, dict) or discussion.get("majority_passed") is not True:
            return {
                **blueprint,
                "ok": False,
                "error_code": "CAPABILITY_MAJORITY_VOTE_REQUIRED",
                "authority": {**blueprint["authority"], "source_write_performed": False},
            }
        target_relative = str(blueprint.get("implementation_target") or "").strip()
        if not target_relative:
            capability_id = str(blueprint["composition_id"])
            slug = self._identifier(capability_id)
            target_relative = (
                "src/backend/services/xingcheng/application/composed_capabilities/"
                f"{slug.replace('-', '_')}.py"
            )
        target = (self._tool_root / target_relative).resolve()
        allowed_root = (
            self._tool_root
            / "src"
            / "backend"
            / "services"
            / "xingcheng"
            / "application"
            / "composed_capabilities"
        ).resolve()
        try:
            target.relative_to(allowed_root)
        except ValueError:
            return {
                **blueprint,
                "ok": False,
                "error_code": "CAPABILITY_SOURCE_SCOPE_DENIED",
                "authority": {**blueprint["authority"], "source_write_performed": False},
            }
        if target.suffix != ".py" or target.name == "__init__.py":
            return {
                **blueprint,
                "ok": False,
                "error_code": "CAPABILITY_SOURCE_TARGET_INVALID",
            }

        source = self._module_source(blueprint)
        try:
            compile(source, str(target), "exec")
        except (SyntaxError, ValueError) as exc:
            return {
                **blueprint,
                "ok": False,
                "error_code": "CAPABILITY_SOURCE_VALIDATION_FAILED",
                "message": str(exc),
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if target.exists():
            backup_root = (
                self._tool_root / "runtime" / "state" / "capability-backups"
            ).resolve()
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = backup_root / f"{target.stem}-{stamp}.py"
            shutil.copy2(target, backup)

        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(source)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            from governance_rule.execution.audit import audit_runtime_governance

            audit_errors = audit_runtime_governance(self._tool_root.parent)
            if audit_errors:
                if backup is not None:
                    shutil.copy2(backup, target)
                else:
                    target.unlink(missing_ok=True)
                return {
                    **blueprint,
                    "ok": False,
                    "error_code": "CAPABILITY_GOVERNANCE_AUDIT_FAILED",
                    "audit_errors": audit_errors[:20],
                    "rolled_back": True,
                    "authority": {
                        **blueprint["authority"],
                        "source_write_performed": False,
                    },
                }
        finally:
            temporary.unlink(missing_ok=True)

        return {
            **blueprint,
            "status": "active-source-module",
            "database_write_performed": False,
            "implementation_target": target.relative_to(self._tool_root).as_posix(),
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "backup_path": (
                backup.relative_to(self._tool_root).as_posix() if backup else ""
            ),
            "governance_audit": "passed",
            "rolled_back": False,
            "authority": {
                **blueprint["authority"],
                "source_write_performed": True,
                "write_scope": "xingcheng-composed-capabilities-only",
                "project_code_authority": "project-source-excluding-governance-rule",
                "database_write_performed": False,
                "composition_owner": blueprint["model_assignments"][
                    "composition_owner"
                ],
            },
        }


__all__ = ["StarCapabilityComposer"]
