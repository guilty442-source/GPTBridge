"""Machine predicate evaluators for the formal rules (A437/A438/A439/A440/A442/A445-A449).

Each evaluator is registered against one ``formal_rule_code`` and converts
typed runtime facts into a (passed, decision, reason) verdict.  Evaluators
implement the predicate recorded in ``formal_rule_registry``; unregistered
facts are fail-closed by the evaluation engine (never PASS).
"""

from __future__ import annotations

from typing import Any, Mapping

from governance_rule.execution.formal_rules import register_rule

# ---------------------------------------------------------------------------
# RULE_CALL_BOUNDARY (A437)
# ---------------------------------------------------------------------------


@register_rule("RULE_CALL_BOUNDARY")
def _call_boundary(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: all-same (process/module/owner/trust/principal/generation/
    state owner) and no io/serialization/side-effect -> PURE_INTERNAL_CALL."""
    identity_keys = (
        "process",
        "module",
        "owner",
        "trust",
        "principal",
        "generation",
        "state_owner",
    )
    identities = {facts.get(key) for key in identity_keys}
    if len(identities) == 1 and next(iter(identities)) is not None:
        same_identity = True
    else:
        same_identity = False
    forbidden_effects = (
        bool(facts.get("io")),
        bool(facts.get("serialization")),
        bool(facts.get("side_effect")),
    )
    no_effects = not any(forbidden_effects)
    if same_identity and no_effects:
        return True, "PURE_INTERNAL_CALL", "all-same and no io/serialization/side-effect"
    diff = [key for key in identity_keys if facts.get(key) is not None]
    reasons = []
    if not same_identity:
        reasons.append("identity changed across a boundary")
    if not no_effects:
        reasons.append("io/serialization/side-effect present")
    boundary = "cross-boundary" if not same_identity else "side-effect-bearing"
    decision = "GOVERNED_BOUNDARY_CALL"
    reason = f"{boundary} ({', '.join(reasons)}) requires governed contract"
    return False, decision, reason


# ---------------------------------------------------------------------------
# RULE_ROLE_SEPARATION (A438)
# ---------------------------------------------------------------------------

_CANONICAL_ROLES = {
    "SOVEREIGN": {"decision", "receipt"},
    "SUB_SOVEREIGN": {"dispatch", "receipt"},
    "EXECUTOR": {"execution", "receipt"},
    "INFORMATION_LAYER": {"receipt"},
    "PERMISSION_SOVEREIGN": {"validation", "receipt"},
    "XINGCHENG": {"review", "receipt"},
}

_FORBIDDEN_BY_ROLE = {
    "SOVEREIGN": {"execution", "dispatch"},
    "SUB_SOVEREIGN": {"execution", "decision"},
    "EXECUTOR": {"decision", "dispatch"},
    "INFORMATION_LAYER": {"decision", "execution", "dispatch"},
    "PERMISSION_SOVEREIGN": {"execution", "decision", "dispatch"},
    "XINGCHENG": {"execution", "decision", "dispatch"},
}


@register_rule("RULE_ROLE_SEPARATION")
def _role_separation(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: operation matches canonical role."""
    role = str(facts.get("role", "")).upper()
    operation = str(facts.get("operation", "")).lower()
    if role not in _CANONICAL_ROLES:
        return False, "FAIL", f"unknown canonical role: {role!r}"
    if operation in _FORBIDDEN_BY_ROLE.get(role, set()):
        return (
            False,
            "FAIL",
            f"role {role} must not perform operation {operation!r}",
        )
    allowed = _CANONICAL_ROLES[role]
    if operation and operation not in allowed:
        return False, "FAIL", f"role {role} cannot claim operation {operation!r}"
    return True, "PASS", f"operation {operation or 'none'} matches role {role}"


# ---------------------------------------------------------------------------
# RULE_IDENTITY_CONVERSION (A439)
# ---------------------------------------------------------------------------

_CARDINALITY_RULES = {
    "tool_id": (0, 1),                       # -> runtime identity (may be absent)
    "runtime_identity": (1, 1),              # -> module code (total for active)
    "module_architecture_code": (0, None),   # -> runtimes (0..many, not reversible)
}


@register_rule("RULE_IDENTITY_CONVERSION")
def _identity_conversion(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: registered resolver yields declared cardinality."""
    namespace = str(facts.get("input_namespace", "")).strip()
    input_id = str(facts.get("input_id", "") or "").strip()
    generation = facts.get("generation")
    registry_hash = str(facts.get("registry_hash", "")).strip()
    output_ids = facts.get("output_ids")
    if not isinstance(output_ids, (list, tuple)):
        return False, "FAIL", "output_ids must be a sequence"
    if namespace not in _CARDINALITY_RULES:
        return False, "FAIL", f"unregistered identity namespace {namespace!r}"
    if not input_id:
        return False, "FAIL", "empty input identity"
    if generation is None:
        return False, "FAIL", "registry generation is required (no stale-generation cache)"
    if not registry_hash:
        return False, "FAIL", "registry evidence hash is required"
    low, high = _CARDINALITY_RULES[namespace]
    count = len(output_ids)
    if count < low or (high is not None and count > high):
        expected = f"{low}-{high}" if high is not None else f">={low}"
        return (
            False,
            "FAIL",
            f"namespace {namespace} declares {expected} results, got {count}",
        )
    return True, "PASS", f"declared cardinality satisfied for {namespace}"


# ---------------------------------------------------------------------------
# RULE_TEST_SUITE_CARDINALITY (A440)
# ---------------------------------------------------------------------------


@register_rule("RULE_TEST_SUITE_CARDINALITY")
def _test_suite_cardinality(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: exactly one canonical logical suite per module."""
    module_code = str(facts.get("module_code", "")).strip()
    suite_ids = facts.get("suite_ids")
    if not module_code:
        return False, "FAIL", "module_code is required"
    if not isinstance(suite_ids, (list, tuple)) or not suite_ids:
        return False, "FAIL", f"module {module_code} has no canonical suite"
    unique = {str(s) for s in suite_ids}
    if len(unique) != 1:
        return (
            False,
            "FAIL",
            f"module {module_code} declares {len(unique)} canonical suites: {sorted(unique)}",
        )
    return True, "PASS", f"module {module_code} has exactly one suite {unique.pop()}"


# ---------------------------------------------------------------------------
# RULE_HEALTH_CONTRACT (A442)
# ---------------------------------------------------------------------------


@register_rule("RULE_HEALTH_CONTRACT")
def _health_contract(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: schema and signals validate exactly."""
    contract = facts.get("contract")
    signals = facts.get("signals")
    schema_version = facts.get("schema_version")
    generation = facts.get("generation")
    if not isinstance(contract, Mapping):
        return False, "INCOMPLETE_EVIDENCE", "contract must be a typed mapping"
    for field in ("health_contract_id", "module_code", "dimensions"):
        if not contract.get(field):
            return False, "INCOMPLETE_EVIDENCE", f"contract missing required field {field}"
    if not schema_version:
        return False, "INCOMPLETE_EVIDENCE", "schema_version is required"
    if generation is None:
        return False, "INCOMPLETE_EVIDENCE", "generation is required"
    if not isinstance(signals, (list, tuple)):
        return False, "INCOMPLETE_EVIDENCE", "signals must be a sequence"
    for index, signal in enumerate(signals):
        if not isinstance(signal, Mapping):
            return False, "INCOMPLETE_EVIDENCE", f"signal {index} is not typed"
        for field in ("signal_id", "dimension", "value", "unit", "observed_at_utc"):
            if not signal.get(field):
                return False, "INCOMPLETE_EVIDENCE", f"signal {index} missing {field}"
    return True, "PASS", f"{len(signals)} signals validate exactly against contract"


# ---------------------------------------------------------------------------
# RULE_PRECEDENCE_RESOLUTION (A445)
# ---------------------------------------------------------------------------


@register_rule("RULE_PRECEDENCE_RESOLUTION")
def _precedence_resolution(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: exactly one effective controlling rule for the scope."""
    scope = str(facts.get("scope", "")).strip()
    successors = facts.get("successor_graph") or {}
    conflicts = facts.get("special_law_conflicts") or {}
    ordinances = facts.get("ordinance_links") or {}
    if not scope:
        return False, "CONFLICT", "scope is required for precedence resolution"
    if not isinstance(successors, Mapping):
        return False, "CONFLICT", "successor_graph must be a mapping"
    if not isinstance(conflicts, Mapping):
        return False, "CONFLICT", "special_law_conflicts must be a mapping"
    if not isinstance(ordinances, Mapping):
        return False, "CONFLICT", "ordinance_links must be a mapping"
    indicated = str(conflicts.get(scope, "")).strip()
    if indicated and indicated in successors:
        resolution = successors[indicated]
        return True, "PASS", f"{scope} resolves to {indicated} -> {resolution}"
    if indicated:
        return False, "CONFLICT", f"scope {scope} conflicts with {indicated} (no successor)"
    if scope in successors:
        return True, "PASS", f"{scope} resolves through explicit successor {successors[scope]}"
    if scope in ordinances:
        return True, "PASS", f"{scope} resolves through subordinate ordinance"
    return False, "INCOMPLETE_EVIDENCE", f"no controlling rule resolves scope {scope}"


# ---------------------------------------------------------------------------
# RULE_EXECUTION_PIPELINE (A446)
# ---------------------------------------------------------------------------

_PIPELINE_STAGES = (
    "INTAKE",
    "AUTHORIZE",
    "PLAN",
    "DISPATCH",
    "EXECUTE",
    "VERIFY",
    "PUBLISH",
)
_REDUCED_STAGES = ("INTAKE", "EXECUTE", "RESULT")


@register_rule("RULE_EXECUTION_PIPELINE")
def _execution_pipeline(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: required stages present for boundary facts."""
    stages = facts.get("stages")
    side_effect = facts.get("side_effect")
    operation = str(facts.get("operation", "")).strip()
    boundary = bool(facts.get("boundary"))
    if not isinstance(stages, (list, tuple)):
        return False, "FAIL", "stages must be a sequence"
    present = [str(s).upper() for s in stages]
    if boundary or side_effect:
        missing = [s for s in _PIPELINE_STAGES if s not in present]
        if missing:
            return (
                False,
                "FAIL",
                f"governed mutation requires stages {list(_PIPELINE_STAGES)}, missing {missing}",
            )
        return True, "PASS", f"full {len(_PIPELINE_STAGES)}-stage pipeline present"
    if operation and operation in _PIPELINE_STAGES and operation not in present:
        return False, "FAIL", f"declared stage {operation} not present in pipeline"
    reduced_ok = all(stage in present for stage in _REDUCED_STAGES)
    if reduced_ok:
        return True, "PASS", "owner-internal support path uses reduced pipeline"
    missing = [s for s in _PIPELINE_STAGES if s not in present]
    return False, "FAIL", f"unrecognised stage set; missing {missing}"


# ---------------------------------------------------------------------------
# RULE_CONNECTION_ROUTE (A447)
# ---------------------------------------------------------------------------

_ROUTE_PROFILES = (
    "LOCAL_PURE_CALL",
    "LOCAL_GOVERNED_DISPATCH",
    "READ_ONLY_QUERY",
    "TEST_ISOLATED_ROUTE",
    "SUPPORT_LIGHTWEIGHT",
    "FULL_TRANSPORT",
)


@register_rule("RULE_CONNECTION_ROUTE")
def _connection_route(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: selected least-cost profile predicate matches facts."""
    selected = str(facts.get("selected_route", "")).upper()
    process = facts.get("process")
    owner = facts.get("owner")
    trust = facts.get("trust")
    principal = facts.get("principal")
    state_owner = facts.get("state_owner")
    environment = str(facts.get("environment", "")).lower()
    io = bool(facts.get("io"))
    mutation = bool(facts.get("mutation"))

    def _crosses() -> bool:
        return not (
            process is not None
            and owner is not None
            and trust is not None
            and principal is not None
            and state_owner is not None
            and process == owner == trust == principal == state_owner
        )

    if selected not in _ROUTE_PROFILES:
        return False, "FAIL", f"unknown route profile {selected!r}"

    if selected == "LOCAL_PURE_CALL":
        eligible = not _crosses() and not io and not mutation
        reason = "local pure call" if eligible else "crosses boundary or has io/mutation"
    elif selected == "LOCAL_GOVERNED_DISPATCH":
        eligible = not io and not mutation and not _crosses()
        reason = "in-process governed dispatch" if eligible else "requires transport or mutates state"
    elif selected == "READ_ONLY_QUERY":
        eligible = io and not mutation and not _crosses()
        reason = "read-only bounded query" if eligible else "not a read-only query"
    elif selected == "TEST_ISOLATED_ROUTE":
        eligible = environment.startswith("test") and not _crosses()
        reason = "test-isolated route" if eligible else "not in test-isolated environment"
    elif selected == "SUPPORT_LIGHTWEIGHT":
        eligible = not mutation and not _crosses() and not io
        reason = "owner-internal support artifact" if eligible else "not lightweight support"
    else:  # FULL_TRANSPORT
        eligible = True
        reason = "full transport for cross-boundary or persisted I/O"

    if not eligible:
        return False, "FAIL", f"selected {selected} invalid: {reason}"
    return True, "PASS", f"selected least-cost profile {selected}: {reason}"


# ---------------------------------------------------------------------------
# RULE_MACHINE_SCHEMA (A448)
# ---------------------------------------------------------------------------

_MACHINE_SCHEMA_FIELDS = {
    "RECEIPT_V1": (
        "receipt_id", "operation_code", "request_id", "actor", "executor",
        "target", "scope", "generation", "idempotency_key", "result",
        "fault_codes", "evidence_hashes", "started_at_utc", "completed_at_utc",
        "correlation_id", "receipt_hash",
    ),
    "TOKEN_V1": (
        "token_id", "type", "issuer", "subject", "audience", "scope_codes",
        "purpose", "issued_at_utc", "not_before_utc", "expires_at_utc",
        "nonce", "generation", "policy_hash", "revocation_generation",
        "signature_reference", "status",
    ),
    "AUDIT_EVENT_V1": (
        "event_id", "sequence", "previous_event_hash", "event_type", "actor",
        "role", "operation", "target", "scope", "result", "fault_codes",
        "evidence_refs", "generation", "occurred_at_utc", "recorded_at_utc",
        "correlation_id", "event_hash",
    ),
}


@register_rule("RULE_MACHINE_SCHEMA")
def _machine_schema(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: payload validates the registered machine schema."""
    schema_code = str(facts.get("schema_code", "")).upper().strip()
    payload = facts.get("payload")
    version = facts.get("version")
    if schema_code not in _MACHINE_SCHEMA_FIELDS:
        return False, "FAIL", f"unregistered machine schema {schema_code!r}"
    if not isinstance(payload, Mapping):
        return False, "FAIL", f"payload for {schema_code} is not a typed mapping"
    if not version:
        return False, "FAIL", f"schema version required for {schema_code}"
    missing = [
        field
        for field in _MACHINE_SCHEMA_FIELDS[schema_code]
        if field not in payload
    ]
    if missing:
        return False, "FAIL", f"{schema_code} missing required fields: {missing}"
    return True, "PASS", f"payload validates {schema_code} v{version}"


# ---------------------------------------------------------------------------
# RULE_RELEASE_VERIFICATION (A449)
# ---------------------------------------------------------------------------


@register_rule("RULE_RELEASE_VERIFICATION")
def _release_verification(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate: VERIFIED only with technical pass AND valid external
    threshold signatures AND trust anchor validation."""
    technical = str(facts.get("technical_status", "")).upper().strip()
    signature_status = str(facts.get("signature_status", "")).upper().strip()
    trust_anchor = str(facts.get("trust_anchor", "")).upper().strip()
    if technical != "TECHNICAL_PASS":
        return False, "INCOMPLETE_EVIDENCE", f"technical status is {technical!r}, not TECHNICAL_PASS"
    if signature_status not in ("COMPLETE", "VALID"):
        return (
            False,
            "INCOMPLETE_EVIDENCE",
            f"missing external threshold signatures ({signature_status!r})",
        )
    if trust_anchor != "VALID":
        return False, "INCOMPLETE_EVIDENCE", f"trust anchor not validated ({trust_anchor!r})"
    return True, "VERIFIED_RELEASE", "technical pass + external signatures + trust anchor"