from pathlib import Path

p = Path(r"E:\GPTBridge\Standalone tools\local-model\tests\test_lifecycle_resource_actions.py")
s = p.read_text(encoding="utf-8")
old = '''try:
    from test_training_job_executor import _fake_train_fn, _queued_job  # noqa: E402
    from xingcheng.infrastructure.native_transformer.execution.auto_release import (  # noqa: E402
        get_manager,
    )
except ModuleNotFoundError as _exc:
    if _exc.name != "torch":
        raise
    _fake_train_fn = _queued_job = get_manager = None  # type: ignore[assignment]

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(
    _queued_job is None,
    reason="A612: torch lineage retired; test_training_job_executor helpers unavailable",
)
'''
new = '''import pytest  # noqa: E402

try:
    from test_training_job_executor import _fake_train_fn, _queued_job  # noqa: E402
    from xingcheng.infrastructure.native_transformer.execution.auto_release import (  # noqa: E402
        get_manager,
    )
except ModuleNotFoundError as _exc:
    if _exc.name != "torch":
        raise
    _fake_train_fn = _queued_job = get_manager = None  # type: ignore[assignment]
except pytest.skip.Exception:
    # A612: test_training_job_executor guards torch with importorskip, which
    # raises Skipped during import; absent helpers must not abort collection.
    _fake_train_fn = _queued_job = get_manager = None  # type: ignore[assignment]

_needs_torch_helpers = pytest.mark.skipif(
    _queued_job is None,
    reason="A612: torch lineage retired; test_training_job_executor helpers unavailable",
)
'''
assert old in s, "top block not found"
s = s.replace(old, new)
s = s.replace(
    "def test_training_state_releases_inference_resources(tmp_path: Path):",
    "@_needs_torch_helpers\ndef test_training_state_releases_inference_resources(tmp_path: Path):",
)
s = s.replace(
    "def test_non_training_state_writes_no_ledger(tmp_path: Path):",
    "@_needs_torch_helpers\ndef test_non_training_state_writes_no_ledger(tmp_path: Path):",
)
p.write_text(s, encoding="utf-8")
print("patched")
