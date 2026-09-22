"""Regression tests for the bounded worker-pool audit check (§10.30 / A590).

Every ``ThreadPoolExecutor``/``ProcessPoolExecutor`` in production code must
carry an explicit ``max_workers`` provably inside the fixed five-core budget -
routed through ``shared_layer.performance.thread_budget``, clamped via
``min()``, or a literal/constant <= 5.  Unbounded pools fail closed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from governance_rule.execution.audit.audit_artifacts import (  # noqa: E402
    check_bounded_worker_pools,
)

_BUDGET_MODULE = """CORE_BUDGET_CAP = 5
def bounded_workers(requested, *, budget=None): ...
def bounded_threads(threads_per_worker, parallel_workers, *, budget=None): ...
def allocation_within_budget(threads_per_worker, parallel_workers, budget_cores): ...
"""


def _write_budget_module(root: Path) -> None:
    module_dir = root / "shared-layer" / "src" / "shared_layer" / "performance"
    module_dir.mkdir(parents=True)
    (module_dir / "thread_budget.py").write_text(
        _BUDGET_MODULE, encoding="utf-8"
    )


def _write_pool(root: Path, source: str) -> Path:
    src_dir = root / "main-system" / "src-core" / "pools"
    src_dir.mkdir(parents=True)
    target = src_dir / "worker_pool.py"
    target.write_text(source, encoding="utf-8")
    return target


def test_production_tree_has_no_unbounded_pools() -> None:
    errors: list[str] = []
    check_bounded_worker_pools(ROOT, errors)
    assert errors == []


def test_missing_budget_module_fails_closed(tmp_path: Path) -> None:
    errors: list[str] = []
    check_bounded_worker_pools(tmp_path, errors)
    assert errors == ["Thread budget module is missing"]


def test_unbounded_len_pool_fails_closed(tmp_path: Path) -> None:
    _write_budget_module(tmp_path)
    _write_pool(
        tmp_path,
        "from concurrent.futures import ThreadPoolExecutor\n"
        "items = [1, 2, 3]\n"
        "pool = ThreadPoolExecutor(max_workers=len(items))\n",
    )
    errors: list[str] = []
    check_bounded_worker_pools(tmp_path, errors)
    assert any("five-core budget" in error for error in errors)


def test_missing_max_workers_fails_closed(tmp_path: Path) -> None:
    _write_budget_module(tmp_path)
    _write_pool(
        tmp_path,
        "from concurrent.futures import ThreadPoolExecutor\n"
        "pool = ThreadPoolExecutor()\n",
    )
    errors: list[str] = []
    check_bounded_worker_pools(tmp_path, errors)
    assert any("missing explicit max_workers" in error for error in errors)


def test_routed_pool_passes(tmp_path: Path) -> None:
    _write_budget_module(tmp_path)
    _write_pool(
        tmp_path,
        "from concurrent.futures import ThreadPoolExecutor\n"
        "from shared_layer.performance.thread_budget import bounded_workers\n"
        "pool = ThreadPoolExecutor(max_workers=bounded_workers(len(items)))\n",
    )
    errors: list[str] = []
    check_bounded_worker_pools(tmp_path, errors)
    assert errors == []


def test_bounded_identifier_and_constant_pass(tmp_path: Path) -> None:
    _write_budget_module(tmp_path)
    _write_pool(
        tmp_path,
        "from concurrent.futures import ThreadPoolExecutor\n"
        "WORKER_CAP = 3\n"
        "workers = max(1, min(4, len(items)))\n"
        "a = ThreadPoolExecutor(max_workers=workers)\n"
        "b = ThreadPoolExecutor(max_workers=WORKER_CAP + 1)\n"
        "c = ThreadPoolExecutor(max_workers=2)\n",
    )
    errors: list[str] = []
    check_bounded_worker_pools(tmp_path, errors)
    assert errors == []


def test_over_cap_literal_fails_closed(tmp_path: Path) -> None:
    _write_budget_module(tmp_path)
    _write_pool(
        tmp_path,
        "from concurrent.futures import ThreadPoolExecutor\n"
        "pool = ThreadPoolExecutor(max_workers=8)\n",
    )
    errors: list[str] = []
    check_bounded_worker_pools(tmp_path, errors)
    assert any("five-core budget" in error for error in errors)