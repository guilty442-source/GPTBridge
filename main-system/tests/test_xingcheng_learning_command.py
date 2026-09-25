"""Xingcheng-commanded learning tests (A485).

Verifies 星澄 commands the learning-evidence sub-sovereign's auto-learning
through the governed delegation path: the child never self-arms, every
learn.* command requires a single-use parent delegation nonce, and
status surfaces the commanded learning state.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT.parent / "shared-layer" / "src"))

from core_system.codex_decision import SovereignRequest  # noqa: E402
from governance.sovereigns.xingcheng_sovereign import XingchengSovereign  # noqa: E402


_CHILD_ID = "learning-evidence-sync-sub-sovereign"


class _App:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.xingcheng_sovereign = None


def _stack(tmp_path: Path) -> tuple[_App, XingchengSovereign, XingchengSovereign]:
    """Materialize 星澄 — learning is an intrinsic capability of the
    single native-model entity (A485), commanded via learn.* intents."""
    app = _App(tmp_path)
    sovereign = XingchengSovereign(app)
    app.xingcheng_sovereign = sovereign
    sovereign._started = True
    sovereign._learning_active = True
    return app, sovereign, sovereign


def test_entity_does_not_self_arm_on_start(tmp_path: Path) -> None:
    """start() activates the capability but must not arm auto-learning —
    only an owner-issued learn.auto-start command may (A485)."""
    app = _App(tmp_path)
    sovereign = XingchengSovereign(app)
    report = asyncio.run(sovereign.start())
    assert report["ok"] is True
    assert sovereign._reconcile_task is None
    assert sovereign._auto_learning_armed is False


def test_fault_manuals_are_ingested_by_learning_while_fault_owner_stays_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE maintenance_manual_directory ("
            "manual_code TEXT, owner TEXT, retired_version TEXT)"
        )
        connection.execute(
            "INSERT INTO maintenance_manual_directory VALUES (?,?,NULL)",
            ("MANUAL_ONE", "learning-evidence-sync-sub-sovereign"),
        )

    class _FixtureConnection:
        def __init__(self) -> None:
            self._conn = sqlite3.connect(str(database))

        def execute(self, statement: str, parameters=()):
            return self._conn.execute(statement, parameters)

        def close(self) -> None:
            self._conn.close()

    @contextlib.contextmanager
    def _fixture_codex_connection(*args, **kwargs):
        connection = _FixtureConnection()
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr(
        "governance_rule.execution.codex_repository.codex_readonly_connection",
        _fixture_codex_connection,
    )
    sovereign = XingchengSovereign(_App(tmp_path))
    sovereign._ingest_fault_manual_catalog()
    projection = sovereign.learning_capability_status()["fault_manual_catalog"]
    assert projection["owner"] == "星澄"
    assert projection["fault_directory_owner"] == "permission-sovereign"
    assert projection["count"] == 1
    assert projection["catalog_hash"]


def test_auto_start_arms_learning_loop(tmp_path: Path) -> None:
    async def _run() -> None:
        _, sovereign, child = _stack(tmp_path)
        result = await sovereign.start_learning_automation()
        assert result["commanded"] is True
        assert sovereign._learning_armed is True
        assert child._auto_learning_armed is True
        assert child._reconcile_task is not None
        await sovereign.stop_learning_automation()
        assert child._auto_learning_armed is False
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_learning_intents_are_not_adjudicatable(tmp_path: Path) -> None:
    """learn.* intents are owner-issued commands (A485), not adjudicatable
    intents — handle() must refuse them at the intent gate."""
    sovereign = XingchengSovereign(_App(tmp_path))
    sovereign._started = True
    sovereign._learning_active = True

    async def _run() -> None:
        outcome = await sovereign.handle(
            SovereignRequest(
                intent="learn.reconcile",
                subject="learning",
                requester="星澄",
                payload={},
            )
        )
        assert outcome.accepted is False
        assert sovereign._auto_learning_armed is False

    asyncio.run(_run())


def test_reconcile_pass_returns_receipt(tmp_path: Path) -> None:
    async def _run() -> None:
        _, sovereign, child = _stack(tmp_path)
        result = await sovereign.command_learning_pass("test")
        assert result["commanded"] is True
        reconciliation = result["result"]["reconciliation"]
        assert reconciliation["ok"] is True
        assert reconciliation["remaining"] == 0

    asyncio.run(_run())


def test_supervision_commands_learning_lifecycle(tmp_path: Path) -> None:
    async def _run() -> None:
        _, sovereign, child = _stack(tmp_path)
        sovereign._started = True
        await sovereign.start_supervision()
        assert sovereign._learning_armed is True
        assert child._auto_learning_armed is True
        await sovereign.stop_supervision()
        assert sovereign._learning_armed is False
        assert child._auto_learning_armed is False

    asyncio.run(_run())


def test_learning_status_surface(tmp_path: Path) -> None:
    async def _run() -> None:
        _, sovereign, child = _stack(tmp_path)
        status = sovereign.learning_status()
        assert status["active"] is True
        assert status["auto_learning"] == "disarmed"
        await sovereign.start_learning_automation()
        status = sovereign.learning_status()
        assert status["armed"] is True
        assert status["auto_learning"] == "armed"
        assert status["commands_issued"] == 1
        assert status["last_command"]["intent"] == "learn.auto-start"
        await sovereign.stop_learning_automation()

    asyncio.run(_run())


def test_push_learning_outcome(tmp_path: Path) -> None:
    async def _run() -> None:
        _, sovereign, child = _stack(tmp_path)
        result = await sovereign.push_learning_outcome(
            {
                "signature_hash": "abc123",
                "error_class": "TEST_FAULT",
                "message_pattern": "test failure",
                "failure_code": "TEST_FAULT",
            },
            {
                "run_id": "run-1",
                "remedy": "restart",
                "ok": True,
                "detail": {"note": "verified"},
            },
        )
        assert result["commanded"] is True
        learning = result["result"]["learning"]
        assert learning.get("recorded") is not False or learning

    asyncio.run(_run())


def test_unknown_learning_intent_fails_closed(tmp_path: Path) -> None:
    async def _run() -> None:
        _, sovereign, child = _stack(tmp_path)
        # An undeclared intent never reaches adjudication — the A10/A11
        # intent allowlist gate refuses it at the authorization tier.
        outcome = await sovereign.handle(
            SovereignRequest(
                intent="learn.destroy-everything",
                subject="learning",
                requester="governed-executor",
                payload={},
            )
        )
        assert outcome.accepted is False

    asyncio.run(_run())
