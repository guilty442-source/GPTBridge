from pathlib import Path
p = Path(r"Standalone tools/local-model/tests/test_transformer_training_repository.py")
d = p.read_text(encoding="utf-8")
old = """    assert Path(status["path"]) == (
        tmp_path / "xingcheng" / "runtime" / "state" / "transformer-training.sqlite3"
    )
"""
new = """    assert status["engine"] == "postgresql"
    assert status["path"] == "postgresql:gptbridge_xingcheng"
"""
assert old in d, "stale sqlite path assertion not found"
p.write_text(d.replace(old, new, 1), encoding="utf-8")
print("ok")
