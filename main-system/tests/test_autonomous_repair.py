"""Autonomous repair chain regression tests.

Covers the tool-isolation crash evidence pipeline (expected-stop
suppression, managed stderr capture, quarantine diagnosis, governed
crash-repair signaling with dedup), the two-tier decision gate
(mutation vs stability), runtime-recovery dispatch, health-signal
classification propagation, and the learned-recipe knowledge loop
(actions derivation, applicability verification, failure suppression).
"""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))
sys.path.insert(0, str(ROOT / "main-system"))

from core_system.tool_isolation import ToolIsolationManager
from core_system.tool_isolation_types import ToolIsolationEntry
from core_system.repair_decision_chain import RepairDecisionChain
from core_system.maintenance_repair_chain import MaintenanceRepairChainMixin
from core_system import auto_action_policy
from governance.sovereigns.xingcheng.auto import XingchengAutoMixin
from tasks.central_repair import CentralRepairService
from tasks.repair_learning import (
    LearnedRecipe,
    RepairLearner,
    RepairLearningStore,
    RepairOutcome,
    _normalize_error_signature,
    learning_database_root,
)
from tasks.repair_planning import plan_repair


@pytest.fixture
def repair_schema():
    import uuid

    schema = "ar_test_" + uuid.uuid4().hex[:12]
    import psycopg

    from shared_layer.security.dsn_policy import DsnPurpose, resolve_dsn

    dsn = resolve_dsn(DsnPurpose.ADMIN).dsn
    with psycopg.connect(dsn, connect_timeout=5) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        c.execute(f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO gptbridge_runtime')
        c.commit()
    try:
        yield schema
    finally:
        try:
            with psycopg.connect(dsn, connect_timeout=5) as c:
                c.execute(f'DROP SCHEMA "{schema}" CASCADE')
                c.commit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dead_process(exit_code: int = 3) -> subprocess.Popen:
    """Return an already-exited process with a real pid and returncode."""
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import sys; sys.exit({exit_code})"]
    )
    proc.wait(timeout=10)
    return proc


def _manager(tmp_path: Path) -> ToolIsolationManager:
    manager = ToolIsolationManager(tmp_path)
    manager._load_policy_config = lambda: {
        "defaults": {"restart_on_crash": False},
        "crash_containment": {
            "isolate_on_crash": True,
            "quarantine_dir": "quarantine",
            "max_quarantine_entries": 20,
        },
    }
    return manager


def _entry(tool_id: str, proc: subprocess.Popen, tmp_path: Path) -> ToolIsolationEntry:
    log_root = tmp_path / "logs" / tool_id
    log_root.mkdir(parents=True, exist_ok=True)
    return ToolIsolationEntry(
        tool_id=tool_id,
        pid=proc.pid,
        process=proc,
        log_root=str(log_root),
    )


def _requests(tmp_path: Path) -> list[dict]:
    path = tmp_path / "main-system" / "runtime" / "state" / "repair-requests.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Expected-stop suppression (intentional shutdown is not a crash)
# ---------------------------------------------------------------------------


def test_expected_stop_is_not_reported_as_crash(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proc = _dead_process(exit_code=1)
    entry = _entry("demo-tool", proc, tmp_path)
    manager._entries["demo-tool"] = entry
    manager.mark_expected_stop("demo-tool")

    health = manager.check_tool_health("demo-tool")
    assert health["status"] == "stopped"
    assert health.get("expected_stop") is True

    result = manager.handle_crash("demo-tool")
    assert result["action"] == "expected-stop"

    # No quarantine evidence, no repair signal for deliberate lifecycle.
    assert not list((tmp_path / "quarantine").glob("*.json"))
    assert _requests(tmp_path) == []


def test_unexpected_exit_is_a_crash(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proc = _dead_process(exit_code=3)
    entry = _entry("demo-tool", proc, tmp_path)
    manager._entries["demo-tool"] = entry

    health = manager.check_tool_health("demo-tool")
    assert health["status"] == "crashed"


# ---------------------------------------------------------------------------
# Crash evidence: stderr tail, diagnosis, quarantine record, repair signal
# ---------------------------------------------------------------------------

CRASH_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "src/channel_runtime.py", line 42, in <module>\n'
    "    run()\n"
    "IndentationError: unexpected indent\n"
)


def test_stderr_tail_is_bounded(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proc = _dead_process()
    entry = _entry("demo-tool", proc, tmp_path)
    log = Path(entry.log_root) / "stderr.log"
    log.write_text("\n".join(f"line-{i}" for i in range(100)), encoding="utf-8")
    tail = manager._tool_stderr_tail("demo-tool", entry)
    assert len(tail) == 40
    assert tail[-1] == "line-99"


def test_diagnose_crash_indentation_is_targeted(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proc = _dead_process()
    entry = _entry("demo-tool", proc, tmp_path)
    Path(entry.log_root, "stderr.log").write_text(CRASH_TRACEBACK, encoding="utf-8")
    diagnosis = manager._diagnose_crash("demo-tool", entry)
    assert diagnosis["action"] == "targeted"
    assert diagnosis["error_type"] == "IndentationError"
    assert diagnosis["tool_id"] == "demo-tool"
    assert diagnosis["exit_code"] == 3
    assert diagnosis["stderr_tail"]


def test_diagnose_crash_runtime_error_is_recovery(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proc = _dead_process()
    entry = _entry("demo-tool", proc, tmp_path)
    Path(entry.log_root, "stderr.log").write_text(
        "Traceback (most recent call last):\n"
        '  File "src/main.py", line 9, in <module>\n'
        "RuntimeError: channel closed\n",
        encoding="utf-8",
    )
    diagnosis = manager._diagnose_crash("demo-tool", entry)
    assert diagnosis["action"] == "runtime-recovery"
    assert diagnosis["error_type"] == "RuntimeError"


def test_handle_crash_quarantines_with_evidence_and_signals(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proc = _dead_process(exit_code=3)
    entry = _entry("demo-tool", proc, tmp_path)
    Path(entry.log_root, "stderr.log").write_text(CRASH_TRACEBACK, encoding="utf-8")
    manager._entries["demo-tool"] = entry

    result = manager.handle_crash("demo-tool")
    assert result["action"] == "quarantined"

    records = list((tmp_path / "quarantine").glob("demo-tool-*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["tool_id"] == "demo-tool"
    assert record["exit_code"] == 3
    assert record["diagnosis"]["action"] == "targeted"
    assert record["diagnosis"]["stderr_tail"]

    requests = _requests(tmp_path)
    assert len(requests) == 1
    request = requests[0]
    assert request["failure_code"] == "TOOL_RUNTIME_CRASH"
    assert request["signal_only"] is True
    proof = request["decision_proof"]
    assert proof["diagnosis"]["tool_id"] == "demo-tool"
    assert proof["diagnosis"]["stderr_tail"]


def test_crash_signal_dedupes_open_request(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proc = _dead_process()
    entry = _entry("demo-tool", proc, tmp_path)
    diagnosis = manager._diagnose_crash("demo-tool", entry)

    manager._signal_crash_repair("demo-tool", entry, diagnosis)
    manager._signal_crash_repair("demo-tool", entry, diagnosis)

    requests = _requests(tmp_path)
    assert len(requests) == 1


def test_crash_signal_allows_new_fault_after_resolution(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proc = _dead_process()
    entry = _entry("demo-tool", proc, tmp_path)
    diagnosis = manager._diagnose_crash("demo-tool", entry)
    manager._signal_crash_repair("demo-tool", entry, diagnosis)
    requests = _requests(tmp_path)
    assert len(requests) == 1
    # Simulate the decision-sovereign resolving the request.
    requests[0]["status"] = "approved-repair-completed"
    path = tmp_path / "main-system" / "runtime" / "state" / "repair-requests.json"
    path.write_text(json.dumps(requests), encoding="utf-8")
    manager._signal_crash_repair("demo-tool", entry, diagnosis)
    assert len(_requests(tmp_path)) == 2


# ---------------------------------------------------------------------------
# Health classification: crash signal -> runtime-recovery tier
# ---------------------------------------------------------------------------


def _maintenance_stub() -> MaintenanceRepairChainMixin:
    return object.__new__(MaintenanceRepairChainMixin)


def test_internal_fault_notifies_xingcheng_and_keeps_assistant_status_only() -> None:
    routed: list[dict] = []
    decision = SimpleNamespace(
        decide_and_route_repair=lambda signal: (
            routed.append(signal) or {"ok": True, "decision": "approved-repair-completed"}
        )
    )
    xingcheng = object.__new__(XingchengAutoMixin)
    xingcheng.app = SimpleNamespace(decision_sovereign=decision)
    xingcheng.sovereign_id = "星澄"
    xingcheng._auto_metrics = {
        "internal_fault_notifications": 0,
        "autonomous_repairs_requested": 0,
    }
    result = xingcheng.handle_internal_fault_repair(_crash_classified())
    assert routed
    assert result["ok"] is True
    assert result["autonomous_owner"] == "星澄"
    assert result["notification_channel"] == "internal-information-layer"
    assert result["assistant_notification"] == "status-only"
    assert result["user_confirmation"] == "not-required"


def test_real_sovereign_init_preserves_full_auto_metrics() -> None:
    """Regression: mixin __init__ chain must not clobber _auto_metrics.

    Each mixin calls super().__init__() before assigning _auto_metrics, so
    the LAST assignment in the unwind order wins. XingchengDomainMixin sits
    earlier in the bases and used to overwrite the richer XingchengAutoMixin
    dict — dropping internal_fault_notifications /
    autonomous_repairs_requested / learning_commands and making
    handle_internal_fault_repair raise KeyError on every signal (silently
    swallowed by the maintenance loop, leaving requests pending forever).
    """
    from governance.sovereigns.xingcheng_sovereign import XingchengSovereign

    decision = SimpleNamespace(
        decide_and_route_repair=lambda signal, **kw: {
            "ok": True,
            "decision": "approved-repair-completed",
        }
    )
    sovereign = XingchengSovereign(
        SimpleNamespace(decision_sovereign=decision, project_root=None)
    )
    metrics = sovereign._auto_metrics
    for key in (
        "observe_cycles",
        "analyze_cycles",
        "learning_commands",
        "internal_fault_notifications",
        "autonomous_repairs_requested",
        "self_upgrade_owner",
    ):
        assert key in metrics, f"missing _auto_metrics key: {key}"

    result = sovereign.handle_internal_fault_repair(_crash_classified())
    assert result["ok"] is True
    assert metrics["internal_fault_notifications"] == 1
    assert metrics["autonomous_repairs_requested"] == 1


def test_classify_crash_signal_maps_to_runtime_recovery() -> None:
    mixin = _maintenance_stub()
    diagnosis = {
        "action": "runtime-recovery",
        "tool_id": "demo-tool",
        "error_type": "RuntimeError",
        "file": "src/main.py",
        "stderr_tail": ["RuntimeError: channel closed"],
    }
    classified = mixin._classify_health_signal(
        {"diagnosis": diagnosis, "stderr_tail": diagnosis["stderr_tail"]},
        request={"failure_code": "TOOL_RUNTIME_CRASH"},
    )
    assert classified["action"] == "runtime-recovery"
    assert classified["tool_id"] == "demo-tool"
    assert classified["failure_code"] == "TOOL_RUNTIME_CRASH"
    assert classified["remedy"] == "runtime-recovery"
    assert classified["stderr_tail"]


def test_classify_targeted_crash_stays_mutation_tier() -> None:
    mixin = _maintenance_stub()
    diagnosis = {
        "action": "targeted",
        "tool_id": "demo-tool",
        "error_type": "IndentationError",
        "file": "src/channel_runtime.py",
    }
    classified = mixin._classify_health_signal(
        {"diagnosis": diagnosis},
        request={"failure_code": "TOOL_RUNTIME_CRASH"},
    )
    assert classified["action"] == "targeted"
    assert classified["remedy"] == "targeted-source-repair"


# ---------------------------------------------------------------------------
# Two-tier decision gate
# ---------------------------------------------------------------------------


class _PermStub:
    def authorize(self, **kwargs):
        return None


def _chain(tmp_path: Path, *, maintenance=None) -> RepairDecisionChain:
    app = SimpleNamespace(
        project_root=tmp_path,
        maintenance_sovereign=maintenance,
        permission_sovereign=_PermStub(),
    )
    return RepairDecisionChain(app)


def _crash_classified() -> dict:
    return {
        "error_type": "RuntimeError",
        "target_file": "",
        "action": "runtime-recovery",
        "failure_code": "TOOL_RUNTIME_CRASH",
        "tool_id": "demo-tool",
        "remedy": "runtime-recovery",
        "diagnosis": {"action": "runtime-recovery", "tool_id": "demo-tool"},
    }


def _mutation_classified(target_file: str) -> dict:
    return {
        "error_type": "IndentationError",
        "target_file": target_file,
        "action": "targeted",
        "failure_code": "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED",
        "remedy": "targeted-source-repair",
        "diagnosis": {"action": "targeted", "file": target_file},
    }


def test_stability_tier_executes_under_audit_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GPTBRIDGE_RENDERER_DEV_URL", raising=False)
    chain = _chain(tmp_path)
    monkeypatch.setattr(
        chain,
        "_dispatch_to_runtime",
        lambda classified, decision: {
            "ok": True,
            "dispatch": "system-runtime",
            "errors": [],
            "workflow_status": "completed",
        },
    )
    result = chain.decide_and_route(_crash_classified())
    assert result["decision"] == "approved-repair-completed"
    assert result["ok"] is True
    assert result["audit"]["flow"] == "system-audit"


def test_mutation_tier_executes_without_user_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GPTBRIDGE_RENDERER_DEV_URL", raising=False)
    target = tmp_path / "src" / "broken.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    chain = _chain(tmp_path)
    monkeypatch.setattr(
        chain,
        "_dispatch_to_programming",
        lambda classified, decision: {"ok": True, "method": "targeted"},
    )
    result = chain.decide_and_route(_mutation_classified("src/broken.py"))
    assert result["ok"] is True
    assert result["decision"] == "approved-repair-completed"


def test_mutation_tier_proceeds_when_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GPTBRIDGE_RENDERER_DEV_URL", raising=False)
    target = tmp_path / "src" / "broken.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    chain = _chain(tmp_path)
    monkeypatch.setattr(
        chain,
        "_dispatch_to_programming",
        lambda classified, decision: {"ok": True, "method": "targeted"},
    )
    result = chain.decide_and_route(
        _mutation_classified("src/broken.py"), user_confirmed=True
    )
    assert result["ok"] is True
    assert result["verification"]["verification"] == "independent-compile-ok"


class _AcceptanceStub:
    def __init__(self, *, submit_ok: bool = True) -> None:
        self._submit_ok = submit_ok
        self.submitted: list = []
        self.accepted: list = []

    def submit_change(self, change_id, spec):
        self.submitted.append((change_id, spec))
        return self._submit_ok

    def accept_change(self, change_id, result):
        self.accepted.append((change_id, result))
        return True


def test_audit_intake_registers_and_accepts_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GPTBRIDGE_RENDERER_DEV_URL", raising=False)
    # A604: even a materialized retired identity must never be consulted —
    # change-acceptance intake resolves to None and the chain records
    # "skipped-unavailable" instead of submitting to a retired child.
    acceptance = _AcceptanceStub()
    app = SimpleNamespace(
        project_root=tmp_path,
        permission_sovereign=_PermStub(),
        _sub_sovereigns={"change-acceptance-sub-sovereign": acceptance},
    )
    chain = RepairDecisionChain(app)
    monkeypatch.setattr(
        chain,
        "_dispatch_to_runtime",
        lambda classified, decision: {
            "ok": True,
            "dispatch": "system-runtime",
            "errors": [],
            "workflow_status": "completed",
        },
    )
    classified = {**_crash_classified(), "request_id": "req-1"}
    result = chain.decide_and_route(classified)
    assert result["ok"] is True
    assert result["audit"]["intake"] == "skipped-unavailable"
    assert result["audit"]["accepted"] is False
    assert acceptance.submitted == []
    assert acceptance.accepted == []


def test_audit_conflict_denies_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GPTBRIDGE_RENDERER_DEV_URL", raising=False)
    acceptance = _AcceptanceStub(submit_ok=False)
    app = SimpleNamespace(
        project_root=tmp_path,
        permission_sovereign=_PermStub(),
        _sub_sovereigns={"change-acceptance-sub-sovereign": acceptance},
    )
    chain = RepairDecisionChain(app)
    dispatched: list = []
    monkeypatch.setattr(
        chain,
        "_dispatch_to_runtime",
        lambda classified, decision: dispatched.append(classified)
        or {"ok": True},
    )
    classified = {**_crash_classified(), "request_id": "req-2"}
    result = chain.decide_and_route(classified)
    # A604: the retired intake is never reached — the chain proceeds
    # without consulting the materialized retired identity.
    assert result["audit"]["intake"] == "skipped-unavailable"
    assert acceptance.submitted == []


def test_unrepairable_action_still_denied(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GPTBRIDGE_RENDERER_DEV_URL", raising=False)
    monkeypatch.setattr(
        auto_action_policy, "automatic_repair_execution_allowed", lambda: True
    )
    chain = _chain(tmp_path)
    classified = {
        "error_type": "Unknown",
        "target_file": "",
        "action": "skip",
        "diagnosis": {"action": "skip"},
    }
    result = chain.decide_and_route(classified)
    assert result["decision"] == "denied-not-repairable"


# ---------------------------------------------------------------------------
# Runtime-recovery dispatch through CentralRepairService
# ---------------------------------------------------------------------------


def _tool_root(tmp_path: Path, tool_id: str) -> Path:
    tool_dir = tmp_path / "Standalone tools" / tool_id
    tool_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").write_text(
        json.dumps({"id": tool_id, "name": tool_id}), encoding="utf-8"
    )
    return tool_dir


def test_repair_tool_resolves_standalone_tool_root(
    tmp_path: Path, repair_schema: str
) -> None:
    _tool_root(tmp_path, "demo-tool")
    service = CentralRepairService(
        tmp_path, tmp_path / "main-system" / "data" / "automatic-repair",
        repair_schema=repair_schema,
    )
    result = service.repair_tool(
        "demo-tool",
        "TOOL_RUNTIME_CRASH",
        package_rebuilder=lambda tid: {"ok": True, "tool_id": tid},
    )
    assert result["ok"] is True
    assert result["target_tool_id"] == "demo-tool"
    assert result["repair_plan"]["rebuild_executable"] is True
    assert "inspect-owned-databases" in result["executed_actions"]
    assert "rebuild-tool-executable" in result["executed_actions"]


def test_repair_tool_denies_unknown_tool(
    tmp_path: Path, repair_schema: str
) -> None:
    service = CentralRepairService(
        tmp_path, tmp_path / "main-system" / "data" / "automatic-repair",
        repair_schema=repair_schema,
    )
    with pytest.raises(PermissionError):
        service.repair_tool("no-such-tool", "TOOL_RUNTIME_CRASH")


# ---------------------------------------------------------------------------
# Learned-recipe loop: actions derivation, verification, suppression
# ---------------------------------------------------------------------------


def _learned_recipe(sig_hash: str, *, rate: float = 1.0, count: int = 3) -> LearnedRecipe:
    return LearnedRecipe(
        recipe_id=f"learned-{sig_hash}",
        name="learned rebuild",
        failure_signatures=("TOOL_RUNTIME_CRASH",),
        remedy="inspect-owned-databases,rebuild-tool-executable",
        owner="星澄",
        automatic=True,
        runtime_only=True,
        learned_at="2026-01-01T00:00:00+00:00",
        occurrence_count=count,
        success_rate=rate,
    )


def test_known_recipes_derives_actions_for_learned(
    tmp_path: Path, repair_schema: str
) -> None:
    service = CentralRepairService(
        tmp_path, learning_database_root(tmp_path), repair_schema=repair_schema
    )
    sig_hash = _normalize_error_signature(
        "TOOL_RUNTIME_CRASH", "", file_path="demo-tool"
    )
    service.learning_store.save_learned_recipe(_learned_recipe(sig_hash))
    recipes = {r["recipe_id"]: r for r in service.known_recipes()}
    learned = recipes[f"learned-{sig_hash}"]
    assert learned["actions"] == [
        "inspect-owned-databases",
        "rebuild-tool-executable",
    ]
    assert learned["verified_applicability"] is True
    assert learned["automatic"] is True


def test_learned_recipe_never_promotes_source_mutation(
    tmp_path: Path, repair_schema: str
) -> None:
    service = CentralRepairService(
        tmp_path, learning_database_root(tmp_path), repair_schema=repair_schema
    )
    sig_hash = _normalize_error_signature("FAULT_X", "", file_path="demo-tool")
    recipe = LearnedRecipe(
        recipe_id=f"learned-{sig_hash}",
        name="bad learned recipe",
        failure_signatures=("FAULT_X",),
        remedy="inspect-owned-databases,repair-main-system-source",
        owner="星澄",
        occurrence_count=5,
        success_rate=1.0,
    )
    service.learning_store.save_learned_recipe(recipe)
    learned = {
        r["recipe_id"]: r for r in service.known_recipes()
    }[f"learned-{sig_hash}"]
    assert "repair-main-system-source" not in learned["actions"]


def test_learned_recipe_suppressed_after_recent_failures(
    tmp_path: Path, repair_schema: str
) -> None:
    service = CentralRepairService(
        tmp_path, learning_database_root(tmp_path), repair_schema=repair_schema
    )
    sig_hash = _normalize_error_signature(
        "TOOL_RUNTIME_CRASH", "", file_path="demo-tool"
    )
    service.learning_store.save_learned_recipe(_learned_recipe(sig_hash))
    store = service.learning_store
    for _ in range(3):
        store.record_outcome(
            RepairOutcome(
                run_id="run",
                signature_hash=sig_hash,
                remedy="inspect-owned-databases,rebuild-tool-executable",
                ok=False,
            )
        )
    learned = {
        r["recipe_id"]: r for r in service.known_recipes()
    }[f"learned-{sig_hash}"]
    assert learned["verified_applicability"] is False
    assert learned["automatic"] is False
    assert learned["suppressed"] is True


def test_plan_repair_uses_learned_recipe_actions() -> None:
    learned_entry = {
        "recipe_id": "learned-abc",
        "failure_signatures": ["CUSTOM_FAULT"],
        "actions": ["inspect-owned-databases", "rebuild-tool-executable"],
        "automatic": True,
        "verified_applicability": True,
        "verification": "promoted-by-verified-repair-outcomes",
        "source": "learned",
    }
    plan = plan_repair("CUSTOM_FAULT", recipes=[learned_entry])
    assert plan.rebuild_executable is True
    assert plan.mutation_allowed is True
    assert plan.recipe_id == "learned-abc"
    assert "repair-main-system-source" not in plan.actions


def test_decision_suppresses_learned_ineffective_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repair_schema: str
) -> None:
    monkeypatch.delenv("GPTBRIDGE_RENDERER_DEV_URL", raising=False)
    monkeypatch.setattr(
        auto_action_policy, "automatic_repair_execution_allowed", lambda: True
    )
    store = RepairLearningStore(
        learning_database_root(tmp_path), schema=repair_schema
    )
    learner = RepairLearner(store)
    sig_hash = _normalize_error_signature("RuntimeError", "", file_path="")
    for _ in range(3):
        store.record_outcome(
            RepairOutcome(
                run_id="run",
                signature_hash=sig_hash,
                remedy="runtime-recovery",
                ok=False,
            )
        )
    maintenance = SimpleNamespace(
        _learning_store=store,
        _learner=learner,
        suggest_remedy=lambda **kw: {"suggested": False, "reason": "no successes"},
    )
    chain = _chain(tmp_path, maintenance=maintenance)
    result = chain.decide_and_route(_crash_classified())
    assert result["decision"] == "denied-not-repairable"
    assert "learned-ineffective-remedy" in result["reason"]


class _FakeCore:
    def __init__(self, allow=True):
        self.allow = allow
        self.registered = {}
        self.unregistered = []

    def register_flow(self, flow_id, tick, **kwargs):
        if not self.allow:
            return False
        self.registered[flow_id] = (tick, kwargs)
        return True

    def unregister(self, name):
        self.unregistered.append(name)


def test_repair_chain_core_driven_no_private_task():
    stub = _maintenance_stub()
    stub.app = SimpleNamespace(automation_core=_FakeCore())
    stub.ROLE = "maintenance-sovereign"
    stub._repair_decision_task = None
    stub._start_repair_decision_loop()
    assert stub._repair_decision_core is True
    assert stub._repair_decision_task is None
    assert "maintenance-repair-chain" in stub.app.automation_core.registered
    status = stub._repair_decision_chain_status()
    assert status["enabled"] is True
    assert status["loop"] == "automation-core"


def test_repair_chain_denied_no_fallback():
    stub = _maintenance_stub()
    stub.app = SimpleNamespace(automation_core=_FakeCore(allow=False))
    stub._repair_decision_task = None
    stub._start_repair_decision_loop()
    assert stub._repair_decision_core is False
    assert stub._repair_decision_task is None  # kill switch: no private loop


def test_isolation_monitor_core_driven(tmp_path):
    import asyncio
    from core_system.tool_isolation import ToolIsolationManager

    mgr = ToolIsolationManager(tmp_path)
    core = _FakeCore()
    mgr.start_monitor(interval=30.0, light=True, automation_core=core)
    assert mgr._core_driven is True
    assert mgr._monitor_thread is None
    assert "tool-isolation-health" in core.registered
    # the flow tick runs a single monitor sweep off-loop
    tick = core.registered["tool-isolation-health"][0]
    asyncio.run(tick())


def test_isolation_monitor_denied_no_thread(tmp_path):
    from core_system.tool_isolation import ToolIsolationManager

    mgr = ToolIsolationManager(tmp_path)
    mgr.start_monitor(automation_core=_FakeCore(allow=False))
    assert mgr._core_driven is False
    assert mgr._monitor_thread is None
