"""P2-1 §10.30 unified thread-policy entry + A590 bounded-workers predicate."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared-layer" / "src"))

from shared_layer.performance.thread_budget import (  # noqa: E402
    CORE_BUDGET_CAP,
    THREAD_ENV_VARS,
    allocation_within_budget,
    apply_thread_env,
    bounded_threads,
    bounded_workers,
    core_budget,
    logical_cores,
    thread_env,
)


def test_core_budget_never_exceeds_cap() -> None:
    assert 1 <= core_budget() <= CORE_BUDGET_CAP
    assert core_budget(override=99) <= CORE_BUDGET_CAP
    assert core_budget(override=3) == min(3, CORE_BUDGET_CAP, logical_cores())


def test_bounded_workers_clamped() -> None:
    assert bounded_workers(0) == 1
    assert bounded_workers(2) == min(2, core_budget())
    assert bounded_workers(64) <= core_budget()


def test_bounded_threads_product_invariant() -> None:
    for workers in (1, 2, 5, 16):
        threads = bounded_threads(99, workers)
        assert threads * bounded_workers(workers) <= core_budget()
        assert threads >= 1


def test_thread_env_covers_blas_omp() -> None:
    env = thread_env(4, 2)
    assert set(env) == set(THREAD_ENV_VARS)
    assert all(v == "2" for v in env.values())


def test_apply_thread_env_respects_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    environ: dict[str, str] = {"OMP_NUM_THREADS": "1"}
    applied = apply_thread_env(8, 1, environ=environ)
    assert environ["OMP_NUM_THREADS"] == "1"  # explicit value wins
    assert environ["MKL_NUM_THREADS"] == str(applied)


def test_allocation_within_budget() -> None:
    assert allocation_within_budget(1, 5, 5)
    assert not allocation_within_budget(8, 1, 5)
    assert not allocation_within_budget(1, 5, 9)
    assert not allocation_within_budget(0, 5, 5)
    assert not allocation_within_budget("x", 5, 5)


# ── A590 evaluator predicate ──────────────────────────────────────────

def _evaluator(monkeypatch: pytest.MonkeyPatch, provision_ok: bool = True):
    from governance_rule.execution.formal_rules import evaluators

    evaluators.finalize_registrations()
    monkeypatch.setattr(
        evaluators,
        "_declared_provision_evaluator",
        lambda pid: (
            lambda facts: (
                (True, "PASS", "ok")
                if provision_ok
                else (False, "FAIL_CLOSED", "inactive")
            )
        ),
    )
    from governance_rule.execution.formal_rules import _EVALUATORS

    return _EVALUATORS["FR-MULTI-CORE-PARALLEL"]


def _facts(**over):
    facts = {"budget_cores": 5, "threads_per_worker": 1, "parallel_workers": 5}
    facts.update(over)
    return facts


def test_a590_passes_within_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    ev = _evaluator(monkeypatch)
    assert ev(_facts())[0] is True


def test_a590_fails_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    ev = _evaluator(monkeypatch)
    passed, code, _ = ev(_facts(threads_per_worker=2, parallel_workers=4))
    assert passed is False and code == "FAIL_CLOSED"


def test_a590_fails_budget_above_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    ev = _evaluator(monkeypatch)
    assert ev(_facts(budget_cores=9))[0] is False


def test_a590_fails_unbounded_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    ev = _evaluator(monkeypatch)
    assert ev(_facts(unbounded_pools=2))[0] is False


def test_a590_incomplete_without_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    ev = _evaluator(monkeypatch)
    passed, code, _ = ev({})
    assert passed is False and code == "INCOMPLETE_EVIDENCE"


def test_a590_fail_closed_when_provision_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ev = _evaluator(monkeypatch, provision_ok=False)
    passed, code, _ = ev(_facts())
    assert passed is False and code == "FAIL_CLOSED"
