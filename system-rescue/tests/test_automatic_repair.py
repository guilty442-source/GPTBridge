from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "system-rescue" / "src" / "backend" / "services"
if str(SERVICES) not in sys.path:
    sys.path.insert(0, str(SERVICES))

from system_rescue.application.automatic_repair import CentralAutomaticRepairService  # noqa: E402
from system_rescue.domain.repair_policy import plan_repair  # noqa: E402
from system_rescue.integration.package_rebuilder import ToolPackageRebuilder  # noqa: E402


def _fake_project(root: Path) -> Path:
    main = root / "main-system"
    (main / "scripts").mkdir(parents=True)
    (main / "package.json").write_text(
        json.dumps({"version": "1.0.0"}), encoding="utf-8"
    )
    target = root / "alpha"
    (target / "runtime" / "state").mkdir(parents=True)
    (target / "manifest.json").write_text(
        json.dumps({"id": "alpha", "version": "1.0.0"}), encoding="utf-8"
    )
    rescue = root / "system-rescue"
    packager = (
        rescue
        / "src"
        / "backend"
        / "services"
        / "system_rescue"
        / "integration"
        / "platform_packager.py"
    )
    packager.parent.mkdir(parents=True)
    packager.write_text("print('{}')\n", encoding="utf-8")
    return rescue


def test_repair_policy_assigns_package_rebuild_to_system_rescue() -> None:
    package_plan = plan_repair("PROCESS_START_FAILED")
    database_plan = plan_repair("DATABASE_UNAVAILABLE")

    assert package_plan.rebuild_executable is True
    assert package_plan.actions == (
        "inspect-owned-databases",
        "rebuild-tool-executable",
    )
    assert database_plan.actions == ("inspect-owned-databases",)


@pytest.mark.parametrize(
    "failure_code",
    [
        "FRONTEND_BACKEND_DISCONNECTED",
        "MODEL_RUNTIME_NOT_READY",
        "COMMAND_EXECUTION_FAILED",
        "STREAM_CHANNEL_FAILED",
    ],
)
def test_star_chat_runtime_failures_are_central_repair_conditions(
    failure_code: str,
) -> None:
    plan = plan_repair(failure_code)

    assert plan.rebuild_executable is True
    assert plan.actions == (
        "inspect-owned-databases",
        "rebuild-tool-executable",
    )


def test_central_repair_executes_rebuild_and_keeps_isolated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rescue_root = _fake_project(tmp_path)
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/main-system",
    )
    rebuilds: list[str] = []

    def rebuild(tool_id: str) -> dict[str, object]:
        rebuilds.append(tool_id)
        return {"ok": True, "owner": "system-rescue", "target_tool_id": tool_id}

    service = CentralAutomaticRepairService(
        tmp_path,
        rescue_root,
        package_rebuilder=rebuild,
    )
    result = service.repair_tool("alpha", "PROCESS_START_FAILED")

    assert result["ok"] is True
    assert result["authority"] == "system-rescue"
    assert result["executed_actions"] == [
        "inspect-owned-databases",
        "rebuild-tool-executable",
    ]
    assert result["package_repair"]["owner"] == "system-rescue"
    assert rebuilds == ["alpha"]
    assert Path(result["database"]) == (
        rescue_root
        / "data"
        / "automatic-repair"
        / "alpha"
        / "automatic-repair.sqlite3"
    )


def test_package_rebuilder_is_owned_by_system_rescue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rescue_root = _fake_project(tmp_path)
    captured: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps({"ok": True, "tool_id": "alpha"}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ToolPackageRebuilder(tmp_path, rescue_root).rebuild("alpha")

    assert result["ok"] is True
    assert result["owner"] == "system-rescue"
    assert captured[0][-2:] == ["alpha", "--json"]
