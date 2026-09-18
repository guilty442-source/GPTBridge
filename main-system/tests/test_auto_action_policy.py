import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from core_system import auto_action_policy, confirmation_service


class _App:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root


def test_switches_default_state(tmp_path: Path) -> None:
    switches = auto_action_policy.read_automation_switches(tmp_path)
    # Repair switch retired: reads as managed-by-system audit flow.
    assert switches["automatic_repair_enabled"] is True
    assert switches["automatic_repair_managed_by"] == "system-audit-flow"
    assert switches["automatic_update_enabled"] is False
    assert auto_action_policy.switch_enabled_for_kind("repair") is True
    # The update switch is a governed user choice; the module-level helper
    # reads the persisted value, so assert consistency with that state.
    assert auto_action_policy.switch_enabled_for_kind("update") is bool(
        auto_action_policy.read_automation_switches().get(
            "automatic_update_enabled"
        )
    )


def test_repair_switch_write_rejected_update_switch_persists(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        auto_action_policy.set_automation_switch(
            tmp_path, "automatic_repair_enabled", False, actor="test-user"
        )

    record = auto_action_policy.set_automation_switch(
        tmp_path, "automatic_update_enabled", True, actor="test-user"
    )
    assert record["automatic_update_enabled"] is True
    switches = auto_action_policy.read_automation_switches(tmp_path)
    assert switches["automatic_update_enabled"] is True
    assert switches["updated_by"] == "test-user"

    audit_path = tmp_path.joinpath(*auto_action_policy.SWITCH_AUDIT_RELATIVE)
    entries = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[-1]["switch"] == "automatic_update_enabled"
    assert entries[-1]["previous"] is False
    assert entries[-1]["enabled"] is True


def test_unknown_switch_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        auto_action_policy.set_automation_switch(tmp_path, "not_a_switch", True)


def test_record_pending_action_round_trip_and_dedupe(tmp_path: Path) -> None:
    first = auto_action_policy.record_pending_action(
        tmp_path,
        kind="repair",
        summary="fault one",
        detail={"request_id": "r1"},
        action_id="repair-r1",
        binding={"fault_id": "r1", "scope": "src"},
    )
    assert first["status"] == "awaiting-confirmation"
    assert first["fault_id"] == "r1"
    assert first["evidence_digest"]

    second = auto_action_policy.record_pending_action(
        tmp_path,
        kind="repair",
        summary="fault one (updated)",
        detail={"request_id": "r1", "classified": {"error_type": "SyntaxError"}},
        action_id="repair-r1",
        binding={"fault_id": "r1", "scope": "src"},
    )
    assert second["summary"] == "fault one (updated)"

    actions = auto_action_policy.read_pending_actions(tmp_path)
    assert len(actions) == 1
    assert actions[0]["detail"]["classified"]["error_type"] == "SyntaxError"


def test_recording_refused_while_switch_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    auto_action_policy.record_pending_action(
        tmp_path,
        kind="repair",
        summary="fault",
        detail={"request_id": "r1"},
        action_id="repair-r1",
    )
    monkeypatch.setattr(
        confirmation_service, "switch_enabled_for_kind", lambda _kind: False
    )

    result = asyncio.run(
        confirmation_service.record_confirmation(_App(tmp_path), "repair-r1")
    )

    assert result["ok"] is False
    assert result["error_code"] == "SWITCH_DISABLED"
    actions = auto_action_policy.read_pending_actions(tmp_path)
    assert actions[0]["status"] == "awaiting-confirmation"


def test_single_item_permission_does_not_require_or_change_switch(
    monkeypatch, tmp_path: Path
) -> None:
    auto_action_policy.record_pending_action(
        tmp_path,
        kind="update",
        summary="one update",
        action_id="update-one",
    )
    monkeypatch.setattr(
        confirmation_service, "switch_enabled_for_kind", lambda _kind: False
    )

    result = asyncio.run(
        confirmation_service.record_confirmation(
            _App(tmp_path), "update-one", permission_mode="single-item"
        )
    )

    assert result["ok"] is True
    assert result["permission_mode"] == "single-item"
    action = auto_action_policy.read_pending_actions(tmp_path)[0]
    assert action["confirmation"]["single_use"] is True
    assert action["confirmation"]["permission_mode"] == "single-item"
    assert auto_action_policy.read_automation_switches(tmp_path)[
        "automatic_update_enabled"
    ] is False


def test_user_can_deny_one_repair_without_affecting_other_items(tmp_path: Path) -> None:
    for action_id in ("repair-one", "repair-two"):
        auto_action_policy.record_pending_action(
            tmp_path,
            kind="repair",
            summary=action_id,
            action_id=action_id,
        )

    result = asyncio.run(
        confirmation_service.deny_pending_action(_App(tmp_path), "repair-one")
    )

    assert result["ok"] is True
    actions = {
        item["action_id"]: item for item in auto_action_policy.read_pending_actions(tmp_path)
    }
    assert actions["repair-one"]["status"] == "denied"
    assert actions["repair-two"]["status"] == "awaiting-confirmation"


def test_record_confirm_revoke_flow(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        confirmation_service, "switch_enabled_for_kind", lambda _kind: True
    )
    auto_action_policy.record_pending_action(
        tmp_path,
        kind="repair",
        summary="fault",
        detail={"request_id": "r2"},
        action_id="repair-r2",
    )
    app = _App(tmp_path)

    recorded = asyncio.run(confirmation_service.record_confirmation(app, "repair-r2"))
    assert recorded["ok"] is True
    assert recorded["status"] == "confirmed"
    confirmation_id = recorded["confirmation_id"]
    assert auto_action_policy.read_pending_actions(tmp_path)[0]["status"] == "confirmed"

    revoked = asyncio.run(
        confirmation_service.revoke_confirmation(app, "repair-r2", confirmation_id)
    )
    assert revoked["ok"] is True
    actions = auto_action_policy.read_pending_actions(tmp_path)
    assert actions[0]["status"] == "awaiting-confirmation"
    assert not actions[0].get("confirmation")


def test_execute_requires_recorded_confirmation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        confirmation_service, "switch_enabled_for_kind", lambda _kind: True
    )
    auto_action_policy.record_pending_action(
        tmp_path,
        kind="update",
        summary="update intent",
        detail={"modules": ["tasks.example"]},
        action_id="update-example",
    )
    result = asyncio.run(
        confirmation_service.execute_approved(
            _App(tmp_path), "update-example", "no-such-confirmation"
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "NOT_CONFIRMED"


def test_expired_confirmation_is_refused(monkeypatch, tmp_path: Path) -> None:
    auto_action_policy.record_pending_action(
        tmp_path,
        kind="repair",
        summary="expired fault",
        detail={"request_id": "r3"},
        action_id="repair-r3",
        binding={"expires_at": "2000-01-01T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        confirmation_service, "switch_enabled_for_kind", lambda _kind: True
    )

    result = asyncio.run(
        confirmation_service.record_confirmation(_App(tmp_path), "repair-r3")
    )

    assert result["ok"] is False
    assert result["error_code"] == "CONFIRMATION_EXPIRED"
    actions = auto_action_policy.read_pending_actions(tmp_path)
    assert actions[0]["status"] == "expired"


def test_evidence_change_invalidates_confirmation(
    monkeypatch, tmp_path: Path
) -> None:
    auto_action_policy.record_pending_action(
        tmp_path,
        kind="update",
        summary="update intent",
        detail={"modules": ["tasks.example"]},
        action_id="update-example",
    )
    monkeypatch.setattr(
        confirmation_service, "switch_enabled_for_kind", lambda _kind: True
    )

    path = auto_action_policy.pending_actions_path(tmp_path)
    actions = json.loads(path.read_text(encoding="utf-8"))
    actions[0]["detail"]["modules"] = ["tasks.changed-after-review"]
    path.write_text(
        json.dumps(actions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    result = asyncio.run(
        confirmation_service.record_confirmation(_App(tmp_path), "update-example")
    )

    assert result["ok"] is False
    assert result["error_code"] == "EVIDENCE_CHANGED"
    actions = auto_action_policy.read_pending_actions(tmp_path)
    assert actions[0]["status"] == "invalidated"


def test_command_resolver_refreshes_on_codex_change(tmp_path: Path) -> None:
    from shared_layer.runtime_gateway import CommandContractResolver

    db_dir = tmp_path / "governance_rule" / "codex" / "data"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "governance_codex.sqlite3"
    connection = sqlite3.connect(str(db_path))
    connection.execute(
        "CREATE TABLE command_code_directory (command_code TEXT PRIMARY KEY)"
    )
    connection.execute(
        "INSERT INTO command_code_directory VALUES ('ALPHA_ONE')"
    )
    connection.commit()
    connection.close()

    resolver = CommandContractResolver(tmp_path)
    assert resolver.is_registered("alpha-one") is True
    assert resolver.is_registered("beta-two") is False

    connection = sqlite3.connect(str(db_path))
    connection.execute("INSERT INTO command_code_directory VALUES ('BETA_TWO')")
    connection.commit()
    connection.close()

    assert resolver.is_registered("beta-two") is True
