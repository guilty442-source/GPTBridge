from pathlib import Path

def patch(path, old, new):
    p = Path(path)
    data = p.read_text(encoding="utf-8")
    assert old in data, f"pattern not found in {path}"
    p.write_text(data.replace(old, new, 1), encoding="utf-8")
    print("patched", path)

# 1) auto_release.py — lazy torch (A612): cuda cache purge optional
patch(
    r"Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/execution/auto_release.py",
    "import torch\n\nfrom .memory import device_memory_info, memory_pressure",
    "try:\n"
    "    import torch\n"
    "except ModuleNotFoundError:\n"
    "    # A612: torch is retired lineage; CUDA cache purge becomes a no-op.\n"
    "    torch = None  # type: ignore[assignment]\n"
    "\n"
    "from .memory import device_memory_info, memory_pressure",
)
patch(
    r"Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/execution/auto_release.py",
    "        if torch.cuda.is_available():\n            torch.cuda.empty_cache()\n    except Exception:\n        pass\n\ndef release_kv_cache",
    "        if torch is not None and torch.cuda.is_available():\n            torch.cuda.empty_cache()\n    except Exception:\n        pass\n\ndef release_kv_cache",
)
patch(
    r"Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/execution/auto_release.py",
    "        cache.reset()\n        if torch.cuda.is_available():\n            torch.cuda.empty_cache()",
    "        cache.reset()\n        if torch is not None and torch.cuda.is_available():\n            torch.cuda.empty_cache()",
)
