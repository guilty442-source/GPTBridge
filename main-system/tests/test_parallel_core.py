"""Sovereign parallel core tests (codex CONCURRENCY: parallel domain checks).

Proves: independent checks overlap in time, dependent stages stay serial,
joins are fail-closed (exception/timeout/mandatory failure), receipts are
machine-readable, and the A446 authorization gate runs its independent
checks on the parallel core while preserving refusal precedence.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT.parent / "shared-layer" / "src"))

from governance.parallel_core import (  # noqa: E402
    DomainCheck,
    SovereignParallelCore,
)
from core_system.codex_decision import SovereignRequest  # noqa: E402


def _request(intent: str = "review.codex") -> SovereignRequest:
    return SovereignRequest(
        intent=intent,
        subject="parallel-core-test",
        requester="test-suite",
        payload={},
    )


@pytest.mark.asyncio
async def test_independent_checks_run_in_parallel() -> None:
    async def slow() -> bool:
        await asyncio.sleep(0.15)
        return True

    started = time.perf_counter()
    core = SovereignParallelCore(label="test-parallel")
    run = await core.run_stage(
        (
            DomainCheck("first", slow),
            DomainCheck("second", slow),
            DomainCheck("third", slow),
        )
    )
    elapsed = time.perf_counter() - started

    assert run.ok is True
    assert elapsed < 0.3, f"checks did not run in parallel: {elapsed:.3f}s"
    assert [item.name for item in run.verdicts] == ["first", "second", "third"]
    assert all(item.ok for item in run.verdicts)
    summary = run.summary()
    assert summary["mode"] == "parallel-independent-checks"
    assert len(summary["checks"]) == 3


@pytest.mark.asyncio
async def test_join_is_fail_closed_on_exception_and_timeout() -> None:
    async def boom() -> bool:
        raise ValueError("deliberate")

    async def too_slow() -> bool:
        await asyncio.sleep(0.5)
        return True

    async def fine() -> bool:
        return True

    core = SovereignParallelCore(label="test-fail-closed", deadline=0.1)
    run = await core.run_stage(
        (
            DomainCheck("boom", boom),
            DomainCheck("slow", too_slow),
            DomainCheck("fine", fine),
        )
    )

    assert run.ok is False
    assert set(run.failed) == {"boom", "slow"}
    assert run.verdict("boom").error == "ValueError"
    assert run.verdict("slow").error == "TimeoutError"
    assert run.verdict("fine").ok is True


@pytest.mark.asyncio
async def test_non_mandatory_failure_does_not_deny_join() -> None:
    core = SovereignParallelCore(label="test-optional")
    run = await core.run_stage(
        (
            DomainCheck("required", lambda: True),
            DomainCheck("optional", lambda: False, mandatory=False),
        )
    )
    assert run.ok is True
    assert run.failed == ()
    assert run.verdict("optional").ok is False


@pytest.mark.asyncio
async def test_dependent_stages_are_serial_and_stop_on_failure() -> None:
    order: list[str] = []

    async def stage_one() -> bool:
        await asyncio.sleep(0.05)
        order.append("stage1")
        return True

    async def stage_two() -> bool:
        order.append("stage2")
        return False

    async def stage_three() -> bool:
        order.append("stage3")
        return True

    core = SovereignParallelCore(label="test-stages")
    runs = await core.run_stages(
        (
            (DomainCheck("one", stage_one),),
            (DomainCheck("two", stage_two),),
            (DomainCheck("three", stage_three),),
        )
    )
    assert order == ["stage1", "stage2"]
    assert [run.ok for run in runs] == [True, False]
    assert len(runs) == 2, "dependent stages must not run after a failed gate"


class _FakeSovereign:
    sovereign_id = "fake-sovereign"

    def __init__(self, *, requester_ok: bool = True, intent_ok: bool = True) -> None:
        self._requester_ok = requester_ok
        self._intent_ok = intent_ok
        self.requester_done = 0.0
        self.intent_done = 0.0

    async def _verify_requester(self, request) -> bool:
        await asyncio.sleep(0.12)
        self.requester_done = time.perf_counter()
        return self._requester_ok

    def _verify_intent(self, intent: str) -> bool:
        self.intent_done = time.perf_counter()
        return self._intent_ok


@pytest.mark.asyncio
async def test_authorization_gate_runs_independent_checks_in_parallel() -> None:
    from governance.execution_pipeline import SovereignExecutionPipeline

    sovereign = _FakeSovereign()
    pipeline = SovereignExecutionPipeline(sovereign)
    started = time.perf_counter()
    gate, details = await pipeline._authorization_gate(_request())
    elapsed = time.perf_counter() - started

    assert gate == "allowed"
    assert elapsed < 0.2, f"gate checks did not overlap: {elapsed:.3f}s"
    assert sovereign.intent_done < sovereign.requester_done, (
        "intent check must not wait for the slow requester check"
    )
    parallel = details["parallel_core"]
    assert parallel["ok"] is True
    assert {item["name"] for item in parallel["checks"]} == {"requester", "intent"}


@pytest.mark.asyncio
async def test_authorization_gate_precedence_is_preserved() -> None:
    from governance.execution_pipeline import SovereignExecutionPipeline

    pipeline = SovereignExecutionPipeline(_FakeSovereign(requester_ok=False))
    gate, _details = await pipeline._authorization_gate(_request())
    assert gate == "UNAUTHORIZED_REQUESTER"

    pipeline = SovereignExecutionPipeline(_FakeSovereign(intent_ok=False))
    gate, _details = await pipeline._authorization_gate(_request())
    assert gate == "UNAUTHORIZED_INTENT"


@pytest.mark.asyncio
async def test_authorization_gate_records_check_error() -> None:
    from governance.execution_pipeline import SovereignExecutionPipeline

    class _Broken(_FakeSovereign):
        async def _verify_requester(self, request) -> bool:
            raise RuntimeError("boom")

    pipeline = SovereignExecutionPipeline(_Broken())
    gate, details = await pipeline._authorization_gate(_request())
    assert gate == "AUTHORIZATION_GATE_ERROR"
    assert details["error"] == "RuntimeError"
    report = details["parallel_core"]
    assert report["ok"] is False
    assert "requester" in report["failed"]
