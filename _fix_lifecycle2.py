from pathlib import Path

# revert auto_release lazy-torch (dead code: package init needs torch anyway)
p = Path(r"Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/execution/auto_release.py")
d = p.read_text(encoding="utf-8")
d = d.replace(
    "try:\n    import torch\nexcept ModuleNotFoundError:\n    # A612: torch is retired lineage; CUDA cache purge becomes a no-op.\n    torch = None  # type: ignore[assignment]\n\nfrom .memory import",
    "import torch\n\nfrom .memory import",
)
d = d.replace(
    "        if torch is not None and torch.cuda.is_available():\n            torch.cuda.empty_cache()\n    except Exception:\n        pass\n\ndef release_kv_cache",
    "        if torch.cuda.is_available():\n            torch.cuda.empty_cache()\n    except Exception:\n        pass\n\ndef release_kv_cache",
)
d = d.replace(
    "        cache.reset()\n        if torch is not None and torch.cuda.is_available():\n            torch.cuda.empty_cache()",
    "        cache.reset()\n        if torch.cuda.is_available():\n            torch.cuda.empty_cache()",
)
p.write_text(d, encoding="utf-8")
print("reverted auto_release")

# lifecycle test: guard auto_release import too (execution package needs torch)
p = Path(r"Standalone tools/local-model/tests/test_lifecycle_resource_actions.py")
d = p.read_text(encoding="utf-8")
old = """try:
    from test_training_job_executor import _fake_train_fn, _queued_job  # noqa: E402
except ModuleNotFoundError as _exc:
    if _exc.name != "torch":
        raise
    _fake_train_fn = _queued_job = None  # type: ignore[assignment]
"""
new = """try:
    from test_training_job_executor import _fake_train_fn, _queued_job  # noqa: E402
    from xingcheng.infrastructure.native_transformer.execution.auto_release import (  # noqa: E402
        get_manager,
    )
except ModuleNotFoundError as _exc:
    if _exc.name != "torch":
        raise
    _fake_train_fn = _queued_job = get_manager = None  # type: ignore[assignment]
"""
assert old in d, "guarded import block not found"
d = d.replace(old, new, 1)
# remove the now-duplicate unconditional auto_release import
old2 = "from xingcheng.infrastructure.native_transformer.execution.auto_release import (  # noqa: E402\n    get_manager,\n)\n"
assert old2 in d, "unconditional auto_release import not found"
d = d.replace(old2, "", 1)
p.write_text(d, encoding="utf-8")
print("patched lifecycle test")
