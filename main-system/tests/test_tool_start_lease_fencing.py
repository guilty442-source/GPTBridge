"""G81 runtime 強制層：跨程序／跨世代 tool start lease fencing。

驗證 ``toolbox_service._acquire_cross_process_start_lock`` 的原子目錄鎖：

- 同一 tool_id 第二個持有者（跨實例＝跨世代）必須 fail-closed；
- owner PID 死亡後 lease 可被回收；
- 非 owner PID 不得釋放他人 lease；
- tool_id 白名單防路徑注入；
- 毀損 owner.json 視為 dead owner 回收。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "main-system" / "src-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from tasks.toolbox_service import ToolboxService  # noqa: E402


def _service(project_root: Path) -> ToolboxService:
    return ToolboxService(project_root)


def _lock_dir(project_root: Path, tool_id: str) -> Path:
    return (
        project_root
        / "main-system"
        / "runtime"
        / "state"
        / "tool-start-locks"
        / tool_id
    )


def _plant_lock(project_root: Path, tool_id: str, owner_pid: int) -> Path:
    lock_path = _lock_dir(project_root, tool_id)
    lock_path.mkdir(parents=True, exist_ok=False)
    (lock_path / "owner.json").write_text(
        json.dumps({"pid": owner_pid, "tool_id": tool_id, "acquired_at": 0.0}),
        encoding="utf-8",
    )
    return lock_path


def test_lease_blocks_second_holder_across_instances(tmp_path: Path) -> None:
    """兩個 ToolboxService 實例（模擬跨世代 backend）競爭同一 tool。"""
    first = _service(tmp_path)
    second = _service(tmp_path)

    assert first._acquire_cross_process_start_lock("local-model") is True
    # owner（本 process）仍存活 → 第二實例 fail-closed
    assert second._acquire_cross_process_start_lock("local-model") is False
    # 釋放後第二實例可取得
    first._release_cross_process_start_lock("local-model")
    assert second._acquire_cross_process_start_lock("local-model") is True
    second._release_cross_process_start_lock("local-model")


def test_stale_lease_reclaimed_when_owner_dead(tmp_path: Path) -> None:
    """owner PID 不存在時，殘留 lease 必須被回收而非永久阻塞。"""
    dead_pid = 0x7FFFFF0  # 不可能存在的 PID
    _plant_lock(tmp_path, "local-model", dead_pid)

    service = _service(tmp_path)
    assert service._acquire_cross_process_start_lock("local-model") is True
    owner = json.loads(
        (_lock_dir(tmp_path, "local-model") / "owner.json").read_text(
            encoding="utf-8"
        )
    )
    assert owner["pid"] == os.getpid()
    service._release_cross_process_start_lock("local-model")


def test_corrupt_owner_file_treated_as_dead(tmp_path: Path) -> None:
    lock_path = _lock_dir(tmp_path, "local-model")
    lock_path.mkdir(parents=True)
    (lock_path / "owner.json").write_text("{not json", encoding="utf-8")

    service = _service(tmp_path)
    assert service._acquire_cross_process_start_lock("local-model") is True
    service._release_cross_process_start_lock("local-model")


def test_foreign_owner_cannot_release_lease(tmp_path: Path) -> None:
    """lease 只能由 owner PID 釋放；其他 process 的 release 不得刪除。"""
    lock_path = _plant_lock(tmp_path, "local-model", os.getpid())
    other = _service(tmp_path)
    # 另一實例（同 process 不同物件）pid 相同也須靠 owner.json 比對；
    # 直接植入「非本 process」的 owner（父 process 存活）
    (lock_path / "owner.json").write_text(
        json.dumps({"pid": os.getppid(), "tool_id": "local-model"}),
        encoding="utf-8",
    )
    other._release_cross_process_start_lock("local-model")
    assert lock_path.is_dir(), "非 owner 的 release 不得移除 lease"


@pytest.mark.parametrize("bad_id", ["../escape", "A" * 65, "UPPER", "bad id", ""])
def test_tool_id_whitelist_fail_closed(tmp_path: Path, bad_id: str) -> None:
    service = _service(tmp_path)
    assert service._acquire_cross_process_start_lock(bad_id) is False
    service._release_cross_process_start_lock(bad_id)  # 不得拋例外
