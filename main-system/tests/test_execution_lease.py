"""§10.17 Runtime Execution Lease tests.

Covers: atomic acquire/renew/verify/handover/release, fencing-token
monotonicity, generation binding, stale expiry, persistence.
"""

from __future__ import annotations

import json
import time

from core_system.execution_lease import (
    LEASE_VERSION,
    RuntimeExecutionLease,
)


def test_acquire_grants_token_and_persists(tmp_path):
    path = tmp_path / "execution-lease.json"
    leases = RuntimeExecutionLease(path)
    result = leases.acquire(
        "scheduled-maintenance", "backend-a", "gen-41"
    )
    assert result.ok is True
    assert result.fencing_token == 1
    assert leases.held("scheduled-maintenance")
    assert leases.verify(
        "scheduled-maintenance", "backend-a", "gen-41", 1
    ) is True

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["lease_version"] == LEASE_VERSION
    assert data["leases"][0]["holder"] == "backend-a"

    reloaded = RuntimeExecutionLease(path)
    assert reloaded.held("scheduled-maintenance")
    assert reloaded.verify(
        "scheduled-maintenance", "backend-a", "gen-41", 1
    ) is True


def test_lease_busy_when_held_by_other(tmp_path):
    leases = RuntimeExecutionLease(tmp_path / "r.json")
    ok = leases.acquire("daily-backup", "backend-a", "gen-1")
    assert ok.ok
    other = leases.acquire("daily-backup", "backend-b", "gen-2")
    assert other.ok is False
    assert other.reason == "lease-busy"


def test_self_hold_reported_not_double_acquired(tmp_path):
    leases = RuntimeExecutionLease(tmp_path / "r.json")
    ok = leases.acquire("db-migration", "backend-a", "gen-1")
    assert ok.ok
    again = leases.acquire("db-migration", "backend-a", "gen-1")
    assert again.ok is False
    assert again.reason == "already-held-by-self"


def test_fencing_token_is_monotonic_across_handovers(tmp_path):
    leases = RuntimeExecutionLease(tmp_path / "r.json")
    a = leases.acquire("scheduled-maintenance", "backend-a", "gen-41")
    token_a = a.fencing_token
    h = leases.handover(
        "scheduled-maintenance",
        "backend-a",
        token_a,
        "backend-b",
        "gen-41",
    )
    assert h.ok is True
    assert h.fencing_token == token_a + 1
    assert h.fencing_token > token_a
    # old token no longer valid
    assert (
        leases.verify(
            "scheduled-maintenance", "backend-b", "gen-41", token_a
        )
        is False
    )
    assert (
        leases.verify(
            "scheduled-maintenance", "backend-b", "gen-41", h.fencing_token
        )
        is True
    )


def test_generation_bound_token_rejected(tmp_path):
    leases = RuntimeExecutionLease(tmp_path / "r.json")
    ok = leases.acquire("db-migration", "backend-a", "gen-41")
    assert ok.ok
    # 舊世代程序用同一 token 執行 → 拒絕
    assert (
        leases.verify("db-migration", "backend-a", "gen-40", ok.fencing_token)
        is False
    )


def test_renew_extends_without_new_token(tmp_path):
    leases = RuntimeExecutionLease(tmp_path / "r.json")
    ok = leases.acquire("daily-backup", "backend-a", "gen-1", ttl_seconds=6)
    old_expiry = leases.get("daily-backup").expires_at
    renewed = leases.renew(
        "daily-backup",
        "backend-a",
        "gen-1",
        ok.fencing_token,
        ttl_seconds=60,
    )
    assert renewed.ok is True
    assert renewed.fencing_token == ok.fencing_token
    assert (
        leases.verify(
            "daily-backup", "backend-a", "gen-1", ok.fencing_token
        )
        is True
    )
    assert leases.get("daily-backup").expires_at != old_expiry


def test_expired_holder_must_reacquire_new_token(tmp_path):
    """過期後租約被回收（fail-closed）：舊持有者須重新取得、token 遞增。"""
    leases = RuntimeExecutionLease(
        tmp_path / "r.json", ttl_seconds=1
    )
    ok = leases.acquire("daily-backup", "backend-a", "gen-1")
    time.sleep(1.1)
    assert leases.held("daily-backup") is False
    reok = leases.acquire("daily-backup", "backend-b", "gen-2")
    assert reok.ok is True
    assert reok.fencing_token == ok.fencing_token + 1


def test_renew_mismatched_holder_or_token_rejected(tmp_path):
    leases = RuntimeExecutionLease(tmp_path / "r.json")
    ok = leases.acquire("db-migration", "backend-a", "gen-1")
    wrong_token = leases.renew(
        "db-migration", "backend-a", "gen-1", 999
    )
    assert wrong_token.ok is False
    assert wrong_token.reason == "lease-mismatch"
    wrong_holder = leases.renew(
        "db-migration", "backend-b", "gen-1", ok.fencing_token
    )
    assert wrong_holder.ok is False
    assert wrong_holder.reason == "lease-mismatch"


def test_handover_mismatch_fail_closed(tmp_path):
    leases = RuntimeExecutionLease(tmp_path / "r.json")
    ok = leases.acquire("scheduled-maintenance", "backend-a", "gen-1")
    bad = leases.handover(
        "scheduled-maintenance", "backend-a", 999, "backend-b", "gen-1"
    )
    assert bad.ok is False
    assert bad.reason == "handover-mismatch"
    # 原租約不變
    assert (
        leases.verify(
            "scheduled-maintenance", "backend-a", "gen-1", ok.fencing_token
        )
        is True
    )


def test_release_only_by_holder(tmp_path):
    leases = RuntimeExecutionLease(tmp_path / "r.json")
    ok = leases.acquire("db-migration", "backend-a", "gen-1")
    rejected = leases.release(
        "db-migration", "backend-b", "gen-1", ok.fencing_token
    )
    assert rejected.ok is False
    assert leases.held("db-migration")
    released = leases.release(
        "db-migration", "backend-a", "gen-1", ok.fencing_token
    )
    assert released.ok is True
    assert (
        leases.verify(
            "db-migration", "backend-a", "gen-1", ok.fencing_token
        )
        is False
    )


def test_expire_stale_drops_expired(tmp_path):
    leases = RuntimeExecutionLease(
        tmp_path / "r.json", ttl_seconds=1
    )
    leases.acquire("daily-backup", "backend-a", "gen-1")
    leases.acquire("scheduled-maintenance", "backend-a", "gen-1")
    time.sleep(1.1)
    stats = leases.expire_stale()
    assert stats["dropped"] == 2
    assert leases.snapshot()["count"] == 0


def test_expired_not_held_and_acquirable(tmp_path):
    leases = RuntimeExecutionLease(
        tmp_path / "r.json", ttl_seconds=1
    )
    leases.acquire("db-migration", "backend-a", "gen-1")
    time.sleep(1.1)
    assert leases.held("db-migration") is False
    reok = leases.acquire("db-migration", "backend-b", "gen-2")
    assert reok.ok is True
    assert reok.fencing_token == 2  # monotonic across generations


def test_snapshot_reports_count(tmp_path):
    leases = RuntimeExecutionLease(tmp_path / "r.json")
    leases.acquire("daily-backup", "backend-a", "gen-1")
    snap = leases.snapshot()
    assert snap["lease_version"] == LEASE_VERSION
    assert snap["count"] == 1