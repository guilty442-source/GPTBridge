from pathlib import Path

# 1) config.py gains the torch-free path helper
p = Path(r"Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/config.py")
d = p.read_text(encoding="utf-8")
assert "from pathlib import Path" not in d
d = d.replace(
    "from dataclasses import dataclass, field, asdict\nfrom typing import Any, Mapping",
    "from dataclasses import dataclass, field, asdict\nfrom pathlib import Path\nfrom typing import Any, Mapping",
    1,
)
d += (
    "\n\ndef default_checkpoint_dir() -> Path:\n"
    '    """Torch-free path helper (A612): the checkpoint root must be\n'
    "    resolvable without importing the retired torch lineage.\n"
    "    ``Standalone tools/local-model/xingcheng/runtime/models/``.\n"
    '    """\n'
    "    # .../services/xingcheng/infrastructure/native_transformer/config.py\n"
    "    local_model_root = Path(__file__).resolve().parents[6]\n"
    '    return local_model_root / "xingcheng" / "runtime" / "models"\n'
)
p.write_text(d, encoding="utf-8")
print("config.py")

# 2) checkpoint.py: keep the public name as a re-export
p = Path(r"Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/checkpoint.py")
d = p.read_text(encoding="utf-8")
old = '''def default_checkpoint_dir() -> Path:
    """'''
idx = d.find(old)
assert idx != -1
# find end of function (next top-level def)
end = d.find("\ndef _iso_now", idx)
assert end != -1
d = d[:idx] + d[end+1:]
d = d.replace(
    "from .config import XingChengConfig",
    "from .config import XingChengConfig, default_checkpoint_dir",
    1,
)
p.write_text(d, encoding="utf-8")
print("checkpoint.py")

# 3) native_engine.py: resolve the default dir without pulling torch
p = Path(r"Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_engine.py")
d = p.read_text(encoding="utf-8")
old = "    directory = _native().default_checkpoint_dir()\n"
new = (
    "    # A612: torch-free path resolution; _native() would pull the retired\n"
    "    # torch lineage just to compute a directory.\n"
    "    from .native_transformer.config import default_checkpoint_dir\n\n"
    "    directory = default_checkpoint_dir()\n"
)
assert old in d
d = d.replace(old, new, 1)
p.write_text(d, encoding="utf-8")
print("native_engine.py")
