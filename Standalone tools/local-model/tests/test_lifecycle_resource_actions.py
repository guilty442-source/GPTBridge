"""R10 驗收：生命週期 `*_TRAINING` 狀態觸發推論資源卸載與帳本。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _xingcheng_test_support  # noqa: F401,E402

from test_training_job_executor import _fake_train_fn, _queued_job  # noqa: E402
from xingcheng.infrastructure.training_job_executor import (  # noqa: E402
    TrainingJobExecutor,
)
from xingcheng.infrastructure.transformer_training_repository import (  # noqa: E402
    TransformerTrainingRepository,
)
from xingcheng.infrastructure.native_transformer.execution.auto_release import (  # noqa: E402
    get_manager,
)


def test_training_state_releases_inference_resources(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path, max_steps=4)
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)

    released: list[str] = []

    class _Dummy:
        pass

    dummy = _Dummy()  # auto_release 以弱引用持有，測試需保留強引用
    manager = get_manager()
    manager.register(
        "test-engine:cpu", dummy, lambda obj: released.append("engine")
    )

    result = executor.run_job(str(job["job_id"]))
    assert result["ok"] is True
    # PRETRAINING 轉移時推論資源被卸載
    assert released == ["engine"]
    assert "test-engine:cpu" not in manager._resources

    ledger = (
        executor.tool_root
        / "xingcheng"
        / "runtime"
        / "logs"
        / "lifecycle-resource-actions.jsonl"
    )
    assert ledger.is_file()
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    action = rows[-1]
    assert action["action"] == "release-inference-resources"
    assert action["state"] == "PRETRAINING"
    assert "test-engine:cpu" in action["released"]


def test_non_training_state_writes_no_ledger(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)
    manager = get_manager()
    manager.register("test-engine:x", object(), lambda obj: None)
    # 非訓練態（LOADED）不觸發資源動作
    executor._apply_lifecycle_resource_action(
        lifecycle=type("L", (), {"model_id": "m"})(), target="LOADED"
    )
    assert "test-engine:x" in manager._resources
    ledger = (
        executor.tool_root
        / "xingcheng"
        / "runtime"
        / "logs"
        / "lifecycle-resource-actions.jsonl"
    )
    assert not ledger.exists()
