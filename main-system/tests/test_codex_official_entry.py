"""Official-entry authorization tests (A435) — real decisions, not smoke.

Covers persisted entry state (revocation generation, session nonces, grant
records, replay protection), the two-key boundary for privileged opens
(codex:full snapshots and amendment-verification), fail-closed behaviour on
a corrupt state store, the Xingcheng-only Chinese mirror, and metadata-only
audit records (no codex content).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT.parent / "governance_rule"))

from governance_rule.execution import codex_entry_state as state  # noqa: E402
from governance_rule.execution.codex_dual_key import (  # noqa: E402
    mint_dual_key_grant,
    requires_dual_key,
    verify_dual_key_grant,
)
from governance_rule.execution.codex_session import (  # noqa: E402
    open_bounded_context,
    open_chinese_review_session,
    open_review_session,
    revoke_codex_read_contexts,
)


@pytest.fixture
def entry_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated entry-state store (the real one stays untouched)."""
    path = tmp_path / "codex_entry_state.json"
    monkeypatch.setenv("GPTBRIDGE_CODEX_ENTRY_STATE", str(path))
    return path


def _full_review_grant(**overrides: object) -> str:
    kwargs = {
        "operation": "codex-open:review-session",
        "primary_actor": "governance-coordination",
        "secondary_actor": "permission-sovereign",
        "purpose": "coordination",
        "scope": ("codex:full",),
        "access_class": "review-session",
    }
    kwargs.update(overrides)
    return mint_dual_key_grant(**kwargs)  # type: ignore[arg-type]


# -- Real authorization decisions -----------------------------------------


def test_bounded_lookup_granted(entry_state: Path) -> None:
    with open_bounded_context(
        "startup-executor",
        purpose="self-declaration",
        scope=("codex:identity",),
    ) as ctx:
        identity = ctx.codex_identity()
    assert identity["codex_version"] > 0


def test_unknown_actor_denied(entry_state: Path) -> None:
    with pytest.raises(PermissionError):
        open_bounded_context(
            "unregistered-process",
            purpose="self-declaration",
            scope=("codex:identity",),
        )


def test_malformed_scope_denied(entry_state: Path) -> None:
    with pytest.raises(PermissionError):
        open_bounded_context(
            "startup-executor", purpose="self-declaration", scope=("bogus",)
        )


def test_codex_full_requires_dual_key(entry_state: Path) -> None:
    """One key alone cannot open a privileged full-codex review."""
    with pytest.raises(PermissionError, match="CODEX_DUAL_KEY_REQUIRED"):
        open_review_session(
            "governance-coordination",
            purpose="coordination",
            scope=("codex:full",),
        )


def test_dual_key_grant_opens_full_review(entry_state: Path) -> None:
    grant = _full_review_grant()
    with open_review_session(
        "governance-coordination",
        purpose="coordination",
        scope=("codex:full",),
        dual_key_grant=grant,
    ) as session:
        snapshot = session.snapshot()
    assert snapshot.codex_version > 0


def test_dual_key_duplicate_actor_denied(entry_state: Path) -> None:
    with pytest.raises(PermissionError, match="CODEX_DUAL_KEY_DUPLICATE"):
        _full_review_grant(
            primary_actor="permission-sovereign",
            secondary_actor="permission-sovereign",
        )


def test_dual_key_unauthorized_primary_denied(entry_state: Path) -> None:
    with pytest.raises(PermissionError, match="CODEX_PRIMARY_DENIED"):
        _full_review_grant(primary_actor="unregistered-process")


def test_dual_key_non_sovereign_secondary_denied(entry_state: Path) -> None:
    """A component proxy cannot countersign — the second key must be a
    registered sovereign identity."""
    with pytest.raises(PermissionError, match="CODEX_SECONDARY_DENIED"):
        _full_review_grant(secondary_actor="decision-layer")


def test_dual_key_replay_denied(entry_state: Path) -> None:
    """A consumed grant cannot open a second session."""
    grant = _full_review_grant()
    with open_review_session(
        "governance-coordination",
        purpose="coordination",
        scope=("codex:full",),
        dual_key_grant=grant,
    ):
        pass
    with pytest.raises(PermissionError, match="CODEX_GRANT_REPLAY"):
        open_review_session(
            "governance-coordination",
            purpose="coordination",
            scope=("codex:full",),
            dual_key_grant=grant,
        )


def test_dual_key_unknown_grant_denied(entry_state: Path) -> None:
    with pytest.raises(PermissionError, match="CODEX_GRANT_UNKNOWN"):
        open_review_session(
            "governance-coordination",
            purpose="coordination",
            scope=("codex:full",),
            dual_key_grant="0" * 32,
        )


def test_dual_key_purpose_mismatch_denied(entry_state: Path) -> None:
    grant = mint_dual_key_grant(
        operation="codex-open:review-session",
        primary_actor="governance-coordination",
        secondary_actor="permission-sovereign",
        purpose="audit",
        scope=("codex:full",),
        access_class="review-session",
    )
    with pytest.raises(PermissionError, match="CODEX_GRANT_PURPOSE_MISMATCH"):
        verify_dual_key_grant(
            grant,
            operation="codex-open:review-session",
            actor="governance-coordination",
            purpose="coordination",
            scope=("codex:full",),
            access_class="review-session",
        )


def test_dual_key_scope_mismatch_denied(entry_state: Path) -> None:
    grant = _full_review_grant()
    with pytest.raises(PermissionError, match="CODEX_GRANT_SCOPE_MISMATCH"):
        verify_dual_key_grant(
            grant,
            operation="codex-open:review-session",
            actor="governance-coordination",
            purpose="coordination",
            scope=("codex:full", "provision:A1"),
            access_class="review-session",
        )


def test_dual_key_operation_mismatch_denied(entry_state: Path) -> None:
    grant = _full_review_grant()
    with pytest.raises(PermissionError, match="CODEX_GRANT_OPERATION_MISMATCH"):
        verify_dual_key_grant(
            grant,
            operation="codex-open:bounded-machine-lookup",
            actor="governance-coordination",
            purpose="coordination",
            scope=("codex:full",),
            access_class="review-session",
        )


def test_dual_key_expired_grant_denied(entry_state: Path) -> None:
    grant = _full_review_grant()
    data = json.loads(entry_state.read_text(encoding="utf-8"))
    data["grants"][grant]["expires_at"] = 0.0
    entry_state.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PermissionError, match="CODEX_GRANT_EXPIRED"):
        verify_dual_key_grant(
            grant,
            operation="codex-open:review-session",
            actor="governance-coordination",
            purpose="coordination",
            scope=("codex:full",),
            access_class="review-session",
        )


def test_dual_key_revoked_grant_denied(entry_state: Path) -> None:
    grant = _full_review_grant()
    revoke_codex_read_contexts()
    with pytest.raises(PermissionError, match="CODEX_GRANT_REVOKED"):
        verify_dual_key_grant(
            grant,
            operation="codex-open:review-session",
            actor="governance-coordination",
            purpose="coordination",
            scope=("codex:full",),
            access_class="review-session",
        )


def test_amendment_purpose_requires_dual_key(entry_state: Path) -> None:
    with pytest.raises(PermissionError, match="CODEX_DUAL_KEY_REQUIRED"):
        open_bounded_context(
            "codex-amendment-executor",
            purpose="amendment-verification",
            scope=("codex:identity",),
        )


def test_amendment_dual_key_roundtrip(entry_state: Path) -> None:
    grant = mint_dual_key_grant(
        operation="codex-open:bounded-machine-lookup",
        primary_actor="codex-amendment-executor",
        secondary_actor="permission-sovereign",
        purpose="amendment-verification",
        scope=("codex:identity",),
        access_class="bounded-machine-lookup",
    )
    with open_bounded_context(
        "codex-amendment-executor",
        purpose="amendment-verification",
        scope=("codex:identity",),
        dual_key_grant=grant,
    ) as ctx:
        assert ctx.codex_identity()["codex_version"] > 0


# -- Persistence -----------------------------------------------------------


def test_session_nonce_and_close_persisted(entry_state: Path) -> None:
    with open_bounded_context(
        "startup-executor",
        purpose="self-declaration",
        scope=("codex:identity",),
    ) as ctx:
        nonce = ctx.nonce
        data = json.loads(entry_state.read_text(encoding="utf-8"))
        assert nonce in data["sessions"]
        assert data["sessions"][nonce]["closed"] is False
    data = json.loads(entry_state.read_text(encoding="utf-8"))
    assert data["sessions"][nonce]["closed"] is True
    assert nonce in data["consumed_nonces"]


def test_revocation_generation_survives_restart(entry_state: Path) -> None:
    """Generation is read from the store each call — a fresh process (no
    in-memory cache) still sees the revocation."""
    revoke_codex_read_contexts()
    revoke_codex_read_contexts()
    data = json.loads(entry_state.read_text(encoding="utf-8"))
    assert data["revocation_generation"] == 2
    assert state.current_revocation() == 2


def test_revoked_session_denied(entry_state: Path) -> None:
    ctx = open_bounded_context(
        "startup-executor",
        purpose="self-declaration",
        scope=("codex:identity",),
    )
    revoke_codex_read_contexts()
    with pytest.raises(PermissionError, match="CODEX_SESSION_REVOKED"):
        ctx.codex_identity()


def test_corrupt_state_fails_closed(entry_state: Path) -> None:
    entry_state.write_text("{ not json", encoding="utf-8")
    with pytest.raises(PermissionError, match="CODEX_STATE_CORRUPT"):
        open_bounded_context(
            "startup-executor",
            purpose="self-declaration",
            scope=("codex:identity",),
        )
    with pytest.raises(PermissionError, match="CODEX_STATE_CORRUPT"):
        _full_review_grant()


def test_expired_session_denied(entry_state: Path) -> None:
    ctx = open_bounded_context(
        "startup-executor",
        purpose="self-declaration",
        scope=("codex:identity",),
        ttl_seconds=1.0,
    )
    ctx._expires = 0.0  # force expiry without sleeping
    with pytest.raises(PermissionError, match="CODEX_SESSION_EXPIRED"):
        ctx.codex_identity()


def test_unauthorized_attempt_gains_no_authority(entry_state: Path) -> None:
    """A denied open leaves no usable session or grant residue."""
    with pytest.raises(PermissionError):
        open_review_session(
            "unregistered-process",
            purpose="coordination",
            scope=("codex:full",),
        )
    data = (
        json.loads(entry_state.read_text(encoding="utf-8"))
        if entry_state.is_file()
        else {}
    )
    assert data.get("sessions", {}) == {}
    assert data.get("grants", {}) == {}


# -- Chinese mirror + audit shape ------------------------------------------


def test_chinese_mirror_xingcheng_only(entry_state: Path) -> None:
    with pytest.raises(PermissionError):
        open_chinese_review_session("decision-layer")
    with open_chinese_review_session("星澄") as session:
        mirror = session.chinese_mirror()
    assert len(mirror) > 0


def test_audit_records_are_metadata_only() -> None:
    """Audit entries carry identity/purpose/scope-hash/result only (A435)."""
    allowed = {
        "timestamp", "entry", "event", "actor", "purpose", "access_class",
        "scope_hash", "codex_version", "correlation", "result",
        "request_count",
    }
    path = state.AUDIT_PATH
    if not path.is_file():
        pytest.skip("no audit records yet")
    lines = path.read_text(encoding="utf-8").strip().splitlines()[-50:]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # torn concurrent append; not a content violation
    ours = [
        r for r in records if r.get("entry") == "governance-codex://official"
    ]
    assert ours
    for record in ours:
        assert set(record) <= allowed


def test_requires_dual_key_matrix() -> None:
    assert requires_dual_key(
        "review-session", "coordination", frozenset({"codex:full"})
    )
    assert requires_dual_key(
        "bounded-machine-lookup", "amendment-verification",
        frozenset({"codex:identity"}),
    )
    assert not requires_dual_key(
        "review-session", "adjudication", frozenset({"provision:A1"})
    )
    assert not requires_dual_key(
        "xingcheng-chinese-review", "global-review",
        frozenset({"chinese:mirror"}),
    )
