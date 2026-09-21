"""G49/§10.4 bounded-retry + cooldown policy tests (star-maintenance-policy/v1)."""
from __future__ import annotations

import time

from core_system.maintenance_retry_policy import MaintenanceRetryPolicy


def _policy(tmp_path, **kw) -> MaintenanceRetryPolicy:
    return MaintenanceRetryPolicy(tmp_path / "policy.json", **kw)


def test_first_attempt_allowed(tmp_path):
    pol = _policy(tmp_path)
    assert pol.check("db-down").allowed


def test_retry_interval_blocks_rapid_retry(tmp_path):
    pol = _policy(tmp_path, retry_interval_s=60)
    pol.record_attempt("db-down")
    decision = pol.check("db-down")
    assert not decision.allowed
    assert decision.reason == "retry-interval"
    assert decision.retry_after_s > 0


def test_attempts_exhausted_escalates_after_cooldown(tmp_path):
    pol = _policy(tmp_path, max_attempts=2, retry_interval_s=0)
    pol.record_attempt("pg")
    pol.record_outcome("pg", verified=False)
    pol.record_attempt("pg")
    pol.record_outcome("pg", verified=False)
    # immediately: still inside the cooldown window
    decision = pol.check("pg")
    assert not decision.allowed
    assert decision.reason == "cooldown"
    # after the cooldown window: budget exhausted → escalate
    decision = pol.check("pg", now=time.monotonic() + 10_000)
    assert not decision.allowed
    assert decision.escalated


def test_cooldown_after_exhaustion(tmp_path):
    pol = _policy(tmp_path, max_attempts=1, retry_interval_s=0, cooldown_s=600)
    pol.record_attempt("svc")
    pol.record_outcome("svc", verified=False)
    ledger = pol.ledger_snapshot()["targets"]["svc"]
    assert ledger["cooldown_until"] > 0


def test_verified_attempt_does_not_escalate(tmp_path):
    pol = _policy(tmp_path, max_attempts=2, retry_interval_s=0)
    pol.record_attempt("svc")
    pol.record_outcome("svc", verified=True)
    pol.record_attempt("svc")
    pol.record_outcome("svc", verified=True)
    assert pol.check("svc").allowed


def test_reset_clears_escalation(tmp_path):
    pol = _policy(tmp_path, max_attempts=1, retry_interval_s=0)
    pol.record_attempt("svc")
    pol.check("svc")  # trips attempts-exhausted -> escalated
    assert pol.check("svc").escalated
    pol.reset("svc")
    assert pol.check("svc").allowed


def test_persist_and_reload(tmp_path):
    path = tmp_path / "policy.json"
    pol = MaintenanceRetryPolicy(path, retry_interval_s=0)
    pol.record_attempt("a", pre_state={"state": "degraded"})
    pol2 = MaintenanceRetryPolicy(path)
    ledger = pol2.ledger_snapshot()["targets"]["a"]
    assert ledger["attempts"] == 1
    assert ledger["last_pre_state"] == {"state": "degraded"}


def test_corrupt_ledger_fail_closed(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text("{bad", encoding="utf-8")
    pol = MaintenanceRetryPolicy(path)
    decision = pol.check("anything")
    assert not decision.allowed
    assert "fail-closed" in decision.reason
