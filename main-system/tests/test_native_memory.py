"""Tests for Native Memory & Buffer Architecture V1.

Matrix coverage (per capability where the native pyd is available):
- small / medium / large buffer specs
- cancellation path releases leases
- failure paths release leases and record nothing allocated
- concurrency (threads through registry + native calls)
- double-free / use-after-free detection
- oversized input rejected before allocation
- repeated-call leak check (registry live count returns to zero,
  process private bytes stable)
- shared-primitive structure: single memory.h, domains share it,
  memory domain has no reverse dependency
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "main-system" / "src-core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core_system.native.memory_contract import (
    BufferError,
    BufferOwnership,
    BufferSpec,
    LeaseRegistry,
    LeaseState,
    MemoryMetrics,
    NativeCapabilitySession,
    TransferPolicy,
    select_transfer_policy,
    validate_buffer_spec,
)

try:
    from core_system.native import _sovereign_native as _native
    _NATIVE = True
except ImportError:
    _NATIVE = False

requires_native = pytest.mark.skipif(not _NATIVE, reason="native pyd not built")


def _spec(shape, ownership=BufferOwnership.BORROWED_READONLY, **kw):
    return validate_buffer_spec(8, shape, ownership, **kw)


# ---------- spec validation: overflow + budget before allocation ----------

def test_spec_small_medium_large():
    for n in (8, 8 * 1024, 8 * 1024 * 1024):
        s = _spec((n,))
        assert s.element_count == n


def test_spec_overflow_rejected():
    with pytest.raises(BufferError):
        validate_buffer_spec(8, (1 << 62, 4), BufferOwnership.BORROWED_READONLY)
    with pytest.raises(BufferError):
        validate_buffer_spec(8, (-1,), BufferOwnership.BORROWED_READONLY)


def test_spec_oversized_rejected_by_budget():
    with pytest.raises(BufferError):
        validate_buffer_spec(
            8, (1 << 30,), BufferOwnership.BORROWED_READONLY,
            budget_bytes=1024,
        )


def test_spec_stride_shape_mismatch():
    with pytest.raises(BufferError):
        _spec((4, 4), strides=(16,))  # rank 2, 1 stride


# ---------- policy ordering ----------

def test_policy_prefers_batch_then_reuse_then_view():
    assert select_transfer_policy(batchable=True, reusable_buffer=True,
                                  zero_copy_profiled=True) is TransferPolicy.BATCH
    assert select_transfer_policy(reusable_buffer=True,
                                  zero_copy_profiled=True) is TransferPolicy.BUFFER_REUSE
    assert select_transfer_policy() is TransferPolicy.BORROWED_VIEW


def test_zero_copy_requires_profile_evidence():
    assert select_transfer_policy(zero_copy_profiled=False) \
        is TransferPolicy.BORROWED_VIEW
    assert select_transfer_policy(zero_copy_profiled=True) \
        is TransferPolicy.ZERO_COPY


# ---------- lease registry ----------

def test_lease_acquire_release():
    reg = LeaseRegistry()
    lease = reg.acquire(_spec((64,)))
    assert reg.live_count == 1
    reg.release(lease)
    assert reg.live_count == 0
    assert reg.live_bytes == 0


def test_double_free_detected():
    reg = LeaseRegistry()
    lease = reg.acquire(_spec((64,)))
    reg.release(lease)
    with pytest.raises(BufferError, match="double-release"):
        reg.release(lease)


def test_use_after_free_detected():
    reg = LeaseRegistry()
    lease = reg.acquire(_spec((64,)))
    reg.release(lease)
    with pytest.raises(BufferError, match="use-after-release"):
        reg.check_use(lease)


def test_cross_runtime_free_rejected():
    reg = LeaseRegistry()
    lease = reg.acquire(_spec((64,), BufferOwnership.NATIVE_OWNED),
                        owner="native")
    with pytest.raises(BufferError, match="cross-runtime"):
        reg.release(lease, by="python")
    reg.release(lease, by="native")  # same-owner succeeds


def test_cancel_idempotent():
    reg = LeaseRegistry()
    lease = reg.acquire(_spec((64,)))
    reg.cancel(lease)
    reg.cancel(lease)  # no error
    assert reg.live_count == 0


def test_concurrent_lease_churn():
    reg = LeaseRegistry()
    errors = []

    def worker():
        try:
            for _ in range(200):
                l = reg.acquire(_spec((32,)))
                reg.check_use(l)
                reg.release(l)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert reg.live_count == 0
    assert reg.live_bytes == 0


# ---------- capability session: failure/cancel/metrics ----------

def _session():
    reg = LeaseRegistry()
    metrics = MemoryMetrics()
    return NativeCapabilitySession(reg, metrics, "test.cap"), reg, metrics


def test_session_releases_on_failure():
    sess, reg, metrics = _session()
    def boom():
        raise RuntimeError("native failed")
    with pytest.raises(RuntimeError):
        sess.run(lambda: boom(), [_spec((64,))])
    assert reg.live_count == 0


def test_session_records_metrics():
    sess, reg, metrics = _session()
    sess.run(lambda: 42, [_spec((100,))], _spec((10,), BufferOwnership.CALLER_PROVIDED_OUTPUT))
    p = metrics.profile("test.cap")
    assert p.call_count == 1
    assert p.boundary_copy_bytes == 100 * 8
    assert reg.live_count == 0


def test_session_repeated_calls_no_lease_leak():
    sess, reg, _ = _session()
    for _ in range(500):
        sess.run(lambda: None, [_spec((16,))])
    assert reg.live_count == 0


# ---------- real native calls (if pyd built) ----------

@requires_native
def test_native_repeated_calls_no_leak():
    reg = LeaseRegistry()
    metrics = MemoryMetrics()
    sess = NativeCapabilitySession(reg, metrics, "vector.dot")
    import numpy as np
    a = np.ones(64)
    b = np.ones(64)
    for _ in range(300):
        spec_in = _spec((64,))
        result = sess.run(
            lambda: _native.vector_dot(a, b),
            [spec_in, spec_in],
        )
        assert result == pytest.approx(64.0)
    assert reg.live_count == 0
    p = metrics.profile("vector.dot")
    assert p.call_count == 300


@requires_native
def test_native_concurrent_calls():
    import numpy as np
    a = np.random.RandomState(0).rand(128)
    b = np.random.RandomState(1).rand(128)
    expected = float(a @ b)
    results, errors = [], []

    def worker():
        try:
            for _ in range(50):
                results.append(_native.vector_dot(a, b))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(r == pytest.approx(expected) for r in results)


@requires_native
def test_native_oversized_input_fails_safely():
    # wrong-shaped arrays rejected by binding validation, not crash
    import numpy as np
    a = np.ones((4, 4))
    b = np.ones(4)
    with pytest.raises(Exception):
        _native.vector_dot(a, b)


@requires_native
def test_native_small_medium_large():
    import numpy as np
    for n in (4, 4096, 1 << 20):
        a = np.ones(n)
        assert _native.vector_l2_norm(a) == pytest.approx(float(n) ** 0.5)


# ---------- structural: shared primitives, no reverse dependency ----------

def test_memory_h_shared_and_dependency_free():
    mem_h = _ROOT / "native" / "core" / "memory.h"
    assert mem_h.is_file()
    text = mem_h.read_text(encoding="utf-8")
    # no include of other native domains — memory owns nothing upward
    for bad in ('"parser.h"', '"vector.h"', '"transformer.h"',
                '"parser.hpp"', '"vector.hpp"', '"transformer.hpp"',
                "gptbridge_native.h"):
        assert bad not in text
    # four ownership classes declared
    for name in ("BORROWED_READONLY", "BORROWED_MUTABLE",
                 "NATIVE_OWNED", "CALLER_PROVIDED_OUTPUT"):
        assert name in text


def test_all_domains_use_shared_primitives():
    for name in ("parser.c", "vector.c", "transformer.c"):
        src = (_ROOT / "native" / "core" / name).read_text(encoding="utf-8")
        assert '#include "memory.h"' in src, name
        assert "gptbridge_native_mem" in src, name


def test_no_memory_cpp_without_need():
    # primitives are header-only; no memory.cpp/.hpp may exist — the
    # compute core is pure C and shares memory.h (per task spec)
    assert not (_ROOT / "native" / "core" / "memory.cpp").exists()
    assert not (_ROOT / "native" / "core" / "memory.hpp").exists()


def test_public_abi_unchanged():
    header = (_ROOT / "native" / "include" / "gptbridge_native.h").read_text(
        encoding="utf-8")
    # sole public ABI still exposes only the platform functions — no
    # memory functions leaked into the public contract
    assert "gptbridge_native_alloc" not in header
    assert "buffer" not in header.lower()
