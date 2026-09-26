from pathlib import Path

def patch(path, old, new, count=1):
    p = Path(path)
    data = p.read_text(encoding="utf-8")
    assert old in data, f"pattern not found in {path}"
    p.write_text(data.replace(old, new, count), encoding="utf-8")
    print("patched", path)

REASON = "A612: torch lineage retired; torch-dependent surface skips without torch"

# --- test_native_transformer.py: whole suite is torch lineage ---
patch(
    r"Standalone tools/local-model/tests/test_native_transformer.py",
    "import pytest\nimport torch\n\nfrom native_transformer import (",
    "import pytest\n\ntry:\n    import torch\n\n    from native_transformer import (",
)
patch(
    r"Standalone tools/local-model/tests/test_native_transformer.py",
    "from native_transformer.quantization import quantize_model, dequantize_model\n",
    "    from native_transformer.quantization import quantize_model, dequantize_model\n"
    "except ModuleNotFoundError as _exc:\n"
    "    if _exc.name != \"torch\":\n"
    "        raise\n"
    "    torch = None  # type: ignore[assignment]\n"
    "\n"
    "pytestmark = pytest.mark.skipif(\n"
    "    torch is None,\n"
    f"    reason=\"{REASON}\",\n"
    ")\n",
)
# indent the remaining import blocks inside try
p = Path(r"Standalone tools/local-model/tests/test_native_transformer.py")
d = p.read_text(encoding="utf-8")
d = d.replace(
    ")\nfrom native_transformer.inference import Generator, KVCache, Sampler, SamplingConfig\nfrom native_transformer.training import (",
    ")\n    from native_transformer.inference import Generator, KVCache, Sampler, SamplingConfig\n    from native_transformer.training import (",
)
p.write_text(d, encoding="utf-8")
print("reindented test_native_transformer imports")

# --- test_self_learning_gates.py ---
patch(
    r"Standalone tools/local-model/tests/test_self_learning_gates.py",
    "import pytest\nimport torch\n\nfrom xingcheng.infrastructure.native_transformer import (\n    XingChengConfig,\n    XingChengForCausalLM,\n    save_checkpoint,\n)\n",
    "import pytest\n\ntry:\n    import torch\n\n    from xingcheng.infrastructure.native_transformer import (\n        XingChengConfig,\n        XingChengForCausalLM,\n        save_checkpoint,\n    )\nexcept ModuleNotFoundError as _exc:\n    if _exc.name != \"torch\":\n        raise\n    torch = None  # type: ignore[assignment]\n\npytestmark = pytest.mark.skipif(\n    torch is None,\n"
    f"    reason=\"{REASON}\",\n"
    ")\n",
)

# --- test_native_engine.py: only _write_checkpoint needs torch ---
patch(
    r"Standalone tools/local-model/tests/test_native_engine.py",
    "import torch\nimport pytest\n\n",
    "import pytest\n\ntry:\n    import torch\nexcept ModuleNotFoundError:\n    torch = None  # type: ignore[assignment]\n\n",
)
patch(
    r"Standalone tools/local-model/tests/test_native_engine.py",
    "from xingcheng.infrastructure.native_transformer import (\n    XingChengConfig,\n    XingChengForCausalLM,\n    XingChengTokenizer,\n    save_checkpoint,\n)\n",
    "try:\n    from xingcheng.infrastructure.native_transformer import (\n        XingChengConfig,\n        XingChengForCausalLM,\n        XingChengTokenizer,\n        save_checkpoint,\n    )\nexcept ModuleNotFoundError as _exc:\n    if _exc.name != \"torch\":\n        raise\n",
)
patch(
    r"Standalone tools/local-model/tests/test_native_engine.py",
    "def _write_checkpoint(tmp_path) -> str:\n    cfg = _small_config()\n",
    "def _write_checkpoint(tmp_path) -> str:\n    if torch is None:\n"
    f"        pytest.skip(\"{REASON}\")\n"
    "    cfg = _small_config()\n",
)

# --- test_lifecycle_resource_actions.py: helpers live in torch module ---
patch(
    r"Standalone tools/local-model/tests/test_lifecycle_resource_actions.py",
    "from test_training_job_executor import _fake_train_fn, _queued_job  # noqa: E402\n",
    "try:\n"
    "    from test_training_job_executor import _fake_train_fn, _queued_job  # noqa: E402\n"
    "except ModuleNotFoundError as _exc:\n"
    "    if _exc.name != \"torch\":\n"
    "        raise\n"
    "    _fake_train_fn = _queued_job = None  # type: ignore[assignment]\n",
)
