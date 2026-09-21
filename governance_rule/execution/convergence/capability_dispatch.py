"""Capability dispatch framework (A592 successor assignment model skeleton).

``MODULE_CAPABILITY_REGISTRY`` replaces the A334 fixed ``managing_sub_sovereign``
assignment with capability matching.  This module defines the registry schema,
its validation, and the deterministic ``RULE_CAPABILITY_DISPATCH_V1`` evaluator
skeleton.  It is a framework: no runtime dispatch is wired yet, and every check
is fail-closed.

Dispatch flow (target):

    task requirements -> capability matching -> permission validation ->
    resource/deadline constraints -> eligible module -> execution lease -> receipt

Forbidden: hard-coded sub-sovereign routing, module self-selection,
module self-authorization.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

REQUIRED_FIELDS = (
    "module_identity",
    "single_responsibility",
    "capability_codes",
    "resource_requirements",
    "permission_requirements",
    "contract_version",
    "runtime_state",
    "availability",
    "version",
    "owner_engine_domain",
    "execution_identity",
)

ACTIVE_STATES = ("active", "ready")
RULE_CODE = "RULE_CAPABILITY_DISPATCH_V1"


class DispatchError(RuntimeError):
    """Fail-closed dispatch denial."""


@dataclass(frozen=True)
class ModuleCapability:
    module_identity: str
    single_responsibility: str
    capability_codes: tuple[str, ...]
    resource_requirements: Mapping[str, Any] = field(default_factory=dict)
    permission_requirements: tuple[str, ...] = ()
    contract_version: str = ""
    runtime_state: str = "registered"
    availability: str = "unavailable"
    version: str = ""
    owner_engine_domain: str = ""
    execution_identity: str = ""

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "ModuleCapability":
        codes = row.get("capability_codes") or ()
        if isinstance(codes, str):
            codes = tuple(item for item in codes.split("|") if item)
        permissions = row.get("permission_requirements") or ()
        if isinstance(permissions, str):
            permissions = tuple(item for item in permissions.split("|") if item)
        return cls(
            module_identity=str(row.get("module_identity") or ""),
            single_responsibility=str(row.get("single_responsibility") or ""),
            capability_codes=tuple(str(item) for item in codes),
            resource_requirements=dict(row.get("resource_requirements") or {}),
            permission_requirements=tuple(str(item) for item in permissions),
            contract_version=str(row.get("contract_version") or ""),
            runtime_state=str(row.get("runtime_state") or "registered"),
            availability=str(row.get("availability") or "unavailable"),
            version=str(row.get("version") or ""),
            owner_engine_domain=str(row.get("owner_engine_domain") or ""),
            execution_identity=str(row.get("execution_identity") or ""),
        )


def validate_module_capability(row: Mapping[str, Any]) -> list[str]:
    """Registry schema validation (every field declared, codes non-empty)."""
    errors: list[str] = []
    for field_name in REQUIRED_FIELDS:
        if row.get(field_name) in (None, "", (), []):
            errors.append(f"MODULE_CAPABILITY_FIELD_MISSING:{field_name}")
    if row.get("single_responsibility") and len(str(row["single_responsibility"])) > 120:
        errors.append("MODULE_CAPABILITY_RESPONSIBILITY_NOT_SINGLE")
    return errors


def evaluate_dispatch(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """``RULE_CAPABILITY_DISPATCH_V1`` predicate (fail-closed).

    Facts: ``requirements`` (capability codes + resource/permission/contract
    constraints), ``candidates`` (registry rows), ``lease``, ``decision``,
    ``selected_by_module``.
    """
    requirements = facts.get("requirements") or {}
    if not requirements or not requirements.get("capability_codes"):
        return False, "FAIL_CLOSED", "request requirements not declared"
    if facts.get("selected_by_module"):
        return False, "FAIL_CLOSED", "module self-selection forbidden"

    candidates: Sequence[Mapping[str, Any]] = facts.get("candidates") or ()
    if not candidates:
        return False, "FAIL_CLOSED", "no candidate modules registered"

    eligible: list[str] = []
    required_codes = set(str(code) for code in requirements["capability_codes"])
    for row in candidates:
        errors = validate_module_capability(row)
        if errors:
            continue
        module = ModuleCapability.from_mapping(row)
        if not required_codes.issubset(set(module.capability_codes)):
            continue
        if module.runtime_state not in ACTIVE_STATES or module.availability not in ACTIVE_STATES:
            continue
        required_permissions = set(str(item) for item in requirements.get("permission_scope") or ())
        if not required_permissions.issubset(set(module.permission_requirements)):
            continue
        resource = requirements.get("resource_constraints") or {}
        module_resource = dict(module.resource_requirements)
        if any(module_resource.get(key, 0) > value for key, value in resource.items()):
            continue
        contract = str(requirements.get("contract_version") or "")
        if contract and module.contract_version != contract:
            continue
        eligible.append(module.module_identity)

    if not eligible:
        return False, "FAIL_CLOSED", "no eligible module"
    if len(eligible) > 1:
        return False, "FAIL_CLOSED", f"dispatch conflict: {sorted(eligible)}"

    lease = facts.get("lease") or {}
    if not lease.get("valid") or not lease.get("fencing_token"):
        return False, "FAIL_CLOSED", "execution lease invalid"
    decision = facts.get("decision") or {}
    if not decision.get("recorded"):
        return False, "FAIL_CLOSED", "dispatch decision not recorded"
    return True, "PASS", f"eligible module {eligible[0]}"


__all__ = [
    "ACTIVE_STATES",
    "DispatchError",
    "ModuleCapability",
    "REQUIRED_FIELDS",
    "RULE_CODE",
    "evaluate_dispatch",
    "validate_module_capability",
]
