"""Git capability tokens, issuer classes and policy binding (A390-428 core).

The tier lists in ``git_tiers.__init__`` answer *how risky* a command is;
this module answers *whether this exact actor may run this exact command
now*.  Tier 1 stays token-free, Tier 2 needs a scoped token, Tier 3 needs a
token issued by the governance authority class only.

Companion modules:
  * ``capability_ledger`` — append-only single-use ledger (JSONL);
  * ``capability_verify`` — the documented verification order;
  * ``capability_time`` — UTC parsing/formatting shared by all three.

Integrity:
  * With ``GPTBRIDGE_CAPABILITY_HMAC_KEY`` set (environment only, never
    committed), tokens are HMAC-SHA256 signed and verified.
  * Without a key the token is explicitly marked
    ``UNSIGNED_DEVELOPMENT_CAPABILITY``; it is never presented as
    cryptographically secure.  ``require_signature=True`` rejects it.

Issuer rules: Tier 3 is restricted to ``IssuerClass.GOVERNANCE_AUTHORITY``;
``SYSTEM_SAFE_AUTOMATION`` may issue Tier-2 tokens only for the declared
whitelist and can never issue Tier 3.  An actor can never issue for itself
(``CAPABILITY_SELF_ISSUED``); tokens are frozen, so extending expiry,
changing scope or reviving a consumed id fails as
``CAPABILITY_MUTATION_FORBIDDEN`` / ``CAPABILITY_ALREADY_CONSUMED``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "AI_ACTOR_PREFIXES",
    "AUTOMATION_ACTOR_PREFIXES",
    "CAPABILITY_LEDGER_ENV",
    "CapabilityContext",
    "CapabilityError",
    "CapabilityLedger",
    "CapabilityToken",
    "DEFAULT_LEDGER_PATH",
    "FAILURE_CODES",
    "HMAC_ALGORITHM",
    "HMAC_KEY_ENV",
    "IssuerClass",
    "LEGACY_COMPATIBILITY_MARKER",
    "SYSTEM_SAFE_DENIED_MARKERS",
    "SYSTEM_SAFE_TIER2_OPERATIONS",
    "UNSIGNED_MARKER",
    "VERIFICATION_ORDER",
    "VerificationResult",
    "is_ai_actor",
    "is_automation_actor",
    "issue_capability",
    "policy_digest",
    "policy_version",
    "repository_id_for",
    "scope_allows",
    "sign_token",
    "system_safe_operation_allowed",
    "token_payload_digest",
    "unsigned_payload",
    "verify_capability",
    "verify_token_signature",
]

HMAC_KEY_ENV = "GPTBRIDGE_CAPABILITY_HMAC_KEY"
UNSIGNED_MARKER = "UNSIGNED_DEVELOPMENT_CAPABILITY"
HMAC_ALGORITHM = "HMAC-SHA256"

#: every legacy (non-token) authorization path records this marker.
LEGACY_COMPATIBILITY_MARKER = "DEPRECATED_COMPATIBILITY"

# 425 failure-code inventory for capability verification.
FAILURE_CODES: tuple[str, ...] = (
    "CAPABILITY_MALFORMED",
    "CAPABILITY_INTEGRITY_INVALID",
    "CAPABILITY_UNSIGNED_REJECTED",
    "CAPABILITY_NOT_YET_VALID",
    "CAPABILITY_EXPIRED",
    "CAPABILITY_REPLAY",
    "CAPABILITY_ALREADY_CONSUMED",
    "CAPABILITY_MUTATION_FORBIDDEN",
    "CAPABILITY_REPOSITORY_MISMATCH",
    "CAPABILITY_ACTOR_MISMATCH",
    "CAPABILITY_OPERATION_MISMATCH",
    "CAPABILITY_COMMAND_MISMATCH",
    "CAPABILITY_WORKTREE_MISMATCH",
    "CAPABILITY_BRANCH_MISMATCH",
    "CAPABILITY_REF_MISMATCH",
    "CAPABILITY_REVISION_MISMATCH",
    "CAPABILITY_TARGET_REVISION_MISMATCH",
    "CAPABILITY_GENERATION_MISMATCH",
    "CAPABILITY_POLICY_CHANGED",
    "CAPABILITY_TIER_FORBIDDEN",
    "CAPABILITY_ISSUER_NOT_AUTHORIZED",
    "CAPABILITY_SELF_ISSUED",
    "CAPABILITY_SCOPE_VIOLATION",
    "CAPABILITY_MISSING",
    "CAPABILITY_COORDINATOR_SELF_AUTHORIZATION",
)

VERIFICATION_ORDER: tuple[str, ...] = (
    "parse", "integrity", "expiry", "replay", "repository", "actor",
    "operation", "worktree", "branch", "ref", "revision", "generation",
    "policy", "tier", "consume",
)

# SYSTEM_SAFE_AUTOMATION issuer-class Tier-2 whitelist, expressed in the
# canonical operation keys produced by ``command_normalizer.operation_key``
# (subcommand + sub-action + long option names).  Nothing destructive or
# history-rewriting ever appears here; the denied-marker scan keeps
# option-carrying operations (``commit --amend``) out even though their bare
# subcommand is whitelisted.  Integration/maintenance operations the governed
# automation actually performs (merge, recovery anchors, bundles, pack
# upkeep, governed push) are included; history rewriting, removals and
# force flags are not.
SYSTEM_SAFE_TIER2_OPERATIONS: frozenset[str] = frozenset(
    {
        "add", "commit", "stash", "stash push", "stash pop", "stash apply",
        "branch", "branch create", "switch --create", "switch -c",
        "worktree add", "worktree lock", "worktree unlock", "worktree prune",
        "fetch", "restore", "mv", "tag --annotate", "tag create",
        "commit-graph write", "multi-pack-index write",
        "merge", "merge-tree", "update-ref", "bundle create", "init",
        "remote add", "remote set-url", "gc", "push",
    }
)
SYSTEM_SAFE_DENIED_MARKERS: tuple[str, ...] = (
    "--force", "-f", "-D", "--delete", "--amend", "--hard", "--soft",
    "--prune", "--interactive", "--root", "expire", "filter-branch",
    "filter-repo", "drop", "clear", "clean", "--mirror", "+",
)

AI_ACTOR_PREFIXES: tuple[str, ...] = (
    "ai", "agent", "assistant", "model", "xingcheng", "kilo", "worker",
    "coordinator", "automation", "governance/coordinator",
    "governance/automation",
)
AUTOMATION_ACTOR_PREFIXES: tuple[str, ...] = (
    "coordinator", "automation", "governance/coordinator",
    "governance/automation", "system", "worker", "ai", "agent", "kilo",
)


def _denied_marker_present(operation: str, marker: str) -> bool:
    """Token-aware denied-marker match.

    Dash markers match a whole option token (``--file`` never matches the
    short ``-f`` marker); plain-word markers keep the substring semantics
    used for sub-actions (``stash drop``, ``reflog expire``).
    """
    if marker.startswith("-"):
        for token in operation.split():
            if token.split("=", 1)[0] == marker:
                return True
        return False
    return marker in operation


def system_safe_operation_allowed(operation: str) -> bool:
    lower = str(operation or "").casefold()
    if any(
        _denied_marker_present(lower, marker)
        for marker in SYSTEM_SAFE_DENIED_MARKERS
    ):
        return False
    return any(
        lower == entry or lower.startswith(entry + " ")
        for entry in SYSTEM_SAFE_TIER2_OPERATIONS
    )


class CapabilityError(Exception):
    """Issuance/consumption rejection carrying a 425 failure code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class IssuerClass(str, Enum):
    USER_CONFIRMATION = "USER_CONFIRMATION"
    GOVERNANCE_AUTHORITY = "GOVERNANCE_AUTHORITY"
    SYSTEM_SAFE_AUTOMATION = "SYSTEM_SAFE_AUTOMATION"

    @classmethod
    def parse(cls, issuer: str) -> "IssuerClass | None":
        prefix = str(issuer or "").split(":", 1)[0].strip().casefold()
        for member in cls:
            if member.value.casefold() == prefix:
                return member
        return None

    def qualify(self, identity: str) -> str:
        return f"{self.value}:{identity}"


def _matches_prefix(value: str, prefixes: Iterable[str]) -> bool:
    text = str(value or "").strip().casefold()
    for prefix in prefixes:
        if text == prefix:
            return True
        if text.startswith(prefix) and text[len(prefix):][:1] in (
            ":", "/", "-", " ", ".", "@", "\\",
        ):
            return True
    return False


def is_ai_actor(actor: str) -> bool:
    return _matches_prefix(actor, AI_ACTOR_PREFIXES)


def is_automation_actor(actor: str) -> bool:
    return _matches_prefix(actor, AUTOMATION_ACTOR_PREFIXES)


# ---------------------------------------------------------------------------
# policy binding + repository identity
# ---------------------------------------------------------------------------


def policy_version() -> str:
    return "git-capability-policy-v1"


def policy_digest() -> str:
    from . import TIER1_OPS, TIER2_OPS, TIER3_OPS

    payload = {
        "version": policy_version(),
        "tier1": sorted(TIER1_OPS),
        "tier2": sorted(TIER2_OPS),
        "tier3": sorted(TIER3_OPS),
        "system_safe_tier2": sorted(SYSTEM_SAFE_TIER2_OPERATIONS),
        "issuer_rules": {
            "tier2": ["USER_CONFIRMATION", "GOVERNANCE_AUTHORITY",
                      "SYSTEM_SAFE_AUTOMATION"],
            "tier3": ["GOVERNANCE_AUTHORITY"],
        },
        "verification_order": list(VERIFICATION_ORDER),
        "failure_codes": list(FAILURE_CODES),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def repository_id_for(path: str | Path) -> str:
    """Stable repository identity from the resolved checkout path."""
    resolved = str(Path(path).resolve())
    return hashlib.sha256(resolved.casefold().encode("utf-8")).hexdigest()


def scope_allows(scope: Sequence[str], operation: str, tier: int) -> bool:
    tokens = {str(item) for item in scope}
    if "*" in tokens:
        return True
    tier_ok = f"tier{tier}" in tokens
    op_ok = any(
        token == operation or token == f"op:{operation}"
        for token in tokens
    )
    return tier_ok and op_ok


def _scope_for(
    scope: Sequence[str] | None, operation: str, tier: int,
) -> tuple[str, ...]:
    chosen = tuple(dict.fromkeys(str(item) for item in (scope or ())))
    if not chosen:
        chosen = (f"tier{tier}", f"op:{operation}")
    return chosen


# ---------------------------------------------------------------------------
# token model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityToken:
    """Proposal-scoped, single-use Git operation authority (frozen)."""

    capability_id: str
    issuer: str
    actor: str
    repository_id: str
    operation: str
    tier: int
    worktree_id: str = ""
    branch: str = ""
    target_ref: str = ""
    command_digest: str = ""
    proposal_id: str = ""
    proposal_generation: int = 0
    source_revision: str = ""
    target_revision: str = ""
    issued_at: str = ""
    not_before: str = ""
    expires_at: str = ""
    nonce: str = ""
    single_use: bool = True
    policy_version: str = ""
    policy_digest: str = ""
    scope: tuple[str, ...] = ()
    key_id: str = ""
    algorithm: str = UNSIGNED_MARKER
    signature: str | None = None

    def issuer_class(self) -> IssuerClass | None:
        return IssuerClass.parse(self.issuer)

    def integrity_state(self) -> str:
        return HMAC_ALGORITHM if self.signature else UNSIGNED_MARKER

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"] = list(self.scope)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityToken":
        known = {f for f in cls.__dataclass_fields__}
        required = known - {"signature"}
        missing = sorted(required - set(data))
        if missing:
            raise ValueError(
                f"missing capability fields: {', '.join(missing)}"
            )
        payload = {key: value for key, value in data.items() if key in known}
        payload["scope"] = tuple(payload.get("scope") or ())
        payload["tier"] = int(payload.get("tier", 0))
        payload["proposal_generation"] = int(
            payload.get("proposal_generation", 0)
        )
        payload["single_use"] = bool(payload.get("single_use", True))
        return cls(**payload)


def unsigned_payload(token: CapabilityToken) -> dict[str, Any]:
    data = token.to_dict()
    data.pop("signature", None)
    return data


def token_payload_digest(token: CapabilityToken) -> str:
    encoded = json.dumps(
        unsigned_payload(token), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# signing (HMAC when keyed; explicit unsigned development otherwise)
# ---------------------------------------------------------------------------


def _hmac_key(explicit: str | None = None) -> bytes | None:
    raw = explicit if explicit is not None else os.environ.get(HMAC_KEY_ENV, "")
    return raw.encode("utf-8") if raw else None


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _hmac_digest(material: bytes, token: CapabilityToken) -> str:
    encoded = json.dumps(
        unsigned_payload(token), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    ).encode("utf-8")
    return hmac.new(material, encoded, hashlib.sha256).hexdigest()


def sign_token(token: CapabilityToken, *, key: str | None = None) -> CapabilityToken:
    """Attach HMAC-SHA256, or mark the token as unsigned development."""
    material = _hmac_key(key)
    if material is None:
        return replace(
            token, algorithm=UNSIGNED_MARKER, key_id="", signature=None,
        )
    staged = replace(
        token, algorithm=HMAC_ALGORITHM, key_id=_key_id(material),
        signature=None,
    )
    return replace(staged, signature=_hmac_digest(material, staged))


def verify_token_signature(
    token: CapabilityToken, *, key: str | None = None,
) -> bool:
    material = _hmac_key(key)
    if material is None or not token.signature:
        return False
    return hmac.compare_digest(
        _hmac_digest(material, replace(token, signature=None)),
        token.signature,
    )


# ---------------------------------------------------------------------------
# companion re-exports (stable import surface: capability.*)
# ---------------------------------------------------------------------------

from .capability_ledger import (  # noqa: E402
    CAPABILITY_LEDGER_ENV,
    DEFAULT_LEDGER_PATH,
    CapabilityLedger,
)

_LAZY_EXPORTS: dict[str, str] = {
    "CapabilityContext": "capability_verify",
    "VerificationResult": "capability_verify",
    "verify_capability": "capability_verify",
    "issue_capability": "capability_issue",
}


def __getattr__(name: str) -> Any:
    """Lazily expose companion names without circular imports."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(f".{module_name}", __package__)
    return getattr(module, name)
