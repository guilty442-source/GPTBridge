from pathlib import Path
p = Path(r"Standalone tools/local-model/tests/test_lifecycle_resource_actions.py")
d = p.read_text(encoding="utf-8")
old = "    _fake_train_fn = _queued_job = None  # type: ignore[assignment]\n"
new = (
    old
    + "\nimport pytest  # noqa: E402\n\npytestmark = pytest.mark.skipif(\n"
    + "    _queued_job is None,\n"
    + '    reason="A612: torch lineage retired; test_training_job_executor helpers unavailable",\n'
    + ")\n"
)
assert old in d
p.write_text(d.replace(old, new, 1), encoding="utf-8")
print("ok")
