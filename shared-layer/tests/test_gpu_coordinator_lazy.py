"""Lazy-import and query-order invariants for ``gpu_coordinator``.

Pins the 2026-09-27 optimisation contract:

- importing the module must NOT import ``torch`` (every consumer process
  would otherwise pay ~1-2s import + CUDA-context risk),
- ``query_gpu`` must prefer ``nvidia-smi`` and consult torch ONLY as a
  fallback (smi reports physical memory under WDDM; torch's
  ``mem_get_info`` overestimates free VRAM),
- the torch probe is memoised so a torch-less box never re-pays import.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from shared_layer.adaptive import gpu_coordinator as gc  # noqa: E402


def test_module_import_does_not_load_torch() -> None:
    """Import in a clean subprocess: 'torch' must stay out of sys.modules."""
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(ROOT / 'shared-layer' / 'src')!r});"
        "import shared_layer.adaptive.gpu_coordinator;"
        "print('torch' in sys.modules)"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", code], text=True, timeout=60
    ).strip()
    assert out == "False", "gpu_coordinator import must not drag in torch"


def test_query_gpu_prefers_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    smi_status = gc.GpuStatus(total_mb=6000, used_mb=1000, free_mb=5000, util_pct=10.0)
    monkeypatch.setattr(gc, "_query_via_nvidia_smi", lambda: smi_status)

    def _boom() -> None:
        raise AssertionError("torch fallback must not run when nvidia-smi answers")

    monkeypatch.setattr(gc, "_query_via_torch", _boom)
    assert gc.query_gpu() is smi_status


def test_query_gpu_falls_back_to_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    torch_status = gc.GpuStatus(total_mb=6000, used_mb=500, free_mb=5500, util_pct=0.0)
    monkeypatch.setattr(gc, "_query_via_nvidia_smi", lambda: None)
    monkeypatch.setattr(gc, "_query_via_torch", lambda: torch_status)
    assert gc.query_gpu() is torch_status


def test_query_gpu_no_sources_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc, "_query_via_nvidia_smi", lambda: None)
    monkeypatch.setattr(gc, "_query_via_torch", lambda: None)
    assert gc.query_gpu() is None


def test_torch_probe_is_memoised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A torch-less environment must not re-pay the import attempt."""
    monkeypatch.setattr(gc, "_TORCH", None)
    monkeypatch.setattr(gc, "_TORCH_PROBED", False)
    sentinel = object()
    monkeypatch.setitem(sys.modules, "torch", sentinel)
    try:
        assert gc._torch() is sentinel
        assert gc._torch() is sentinel
    finally:
        monkeypatch.delitem(sys.modules, "torch", raising=False)
        monkeypatch.setattr(gc, "_TORCH", None)
        monkeypatch.setattr(gc, "_TORCH_PROBED", False)


def test_can_acquire_respects_95pct_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    coord = gc.GpuCoordinator(poll_interval=0.01)
    # 6GB card: cap = 5700MB.  With used=5400, request=400 pushes
    # used+request (5800) over the cap -> denied even though free=600.
    status = gc.GpuStatus(total_mb=6000, used_mb=5400, free_mb=600, util_pct=50.0)
    monkeypatch.setattr(gc, "query_gpu", lambda: status)
    assert coord.can_acquire(400) is False
    # Same free headroom but under the cap: 600 >= 100+500 and
    # 5400+100 <= 5700 -> allowed.
    assert coord.can_acquire(100) is True
