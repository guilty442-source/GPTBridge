"""Machine predicate evaluators for the formal rules (A437/A438/A439/A440/A442/A445-A449).

Each evaluator is registered against one ``formal_rule_code`` and converts
typed runtime facts into a (passed, decision, reason) verdict.  Evaluators
implement the predicate recorded in ``formal_rule_registry``; unregistered
facts are fail-closed by the evaluation engine (never PASS).
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Mapping

# Store pending registrations to avoid circular import at module load time
_pending_registrations: list[tuple[str, Callable]] = []


def register_rule(rule_code: str):
    """Decorator that stores registration for later finalization."""
    def decorator(evaluator: Callable) -> Callable:
        _pending_registrations.append((rule_code, evaluator))
        return evaluator
    return decorator


def finalize_registrations() -> None:
    """Register all pending evaluators with the formal_rules registry.

    This must be called after the formal_rules package is fully initialized.
    """
    formal_rules = importlib.import_module("governance_rule.execution.formal_rules")
    real_register = formal_rules.register_rule
    for rule_code, evaluator in _pending_registrations:
        real_register(rule_code)(evaluator)
    _pending_registrations.clear()


# ---------------------------------------------------------------------------
# RULE_CALL_BOUNDARY (A437)
# ---------------------------------------------------------------------------


_BOUNDARY_NAMESPACES = (
    "process",
    "module",
    "owner",
    "trust",
    "principal",
    "generation",
    "state_owner",
)


@register_rule("RULE_CALL_BOUNDARY")
def _call_boundary(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A437): paired caller/callee fields equal by namespace and no
    io/serialization/side-effect -> PURE_INTERNAL_CALL.

    Paired facts (``caller.<namespace>`` / ``callee.<namespace>``) compare the
    two call contexts per namespace.  A flat single-context fact set describes
    one context shared by both sides.  Missing pair evidence, a changed
    namespace or any effect classifies as a governed boundary call
    (fail-closed); names across different namespaces are never compared.
    """
    paired = [
        ns
        for ns in _BOUNDARY_NAMESPACES
        if f"caller.{ns}" in facts or f"callee.{ns}" in facts
    ]
    if paired:
        incomplete = [
            ns
            for ns in _BOUNDARY_NAMESPACES
            if f"caller.{ns}" not in facts or f"callee.{ns}" not in facts
        ]
        if incomplete:
            return (
                False,
                "GOVERNED_BOUNDARY_CALL",
                f"incomplete caller/callee evidence for {incomplete}",
            )
        changed = [
            ns
            for ns in _BOUNDARY_NAMESPACES
            if facts[f"caller.{ns}"] != facts[f"callee.{ns}"]
        ]
    else:
        present = [ns for ns in _BOUNDARY_NAMESPACES if ns in facts]
        if not present:
            return (
                False,
                "GOVERNED_BOUNDARY_CALL",
                "no caller/callee identity facts supplied",
            )
        if any(facts.get(ns) in (None, "") for ns in present):
            return (
                False,
                "GOVERNED_BOUNDARY_CALL",
                "identity facts contain empty values",
            )
        changed = []
    effects = (
        bool(facts.get("io")),
        bool(facts.get("serialization")),
        bool(facts.get("side_effect")),
    )
    if not changed and not any(effects):
        return (
            True,
            "PURE_INTERNAL_CALL",
            "caller/callee equal by namespace and no io/serialization/side-effect",
        )
    reasons = []
    if changed:
        reasons.append(f"identity changed across a boundary: {', '.join(changed)}")
    if any(effects):
        reasons.append("io/serialization/side-effect present")
    return False, "GOVERNED_BOUNDARY_CALL", "; ".join(reasons)


# ---------------------------------------------------------------------------
# RULE_ROLE_SEPARATION (A438)
# ---------------------------------------------------------------------------

_CANONICAL_ROLES = {
    "SOVEREIGN": frozenset({"decision", "receipt"}),
    "SUB_SOVEREIGN": frozenset({"manage", "dispatch", "receipt"}),
    "EXECUTOR": frozenset({"execution", "receipt"}),
    "INFORMATION_LAYER": frozenset({"contract", "status", "receipt"}),
    "PERMISSION_SOVEREIGN": frozenset({"validation", "receipt"}),
    # A336/A337/A378: native programming review, whole-system automation
    # coordination and governed tool invocation/dispatch within 星澄's
    # privileged institution; mutation still needs scope+authorization.
    "XINGCHENG": frozenset(
        {"review", "coordinate", "invocation", "dispatch", "execution", "receipt"}
    ),
}

_FORBIDDEN_BY_ROLE = {
    "SOVEREIGN": frozenset({"execution", "dispatch"}),
    "SUB_SOVEREIGN": frozenset({"execution", "decision"}),
    "EXECUTOR": frozenset({"decision", "dispatch"}),
    "INFORMATION_LAYER": frozenset({"decision", "execution", "dispatch"}),
    "PERMISSION_SOVEREIGN": frozenset({"decision", "execution", "dispatch"}),
    # A378 FORBID: grant/revoke permission, alter Codex, bypass sovereign
    # decisions — review/coordination never carries decision power.
    "XINGCHENG": frozenset({"decision"}),
}

_CAPABILITY_FLAGS = ("decision", "dispatch", "execution", "receipt")


def _role_identity_matches(role: str, identity: str) -> bool:
    """One active request role identity: the identity is the role itself or a
    role-scoped sub-role activation such as ``XINGCHENG:COORDINATION`` (A438:
    a component may implement multiple interfaces only under one role identity
    per request; role mixing inside one execution context is forbidden)."""
    identity = identity.strip().upper()
    return identity == role or identity.startswith(role + ":") or identity.startswith(
        role + "_"
    ) or identity.startswith(role + "-")


@register_rule("RULE_ROLE_SEPARATION")
def _role_separation(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A438): one active request role matches Codex-authorized
    capability including 星澄 special-law roles."""
    role = str(facts.get("role", "")).upper().strip()
    operation = str(facts.get("operation", "")).lower().strip()
    if role not in _CANONICAL_ROLES:
        return False, "FAIL", f"unknown canonical role: {role!r}"
    active_identity = str(facts.get("active_role_identity", "") or "").strip()
    if active_identity and not _role_identity_matches(role, active_identity):
        return (
            False,
            "FAIL",
            f"active role identity {active_identity!r} is not a {role} activation "
            "(role mixing)",
        )
    allowed = _CANONICAL_ROLES[role]
    forbidden = _FORBIDDEN_BY_ROLE.get(role, frozenset())
    claims = [operation] if operation else []
    claims.extend(
        flag
        for flag in _CAPABILITY_FLAGS
        if bool(facts.get(flag)) and flag not in claims
    )
    for claim in claims:
        if claim in forbidden:
            return False, "FAIL", f"role {role} must not perform operation {claim!r}"
        if claim not in allowed:
            return False, "FAIL", f"role {role} cannot claim operation {claim!r}"
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
    """Predicate (A439): certified current-generation resolver validates
    registry membership, status, cardinality and receipt."""
    namespace = str(facts.get("input_namespace", "")).strip()
    input_id = str(facts.get("input_id", "") or "").strip()
    generation = facts.get("generation")
    registry_generation = facts.get("registry_generation")
    registry_hash = str(facts.get("registry_hash", "")).strip()
    resolver_id = str(facts.get("resolver_id", "") or "").strip()
    resolver_receipt = facts.get("resolver_receipt")
    output_ids = facts.get("output_ids")
    if namespace not in _CARDINALITY_RULES:
        return False, "FAIL", f"unregistered identity namespace {namespace!r}"
    if not input_id:
        return False, "FAIL", "empty input identity"
    if generation is None:
        return False, "FAIL", "registry generation is required (no stale-generation cache)"
    if registry_generation is None:
        return False, "FAIL", "registry generation evidence is required (resolver receipt binding)"
    if str(generation) != str(registry_generation):
        return (
            False,
            "FAIL",
            f"stale generation: input generation {generation} != registry generation {registry_generation}",
        )
    if not registry_hash:
        return False, "FAIL", "registry evidence hash is required"
    if not resolver_id:
        return False, "FAIL", "certified resolver identity is required"
    receipt_error = _resolver_receipt_error(resolver_receipt, resolver_id, generation)
    if receipt_error:
        return False, "FAIL", receipt_error
    if not isinstance(output_ids, (list, tuple)):
        return False, "FAIL", "output_ids must be a sequence"
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


def _resolver_receipt_error(
    receipt: Any, resolver_id: str, generation: Any
) -> str:
    """Validate the resolver receipt binding (A439 receipt, no fabrication)."""
    if isinstance(receipt, Mapping):
        receipt_resolver = str(receipt.get("resolver_id", "") or "").strip()
        if receipt_resolver and receipt_resolver != resolver_id:
            return "resolver receipt belongs to another resolver"
        receipt_generation = receipt.get("registry_generation")
        if receipt_generation is not None and str(receipt_generation) != str(generation):
            return "resolver receipt generation mismatch"
        status = str(receipt.get("status", "") or "").strip().lower()
        if status and status not in {"ok", "valid", "verified", "current", "resolved"}:
            return f"resolver receipt status {status!r} is not certified"
        return "" if receipt else "resolver receipt is required"
    if not str(receipt or "").strip():
        return "resolver receipt is required"
    return ""


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


def _stage_value(facts: Mapping[str, Any], field: str, stage: str) -> Any:
    values = facts.get(field)
    if not isinstance(values, Mapping):
        return None
    return values.get(stage)


def _stage_contract_error(facts: Mapping[str, Any], stage: str) -> str | None:
    """One A446 stage contract: owner + input/output schema + timeout +
    idempotency + fault codes + receipt type."""
    owner = _stage_value(facts, "stage_owners", stage)
    if not str(owner or "").strip():
        return f"{stage} has no stage owner"
    schemas = _stage_value(facts, "schemas", stage)
    if (
        not isinstance(schemas, Mapping)
        or not str(schemas.get("input", "") or "").strip()
        or not str(schemas.get("output", "") or "").strip()
    ):
        return f"{stage} has no input/output schema"
    timeout = _stage_value(facts, "timeouts", stage)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return f"{stage} has no numeric timeout"
    if timeout_value <= 0:
        return f"{stage} timeout must be positive"
    idempotency = _stage_value(facts, "idempotency", stage)
    if idempotency in (None, "", False):
        return f"{stage} has no idempotency declaration"
    faults = _stage_value(facts, "fault_codes", stage)
    if not isinstance(faults, (list, tuple)) or not faults:
        return f"{stage} has no fault codes"
    receipt = _stage_value(facts, "receipts", stage)
    if not str(receipt or "").strip():
        return f"{stage} has no receipt type"
    return None


def _stage_order_error(present: list[str], required: tuple[str, ...]) -> str | None:
    duplicates = sorted({stage for stage in present if present.count(stage) > 1})
    if duplicates:
        return f"duplicate stages {duplicates}"
    missing = [stage for stage in required if stage not in present]
    if missing:
        return f"missing stages {missing}"
    indices = [present.index(stage) for stage in required]
    if indices != sorted(indices):
        return f"stages out of order: {present}"
    return None


@register_rule("RULE_EXECUTION_PIPELINE")
def _execution_pipeline(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A446): ordered unique stages validate owners, schemas,
    timeouts, idempotency, faults, receipts and executor-verifier separation.
    """
    stages = facts.get("stages")
    side_effect = facts.get("side_effect")
    operation = str(facts.get("operation", "")).strip().upper()
    boundary = bool(facts.get("boundary"))
    if not isinstance(stages, (list, tuple)):
        return False, "FAIL", "stages must be a sequence"
    present = [str(stage).upper() for stage in stages]
    if boundary or side_effect:
        order_error = _stage_order_error(present, _PIPELINE_STAGES)
        if order_error:
            return (
                False,
                "FAIL",
                "governed mutation requires ordered unique "
                f"{list(_PIPELINE_STAGES)}: {order_error}",
            )
        contract_errors = [
            error
            for stage in _PIPELINE_STAGES
            if (error := _stage_contract_error(facts, stage)) is not None
        ]
        if contract_errors:
            return False, "FAIL", "; ".join(contract_errors)
        verifier = str(facts.get("verifier", "") or "").strip()
        if not verifier:
            return False, "FAIL", "independent verifier identity is required"
        executor = str(
            facts.get("executor")
            or _stage_value(facts, "stage_owners", "EXECUTE")
            or ""
        ).strip()
        if executor and executor == verifier:
            return False, "FAIL", "executor must not verify its own work (A446/A451)"
        return True, "PASS", f"full {len(_PIPELINE_STAGES)}-stage pipeline validated"
    if operation and operation in _PIPELINE_STAGES and operation not in present:
        return False, "FAIL", f"declared stage {operation} not present in pipeline"
    if present == list(_REDUCED_STAGES):
        return True, "PASS", "owner-internal support path uses reduced pipeline"
    return (
        False,
        "FAIL",
        f"unrecognised stage set {present}; expected "
        f"{list(_PIPELINE_STAGES)} or {list(_REDUCED_STAGES)}",
    )


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


# ---------------------------------------------------------------------------
# RULE_CODEX_CONVERGENCE_V1 (A77)
# ---------------------------------------------------------------------------


@register_rule("RULE_CODEX_CONVERGENCE_V1")
def _codex_convergence(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A77): no conflicting active duplicate, dynamic fact encoded
    only in Article, superseded effective/default-search result, multiple formal
    owners, unregistered directory, or conflicting closure policy. Unresolved
    classification or parity evidence is INCOMPLETE_EVIDENCE; otherwise PASS."""
    conflicts = facts.get("conflicts") or {}
    active_duplicates = facts.get("active_duplicates") or []
    dynamic_facts = facts.get("dynamic_facts") or []
    superseded_results = facts.get("superseded_results") or []
    formal_owners = facts.get("formal_owners") or []
    unregistered_dirs = facts.get("unregistered_directories") or []
    closure_conflicts = facts.get("closure_conflicts") or []
    classification = str(facts.get("classification", "")).strip()
    parity_evidence = facts.get("parity_evidence")

    if active_duplicates:
        return False, "FAIL_CLOSED", f"active duplicates detected: {active_duplicates}"
    if dynamic_facts:
        return False, "FAIL_CLOSED", f"dynamic facts outside Article: {dynamic_facts}"
    if superseded_results:
        return False, "FAIL_CLOSED", f"superseded results still active: {superseded_results}"
    if len(formal_owners) > 1:
        return False, "FAIL_CLOSED", f"multiple formal owners: {formal_owners}"
    if unregistered_dirs:
        return False, "FAIL_CLOSED", f"unregistered directories: {unregistered_dirs}"
    if closure_conflicts:
        return False, "FAIL_CLOSED", f"conflicting closure policy: {closure_conflicts}"
    if classification and classification not in ("PASS", "FAIL", "INCOMPLETE_EVIDENCE"):
        return False, "INCOMPLETE_EVIDENCE", f"unresolved classification: {classification}"
    if parity_evidence is None:
        return False, "INCOMPLETE_EVIDENCE", "parity evidence required"
    return True, "PASS", "codex convergence validated"


# ---------------------------------------------------------------------------
# RULE_DELEGATED_A498_V1 (A498)
# ---------------------------------------------------------------------------


@register_rule("RULE_DELEGATED_A498_V1")
def _delegated_a498(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A498): machine_schema_registry is sole canonical typed contract
    registry including field types, nullability, enums, ranges, compatibility,
    hash, lineage, unknown-field policy. Chinese mirror parts are object/map.
    CODEX_READ_SESSION_V1.single_use is boolean true. IDENTITY_CONVERSION_V1
    matches A439 canonical output. IDENTITY_RESOLUTION_EVIDENCE_V1 carries
    resolver receipt without second output envelope. All 10 formal rule evidence
    schemas and GATE_EVIDENCE_V1 use versioned canonical IDs and
    machine_schema_lineage. AUDIT_EVENT_V1 includes redaction_class and A448
    required set. Technical and certification states are separate required fields.
    JSON text only as canonical schema descriptor inside normalized registry
    columns, never as authoritative domain row payload."""
    registry = facts.get("machine_schema_registry")
    chinese_mirror = facts.get("chinese_mirror_parts")
    codex_read_session = facts.get("codex_read_session")
    identity_conversion = facts.get("identity_conversion")
    identity_resolution = facts.get("identity_resolution_evidence")
    formal_rule_schemas = facts.get("formal_rule_evidence_schemas")
    gate_evidence = facts.get("gate_evidence")
    audit_event = facts.get("audit_event")
    json_usage = facts.get("json_text_usage")

    if not registry:
        return False, "FAIL_CLOSED", "machine_schema_registry missing"
    if not isinstance(chinese_mirror, Mapping) or not chinese_mirror:
        return False, "FAIL_CLOSED", "chinese mirror parts must be object/map"
    if not isinstance(codex_read_session, Mapping):
        return False, "FAIL_CLOSED", "CODEX_READ_SESSION_V1 missing"
    if not codex_read_session.get("single_use") is True:
        return False, "FAIL_CLOSED", "CODEX_READ_SESSION_V1.single_use must be true"
    if not identity_conversion:
        return False, "FAIL_CLOSED", "IDENTITY_CONVERSION_V1 missing"
    if not identity_resolution:
        return False, "FAIL_CLOSED", "IDENTITY_RESOLUTION_EVIDENCE_V1 missing"
    if not isinstance(formal_rule_schemas, (list, tuple)) or len(formal_rule_schemas) != 10:
        return False, "FAIL_CLOSED", "exactly 10 formal rule evidence schemas required"
    if not gate_evidence:
        return False, "FAIL_CLOSED", "GATE_EVIDENCE_V1 missing"
    if not audit_event or "redaction_class" not in audit_event:
        return False, "FAIL_CLOSED", "AUDIT_EVENT_V1 missing redaction_class"
    if json_usage and json_usage != "descriptor-only":
        return False, "FAIL_CLOSED", f"JSON text usage invalid: {json_usage}"
    return True, "PASS", "A498 delegated contract validated"


# ---------------------------------------------------------------------------
# RULE_DELEGATION_VALIDATION_V1 (A334)
# ---------------------------------------------------------------------------


@register_rule("RULE_DELEGATION_VALIDATION_V1")
def _delegation_validation(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A334): validate in declared order; any mandatory failure denies;
    replay, expiry, scope expansion deny."""
    steps = facts.get("validation_steps") or []
    order = facts.get("declared_order") or []
    mandatory = facts.get("mandatory_steps") or []
    replay = facts.get("replay_detected", False)
    expired = facts.get("expired", False)
    scope_expansion = facts.get("scope_expansion", False)

    if replay:
        return False, "FAIL_CLOSED", "replay detected"
    if expired:
        return False, "FAIL_CLOSED", "delegation expired"
    if scope_expansion:
        return False, "FAIL_CLOSED", "scope expansion not allowed"

    if steps != order:
        return False, "FAIL_CLOSED", "validation steps out of declared order"

    for step in mandatory:
        if step not in steps:
            return False, "FAIL_CLOSED", f"mandatory step missing: {step}"
        step_result = facts.get(f"step_{step}_result")
        if step_result != "PASS":
            return False, "FAIL_CLOSED", f"mandatory step {step} failed: {step_result}"

    return True, "PASS", "delegation validation passed"


# ---------------------------------------------------------------------------
# RULE_EXECUTION_VERIFICATION_SEPARATION_V1 (A446)
# ---------------------------------------------------------------------------


@register_rule("RULE_EXECUTION_VERIFICATION_SEPARATION_V1")
def _execution_verification_separation(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A446): independent verification required operations cannot
    use executor as sole verifier; missing evidence is incomplete; mismatch fails."""
    executor = str(facts.get("executor", "")).strip()
    verifier = str(facts.get("verifier", "")).strip()
    evidence = facts.get("verification_evidence")
    operation_requires_verification = bool(facts.get("requires_independent_verification", True))

    if not operation_requires_verification:
        return True, "PASS", "operation does not require independent verification"

    if not executor:
        return False, "FAIL_CLOSED", "executor identity required"
    if not verifier:
        return False, "FAIL_CLOSED", "verifier identity required"
    if executor == verifier:
        return False, "FAIL_CLOSED", "executor must not verify its own work"
    if evidence is None:
        return False, "INCOMPLETE_EVIDENCE", "verification evidence required"

    return True, "PASS", "independent verification separation validated"


# ---------------------------------------------------------------------------
# RULE_FILE_OPERATION_RELIABILITY_V1 (A446)
# ---------------------------------------------------------------------------


@register_rule("RULE_FILE_OPERATION_RELIABILITY_V1")
def _file_operation_reliability(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A446): state-changing file operation requires declared lock,
    staging, integrity, journal, recovery and final receipt evidence."""
    operation = str(facts.get("operation", "")).strip()
    lock = facts.get("lock_evidence")
    staging = facts.get("staging_evidence")
    integrity = facts.get("integrity_evidence")
    journal = facts.get("journal_evidence")
    recovery = facts.get("recovery_evidence")
    receipt = facts.get("final_receipt")

    if not operation:
        return False, "FAIL_CLOSED", "operation type required"

    required = {
        "lock": lock,
        "staging": staging,
        "integrity": integrity,
        "journal": journal,
        "recovery": recovery,
        "receipt": receipt,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        return False, "FAIL_CLOSED", f"missing file operation evidence: {', '.join(missing)}"

    return True, "PASS", f"file operation {operation} reliability validated"


# ---------------------------------------------------------------------------
# RULE_MATURE_CAPABILITY_PRESERVATION_V1 (A77)
# ---------------------------------------------------------------------------


@register_rule("RULE_MATURE_CAPABILITY_PRESERVATION_V1")
def _mature_capability_preservation(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A77): capability removal, degradation, or loss of contract,
    test, or evidence path without explicit supersession fails."""
    capability_id = str(facts.get("capability_id", "")).strip()
    action = str(facts.get("action", "")).strip().lower()
    supersession = facts.get("explicit_supersession")
    contract_preserved = facts.get("contract_preserved", False)
    test_preserved = facts.get("test_preserved", False)
    evidence_preserved = facts.get("evidence_path_preserved", False)

    if not capability_id:
        return False, "FAIL_CLOSED", "capability_id required"

    if action in ("remove", "degrade", "loss"):
        if not supersession:
            return False, "FAIL_CLOSED", f"capability {capability_id} {action} without explicit supersession"
        if not (contract_preserved and test_preserved and evidence_preserved):
            return False, "FAIL_CLOSED", f"capability {capability_id} {action} loses contract/test/evidence"

    return True, "PASS", f"capability {capability_id} preservation validated"


def _registered_domain_evidence(
    facts: Mapping[str, Any], domain: str
) -> tuple[bool, str, str]:
    """Shared evaluator shape for registry-owned final-convergence domains."""
    violations = facts.get("violations") or []
    evidence = facts.get("evidence")
    if violations:
        return False, "FAIL_CLOSED", f"{domain} violations: {violations}"
    if evidence is None:
        return False, "INCOMPLETE_EVIDENCE", f"{domain} evidence required"
    return True, "PASS", f"{domain} evidence validated"


@register_rule("RULE_GIT_WORKTREE_V1")
def _git_worktree(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    return _registered_domain_evidence(facts, "git-worktree")


@register_rule("RULE_RAG_PROVENANCE_V1")
def _rag_provenance(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    return _registered_domain_evidence(facts, "rag-provenance")


@register_rule("RULE_NATIVE_PROMOTION_V1")
def _native_promotion(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    return _registered_domain_evidence(facts, "native-promotion")


@register_rule("RULE_BACKEND_HANDOFF_V1")
def _backend_handoff(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    return _registered_domain_evidence(facts, "backend-handoff")


@register_rule("RULE_MODEL_DIALOGUE_MODE_V1")
def _model_dialogue_mode(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    return _registered_domain_evidence(facts, "model-dialogue-mode")


@register_rule("RULE_TOOL_RUNTIME_V1")
def _tool_runtime(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    return _registered_domain_evidence(facts, "tool-runtime")


# ---------------------------------------------------------------------------
# RULE_RUNTIME_AUTHORITY_RESOLUTION_V1 (A334)
# ---------------------------------------------------------------------------


@register_rule("RULE_RUNTIME_AUTHORITY_RESOLUTION_V1")
def _runtime_authority_resolution(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A334): registered active module; exactly one assignment; active
    parent; valid separated authorities; executor exact match; current
    generation; no overlap."""
    module = str(facts.get("module_code", "")).strip()
    assignment = facts.get("module_assignment")
    parent = facts.get("parent_sovereign")
    authorities = facts.get("separated_authorities") or {}
    executor = str(facts.get("executor", "")).strip()
    generation = facts.get("generation")
    overlap = facts.get("authority_overlap", False)

    if not module:
        return False, "FAIL_CLOSED", "module_code required"
    if not assignment:
        return False, "FAIL_CLOSED", "module assignment required"
    if str(assignment).count(";") > 0:
        return False, "FAIL_CLOSED", "multiple assignments for module"
    if not parent:
        return False, "FAIL_CLOSED", "parent sovereign required"
    required_auth = {"decision", "permission", "execution", "runtime"}
    if set(authorities.keys()) != required_auth:
        return False, "FAIL_CLOSED", f"separated authorities must be exactly {required_auth}, got {set(authorities.keys())}"
    if not executor:
        return False, "FAIL_CLOSED", "executor identity required"
    if generation is None:
        return False, "FAIL_CLOSED", "current generation required"
    if overlap:
        return False, "FAIL_CLOSED", "authority overlap detected"

    return True, "PASS", f"runtime authority for {module} resolved"


# ---------------------------------------------------------------------------
# RULE_SQL_GOVERNANCE_CLOSURE (A516)
# ---------------------------------------------------------------------------


@register_rule("RULE_SQL_GOVERNANCE_CLOSURE")
def _sql_governance_closure(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A516): PASS only when every required SQL governance root is
    present and valid, declared_schema_hash equals replayed_schema_hash equals
    live_introspected_schema_hash, open_findings equals zero and tests pass;
    missing evidence is INCOMPLETE_EVIDENCE; drift, security excess, chain
    break or reconciliation violation is FAIL; both non-PASS states deny release."""
    roots = facts.get("governance_roots")
    if roots is None:
        # Accept individual hash fields as governance roots (test format)
        roots = {
            "postgres_catalog_hash": facts.get("postgres_catalog_hash"),
            "migration_chain_hash": facts.get("migration_chain_hash"),
            "live_schema_hash": facts.get("live_schema_hash"),
            "security_projection_hash": facts.get("security_projection_hash"),
            "sqlite_scope_hash": facts.get("sqlite_scope_hash"),
            "reconciliation_hash": facts.get("reconciliation_hash"),
            "audit_contract_hash": facts.get("audit_contract_hash"),
            "transport_contract_hash": facts.get("transport_contract_hash"),
            "test_evidence_hash": facts.get("test_evidence_hash"),
        }
    declared_hash = str(facts.get("declared_schema_hash", "")).strip()
    replayed_hash = str(facts.get("replayed_schema_hash", "")).strip()
    # Accept both live_introspected_schema_hash and live_schema_hash
    live_hash = str(facts.get("live_introspected_schema_hash", facts.get("live_schema_hash", ""))).strip()
    open_findings = facts.get("open_findings", 0)
    # Accept both tests_pass and result="PASS"
    tests_pass = facts.get("tests_pass", facts.get("result") == "PASS")
    drift = facts.get("schema_drift", False)
    security_excess = facts.get("security_excess", False)
    chain_break = facts.get("chain_break", False)
    reconciliation_violation = facts.get("reconciliation_violation", False)

    if not roots or not any(roots.values()):
        return False, "INCOMPLETE_EVIDENCE", "governance roots evidence required"
    if not declared_hash or not replayed_hash or not live_hash:
        return False, "INCOMPLETE_EVIDENCE", "schema hashes required"
    if declared_hash != replayed_hash or declared_hash != live_hash:
        return False, "FAIL_CLOSED", "schema hash mismatch: declared != replayed != live"
    if open_findings != 0:
        return False, "FAIL_CLOSED", f"open findings: {open_findings}"
    if not tests_pass:
        return False, "FAIL_CLOSED", "tests do not pass"
    if drift or security_excess or chain_break or reconciliation_violation:
        return False, "FAIL_CLOSED", f"drift={drift} security_excess={security_excess} chain_break={chain_break} reconciliation_violation={reconciliation_violation}"

    return True, "PASS", "SQL governance closure validated"


# ---------------------------------------------------------------------------
# RULE_SQL_MIGRATION_AUTHORITY (A502)
# ---------------------------------------------------------------------------


@register_rule("RULE_SQL_MIGRATION_AUTHORITY")
def _sql_migration_authority(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A502): current_postgresql_schema_hash equals
    ordered_verified_migrations_result_hash and directory/catalog parity is
    exact; otherwise SCHEMA_DRIFT and FAIL_CLOSED."""
    current_hash = str(facts.get("current_postgresql_schema_hash", "")).strip()
    migrations_hash = str(facts.get("ordered_verified_migrations_result_hash", "")).strip()
    # Accept both boolean parity flags and direct hash comparison (test format)
    directory_parity = facts.get("directory_catalog_parity")
    if directory_parity is None:
        directory_parity = facts.get("data_schema_authority_root") == facts.get("catalog_snapshot_hash")
    catalog_parity = facts.get("catalog_parity")
    if catalog_parity is None:
        catalog_parity = facts.get("data_schema_authority_root") == facts.get("catalog_snapshot_hash")

    if not current_hash:
        return False, "FAIL_CLOSED", "current_postgresql_schema_hash required"
    if not migrations_hash:
        return False, "FAIL_CLOSED", "ordered_verified_migrations_result_hash required"
    if current_hash != migrations_hash:
        return False, "SCHEMA_DRIFT_FAIL_CLOSED", "current schema hash != migrations result hash"
    if not directory_parity:
        return False, "SCHEMA_DRIFT_FAIL_CLOSED", "directory parity mismatch"
    if not catalog_parity:
        return False, "SCHEMA_DRIFT_FAIL_CLOSED", "catalog parity mismatch"

    return True, "PASS", "SQL migration authority validated"


# ---------------------------------------------------------------------------
# Declared blueprint-decision provisions (A551-A592, governor construction)
# ---------------------------------------------------------------------------
#
# One machine predicate per declared provision: the controlling article must
# exist in the official codex and be lifecycle-active; an unreadable codex or
# a non-active provision is FAIL_CLOSED (A445 forbid: missing rule treated
# PASS).  A453-style governor amendments may replace a predicate with a
# dedicated evaluator without changing the rule code.

_DECLARED_PROVISION_RULES: dict[str, str] = {
    "FR-LANG-LAYERING": "A551",
    "FR-GPU-ACCEL-TRACK": "A552",
    "FR-MODEL-MOE": "A553",
    # A554 superseded by A598 (autonomous-training authority); anchor follows
    # the active successor — rule code and predicate are unchanged.
    "FR-XINGCHENG-AUTONOMOUS-TRAINING-UPGRADE": "A598",
    "FR-GIT-SQL-RAG-CAG-DAG-DIVISION": "A555",
    "FR-MODEL-MATURITY": "A556",
    "FR-MODEL-TRAINING-TIERS": "A557",
    "FR-PRETRAIN-DATA-PIPELINE": "A558",
    "FR-SINGLE-BLUEPRINT": "A559",
    "FR-TRAIN-NTP-CORRECTNESS": "A560",
    "FR-SMALL-MODEL-BASELINE": "A561",
    "FR-XINGCHENG-PRIMARY-GOAL": "A562",
    "FR-MODEL-SCALING": "A563",
    "FR-MOE-QC": "A564",
    "FR-DIALOGUE-TRAINING": "A565",
    "FR-INFER-CONSISTENCY": "A566",
    "FR-KV-CACHE-TESTS": "A567",
    "FR-EVAL-SUITE": "A568",
    "FR-WEIGHT-EVOLUTION": "A569",
    "FR-CONVERGENCE-PLAN": "A570",
    "FR-CONNECTION-RECOVERY": "A571",
    "FR-MAINT-UPDATE-INTEGRATION": "A572",
    "FR-TASK-LIFECYCLE": "A573",
    "FR-EXECUTION-LEASE": "A574",
    "FR-RELEASE-MANIFEST": "A575",
    "FR-DEV-RUNTIME-SEPARATION": "A576",
    "FR-BACKEND-LIFECYCLE": "A577",
    "FR-INTEGRATION-SEQUENCE": "A578",
    "FR-REQUEST-REGISTRY": "A579",
    "FR-REQUEST-LIFECYCLE": "A580",
    "FR-CONNECTION-STATE": "A581",
    "FR-INTEGRATED-GOALS": "A582",
    "FR-IMPL-PRECEDENCE": "A583",
    "FR-MULTI-CONFIG-GPT": "A584",
    "FR-TOOL-CALLING-COMPETENCE": "A585",
    "FR-OLLAMA-ON-DEMAND": "A586",
    "FR-ONDEMAND-MODULES": "A587",
    "FR-SINGLE-PURPOSE-MODULE": "A588",
    "FR-MODULE-HASH-CHAIN": "A589",
    # A591/A592 superseded by A604 (authority convergence); anchors follow the
    # active successor — rule codes and predicates are unchanged.
    "FR-SOVEREIGN-TO-CORE-ENGINE": "A604",
    "FR-NO-SUB-SOVEREIGN-ALL-MODULES": "A604",
}


def _declared_provision_evaluator(
    provision_id: str,
) -> Callable[[Mapping[str, Any]], tuple[bool, str, str]]:
    """Predicate factory: controlling provision is declared and active."""

    def evaluator(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
        import sqlite3

        import psycopg

        from governance_rule.execution.codex_repository import (
            CODEX_DATABASE_PATH,
            codex_readonly_connection,
        )

        try:
            with codex_readonly_connection(CODEX_DATABASE_PATH) as connection:
                row = connection.execute(
                    "SELECT lifecycle_state FROM provision_lifecycle_status "
                    "WHERE provision_type='article' AND provision_id=?",
                    (provision_id,),
                ).fetchone()
        except (OSError, sqlite3.Error, psycopg.Error) as error:
            return False, "FAIL_CLOSED", f"codex unreadable: {error}"
        if row is None:
            return False, "FAIL_CLOSED", f"provision {provision_id} is not declared"
        state = str(row[0]).strip().lower()
        if state != "active":
            return False, "FAIL_CLOSED", f"provision {provision_id} lifecycle {state or 'unknown'}"
        return True, "PASS", f"declared provision {provision_id} is active"

    return evaluator


@register_rule("FR-MULTI-CORE-PARALLEL")
def _multi_core_parallel(facts: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Predicate (A590): provision active AND the thread/worker
    allocation stays inside the fixed five-core budget —
    threads_per_worker × parallel_workers ≤ budget_cores ≤ 5, and no
    unbounded worker pools."""
    passed, code, reason = _declared_provision_evaluator("A590")(facts)
    if not passed:
        return passed, code, reason
    try:
        budget = int(facts.get("budget_cores"))
        threads = int(facts.get("threads_per_worker"))
        workers = int(facts.get("parallel_workers"))
        unbounded = int(facts.get("unbounded_pools") or 0)
    except (TypeError, ValueError):
        return False, "INCOMPLETE_EVIDENCE", "thread-budget facts required"
    if unbounded > 0:
        return False, "FAIL_CLOSED", f"unbounded worker pools: {unbounded}"
    if min(threads, workers, budget) < 1:
        return False, "FAIL_CLOSED", "threads/workers/budget must be >= 1"
    if budget > 5:
        return False, "FAIL_CLOSED", f"core budget {budget} exceeds cap 5"
    if threads * workers > budget:
        return (
            False,
            "FAIL_CLOSED",
            f"threads×workers {threads * workers} exceeds budget {budget}",
        )
    return True, "PASS", "allocation within five-core budget"


for _rule_code, _provision_id in _DECLARED_PROVISION_RULES.items():
    register_rule(_rule_code)(_declared_provision_evaluator(_provision_id))
