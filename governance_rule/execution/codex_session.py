"""Controlled codex read sessions — the A435 engine behind ``governance-codex://official``.

法典依據:
- A435 (controlling): ACCESS-CLASSES = SELF_DECLARATION_LOCAL |
  BOUNDED_MACHINE_LOOKUP | REVIEW_SESSION | XINGCHENG_CHINESE_REVIEW.
  This module implements the three real read classes; self-declaration
  performs no codex read (reconciliation lives in ``codex_reconcile``).
- A435: ENTRY governance-codex://official only; permission-sovereign owns
  the entry; every view carries identity attestation + purpose + least
  provision scope + nonce + expiry + session + audit; RESPONSE is typed,
  version-identified and requested-scope-only; ALL-OTHER denied.
- A173: the Chinese mirror is 星澄-only, mediated by this same entry
  (identity + purpose + scope + session + metadata-only audit;
  permission-review exempt).
- A279: certified tooling reads the machine codex through the governed
  repository interface (``codex_repository``); every other reader enters
  here.

A session is bound to (actor, purpose, scope, nonce, expiry, codex version,
revocation generation); expiry, revocation or a codex version change kills
it (A435).  Bounded contexts accumulate reads into a bounded batch digest
flushed as one metadata-only audit record; review sessions audit each read
individually.  Audit never carries codex content (A435 content-in-audit
forbidden).
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Iterable

from governance_rule.execution import codex_dual_key as _dual_key
from governance_rule.execution import codex_entry_state as _state
from governance_rule.execution.codex_repository import (
    CODEX_DATABASE_PATH,
    GovernanceCodex,
    format_codex_version,
    load_governance_codex,
)

ACCESS_BOUNDED = _state.ACCESS_BOUNDED
ACCESS_REVIEW = _state.ACCESS_REVIEW
ACCESS_CHINESE = _state.ACCESS_CHINESE

_DEFAULT_SESSION_TTL = _state.DEFAULT_SESSION_TTL
_DEFAULT_CONTEXT_TTL = _state.DEFAULT_CONTEXT_TTL


def _review_request(actor: str, purpose: str, access_class: str) -> str | None:
    """Deterministic per-request permission review at the official entry.

    Returns a denial code or None.  The entry owner (permission-sovereign)
    manages this review; callers never self-issue permission — an unknown
    or empty actor is always denied (closed-security, fail-closed).
    """
    if not actor:
        return "CODEX_ACTOR_REQUIRED"
    if purpose not in _state.GOVERNED_PURPOSES:
        return "CODEX_PURPOSE_REQUIRED"
    if access_class == ACCESS_CHINESE:
        # A173/A435: 星澄 bypasses permission review only; identity must
        # still be the registered 星澄 identity.
        return None if actor in _state.XINGCHENG_IDS else "CODEX_CHINESE_DENIED"
    try:
        sovereign_ids = frozenset(
            str(s.id) for s in load_governance_codex().sovereigns
        )
    except (OSError, ValueError, KeyError, RuntimeError, ImportError, AttributeError):
        return "CODEX_UNAVAILABLE"
    if access_class == ACCESS_REVIEW:
        if actor in sovereign_ids or actor in _state.REVIEW_COMPONENT_ACTORS:
            return None
        return "CODEX_REVIEW_DENIED"
    if access_class == ACCESS_BOUNDED:
        if (
            actor in sovereign_ids
            or actor in _state.COMPONENT_ACTORS
            or actor in _state.REVIEW_COMPONENT_ACTORS
            or actor in _state.XINGCHENG_IDS
        ):
            return None
        return "CODEX_LOOKUP_DENIED"
    return "CODEX_ACCESS_CLASS_UNKNOWN"


class CodexReadSession:
    """A controlled official-entry read session (A435).

    Instances come from ``open_codex_session`` / ``open_bounded_context`` /
    ``open_chinese_review_session`` only.  Every read is checked against
    liveness (closed/expiry/revocation/version) and the declared least
    scope, then audited — bounded contexts batch their digest, review
    sessions audit each read individually.
    """

    def __init__(
        self,
        *,
        actor: str,
        purpose: str,
        scope: frozenset[str],
        access_class: str,
        ttl_seconds: float,
    ) -> None:
        self._actor = actor
        self._purpose = purpose
        self._scope = scope
        self._access_class = access_class
        self._nonce = secrets.token_hex(16)
        self._expires = time.monotonic() + max(1.0, float(ttl_seconds))
        self._generation = _state.current_revocation()
        self._codex_version = load_governance_codex().codex_version
        # Persist the minted session (nonce uniqueness + lifecycle evidence);
        # a corrupt or unavailable state store fails the open closed.
        _state.register_session_nonce(
            nonce=self._nonce, actor=actor, purpose=purpose,
            access_class=access_class, scope=scope,
            codex_version=self._codex_version, generation=self._generation,
            expires_at=time.time() + max(1.0, float(ttl_seconds)),
        )
        self._closed = False
        self._digest: list[tuple[int, str]] = []
        self._digest_seq = 0
        if access_class != ACCESS_BOUNDED:
            _state.record_session_audit(
                event="session-open",
                actor=actor, purpose=purpose, access_class=access_class,
                scope=scope, codex_version=self._codex_version,
                correlation=self._nonce, result="OPEN",
            )

    # -- session state ---------------------------------------------------

    @property
    def nonce(self) -> str:
        return self._nonce

    @property
    def codex_version(self) -> int:
        return self._codex_version

    @property
    def codex_version_text(self) -> str:
        return format_codex_version(self._codex_version)

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._expires

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _state.close_session_nonce(self._nonce)
        except PermissionError:
            pass
        if self._access_class == ACCESS_BOUNDED:
            self._flush_digest("CLOSED")
        else:
            _state.record_session_audit(
                event="session-close",
                actor=self._actor, purpose=self._purpose,
                access_class=self._access_class, scope=self._scope,
                codex_version=self._codex_version,
                correlation=self._nonce, result="CLOSED",
            )

    def __enter__(self) -> "CodexReadSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- controls ---------------------------------------------------------

    def _codex(self) -> GovernanceCodex:
        if self._closed:
            raise PermissionError("CODEX_SESSION_CLOSED")
        if self.expired:
            self._deny("CODEX_SESSION_EXPIRED")
        if self._generation != _state.current_revocation():
            self._deny("CODEX_SESSION_REVOKED")
        codex = load_governance_codex()
        if codex.codex_version != self._codex_version:
            self._deny("CODEX_VERSION_CHANGED")
        return codex

    def _allows(self, scope_item: str) -> bool:
        kind, _, _name = scope_item.partition(":")
        if scope_item in self._scope:
            return True
        wildcard = f"{kind}:*"
        if wildcard not in self._scope:
            return False
        if self._access_class == ACCESS_REVIEW:
            return True
        return kind in _state.BOUNDED_WILDCARD_KINDS

    def _require(self, scope_item: str) -> GovernanceCodex:
        if not self._allows(scope_item):
            self._deny(f"CODEX_SCOPE_DENIED:{scope_item}")
        codex = self._codex()
        self._audit_read(scope_item, "GRANTED")
        return codex

    def _audit_read(self, scope_item: str, result: str) -> None:
        if self._access_class == ACCESS_BOUNDED:
            self._digest_seq += 1
            self._digest.append((self._digest_seq, scope_item))
            if len(self._digest) >= _state.DIGEST_FLUSH_BOUND:
                self._flush_digest("BATCH_BOUND")
            return
        _state.record_session_audit(
            event="session-read",
            actor=self._actor, purpose=self._purpose,
            access_class=self._access_class, scope=self._scope,
            codex_version=self._codex_version,
            correlation=self._nonce, result=f"{result}:{scope_item}",
        )

    def _flush_digest(self, result: str) -> None:
        if not self._digest:
            return
        count = len(self._digest)
        self._digest.clear()
        _state.record_session_audit(
            event="bounded-digest",
            actor=self._actor, purpose=self._purpose,
            access_class=self._access_class, scope=self._scope,
            codex_version=self._codex_version,
            correlation=self._nonce, result=result, request_count=count,
        )

    def _deny(self, code: str) -> None:
        _state.record_session_audit(
            event="session-deny",
            actor=self._actor, purpose=self._purpose,
            access_class=self._access_class, scope=self._scope,
            codex_version=self._codex_version,
            correlation=self._nonce, result=code,
        )
        raise PermissionError(code)

    # -- typed reads (version-identified, requested-scope-only) -----------

    def codex_identity(self) -> dict[str, Any]:
        """Codex identity tuple: schema, version, authority rank, scope."""
        codex = self._require("codex:identity")
        return {
            "schema": codex.schema,
            "codex_version": codex.codex_version,
            "codex_version_text": format_codex_version(codex.codex_version),
            "authority_rank": codex.preamble.authority_rank,
            "binding_scope": codex.preamble.binding_scope,
        }

    def sovereign(self, sovereign_id: str) -> Any | None:
        """One registered sovereign identity/status/binding record."""
        sid = str(sovereign_id or "").strip()
        codex = self._require(f"sovereign:{sid}")
        for item in codex.sovereigns:
            if item.id == sid:
                return item
        return None

    def sovereigns(self) -> tuple[Any, ...]:
        """All registered sovereign records (review scope only)."""
        codex = self._require("sovereign:*")
        return codex.sovereigns

    def provision_exists(self, reference: str) -> bool:
        """Registered-identity check for a provision token (bounded)."""
        ref = str(reference or "").strip()
        codex = self._require(f"provision:{ref}")
        return self._find_provision(codex, ref) is not None

    def provision_text(self, reference: str) -> str:
        """Rule text for a provision token (REVIEW_SESSION only)."""
        if self._access_class != ACCESS_REVIEW:
            self._deny("CODEX_REVIEW_REQUIRED")
        ref = str(reference or "").strip()
        codex = self._require(f"provision:{ref}")
        found = self._find_provision(codex, ref)
        if found is None:
            raise KeyError(f"unknown provision {ref!r}")
        return found

    @staticmethod
    def _find_provision(codex: GovernanceCodex, reference: str) -> str | None:
        kind = reference[:1]
        if kind == "A":
            pool = ((a.id, a.rule) for a in codex.articles)
        elif kind == "E":
            pool = ((e.id, e.edict) for e in codex.edicts)
        elif kind == "P":
            pool = ((p.id, p.statement) for p in codex.principles)
        else:
            return None
        for provision_id, text in pool:
            if provision_id == reference:
                return text
        return None

    def edicts(self, area: str) -> list[dict[str, str]]:
        """Edicts governing one area (adjudication evidence, review)."""
        if self._access_class != ACCESS_REVIEW:
            self._deny("CODEX_REVIEW_REQUIRED")
        target = str(area or "").strip()
        codex = self._require(f"edicts:{target}")
        return [
            {"id": e.id, "edict": e.edict, "immutability": e.immutability}
            for e in codex.edicts
            if e.area == target
        ]

    def articles(self) -> tuple[Any, ...]:
        if self._access_class != ACCESS_REVIEW:
            self._deny("CODEX_REVIEW_REQUIRED")
        return self._require("articles:*").articles

    def principles(self) -> tuple[Any, ...]:
        if self._access_class != ACCESS_REVIEW:
            self._deny("CODEX_REVIEW_REQUIRED")
        return self._require("principles:*").principles

    def registry_names(self) -> tuple[str, ...]:
        """Registered ``*_registry`` table names (metadata, non-content)."""
        codex = self._require("registry:*")
        return tuple(sorted(codex.registries))

    def directory_names(self) -> tuple[str, ...]:
        """Registered ``*_directory`` table names (metadata, non-content)."""
        codex = self._require("directory:*")
        return tuple(sorted(codex.directories))

    def registry(self, name: str) -> tuple[dict[str, str], ...]:
        """Registered non-content registry rows (A334 machine authority)."""
        table = str(name or "").strip()
        codex = self._require(f"registry:{table}")
        return tuple(
            row.as_dict() for row in codex.registries.get(table, ())
        )

    def directory(self, name: str) -> tuple[dict[str, str], ...]:
        """Registered directory rows (identity/status/binding)."""
        table = str(name or "").strip()
        codex = self._require(f"directory:{table}")
        return tuple(
            row.as_dict() for row in codex.directories.get(table, ())
        )

    def snapshot(self) -> GovernanceCodex:
        """Full codex projection (cross-module inspection; review only)."""
        if self._access_class != ACCESS_REVIEW:
            self._deny("CODEX_REVIEW_REQUIRED")
        return self._require("codex:full")

    def chinese_mirror(self) -> str:
        """星澄-only Chinese mirror text (A173; metadata-only audit)."""
        if self._access_class != ACCESS_CHINESE:
            self._deny("CODEX_CHINESE_DENIED")
        self._require("chinese:mirror")
        from .chinese_codex_mirror import render_chinese_codex

        return render_chinese_codex(CODEX_DATABASE_PATH.parent.parent)


# ---------------------------------------------------------------------------
# Entry points (permission review happens here, fail-closed)
# ---------------------------------------------------------------------------


def open_codex_session(
    actor: str,
    *,
    purpose: str,
    scope: Iterable[str],
    access_class: str = ACCESS_REVIEW,
    ttl_seconds: float = _DEFAULT_SESSION_TTL,
    dual_key_grant: str | None = None,
) -> CodexReadSession:
    """Open a controlled codex read session through the official entry.

    Per-request permission review (A435): actor eligibility is checked
    against the registered sovereign set plus governed component actors;
    星澄's Chinese review is permission-review exempt but identity-bound.
    Every denial is audited; every session carries nonce + expiry + the
    codex version and revocation generation it was minted under.
    """
    actor = str(actor or "").strip()
    purpose = str(purpose or "").strip()
    try:
        parsed_scope = _state.parse_scope(scope)
    except PermissionError:
        _state.record_session_audit(
            event="session-open", actor=actor, purpose=purpose,
            access_class=access_class, scope=frozenset(),
            codex_version=None, correlation="", result="DENIED_SCOPE",
        )
        raise
    denial = _review_request(actor, purpose, access_class)
    if denial is not None:
        _state.record_session_audit(
            event="session-open", actor=actor, purpose=purpose,
            access_class=access_class, scope=parsed_scope,
            codex_version=None, correlation="", result=denial,
        )
        raise PermissionError(denial)
    # A435 two-key boundary: privileged review/amendment opens require a
    # single-use grant countersigned by a distinct registered sovereign.
    if _dual_key.requires_dual_key(access_class, purpose, parsed_scope):
        if not dual_key_grant:
            _state.record_session_audit(
                event="session-open", actor=actor, purpose=purpose,
                access_class=access_class, scope=parsed_scope,
                codex_version=None, correlation="",
                result="CODEX_DUAL_KEY_REQUIRED",
            )
            raise PermissionError("CODEX_DUAL_KEY_REQUIRED")
        _dual_key.verify_dual_key_grant(
            dual_key_grant,
            operation=f"codex-open:{access_class}",
            actor=actor, purpose=purpose, scope=parsed_scope,
            access_class=access_class,
        )
    return CodexReadSession(
        actor=actor, purpose=purpose, scope=parsed_scope,
        access_class=access_class, ttl_seconds=ttl_seconds,
    )


def open_bounded_context(
    actor: str,
    *,
    purpose: str,
    scope: Iterable[str],
    ttl_seconds: float = _DEFAULT_CONTEXT_TTL,
    dual_key_grant: str | None = None,
) -> CodexReadSession:
    """BOUNDED_MACHINE_LOOKUP context (A435): non-content exact lookups."""
    return open_codex_session(
        actor, purpose=purpose, scope=scope,
        access_class=ACCESS_BOUNDED, ttl_seconds=ttl_seconds,
        dual_key_grant=dual_key_grant,
    )


def open_review_session(
    actor: str,
    *,
    purpose: str,
    scope: Iterable[str],
    ttl_seconds: float = _DEFAULT_SESSION_TTL,
    dual_key_grant: str | None = None,
) -> CodexReadSession:
    """REVIEW_SESSION (A435): rule text / evidence / citation reads."""
    return open_codex_session(
        actor, purpose=purpose, scope=scope,
        access_class=ACCESS_REVIEW, ttl_seconds=ttl_seconds,
        dual_key_grant=dual_key_grant,
    )


def open_chinese_review_session(
    actor: str,
    *,
    purpose: str = "global-review",
    scope: Iterable[str] = ("chinese:mirror",),
    ttl_seconds: float = _DEFAULT_SESSION_TTL,
) -> CodexReadSession:
    """XINGCHENG_CHINESE_REVIEW (A173/A435): 星澄-only, review exempt."""
    return open_codex_session(
        actor, purpose=purpose, scope=scope,
        access_class=ACCESS_CHINESE, ttl_seconds=ttl_seconds,
    )


def revoke_codex_read_contexts() -> None:
    """Revoke every outstanding context/session (amendment, recertify)."""
    _state.revoke_codex_read_contexts()


__all__ = [
    "ACCESS_BOUNDED",
    "ACCESS_CHINESE",
    "ACCESS_REVIEW",
    "CodexReadSession",
    "open_bounded_context",
    "open_chinese_review_session",
    "open_codex_session",
    "open_review_session",
    "revoke_codex_read_contexts",
]
