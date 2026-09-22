"""P4 adaptive plane：observe_merge 多生產者欄位級合併。"""
from __future__ import annotations

import sys
import pytest
from queue import Empty
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[2] / "shared-layer" / "src"
_SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
for _p in (str(_SHARED), str(_SRC_CORE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared_layer.adaptive.plane import AdaptiveDataPlane  # noqa: E402
from shared_layer.adaptive.types import LoadSignals  # noqa: E402


def test_observe_merge_preserves_other_producers_fields():
    plane = AdaptiveDataPlane()
    # 生產者 A（maintenance controller）：pg／backlog 欄位
    plane.observe_merge(
        LoadSignals(pg_latency_ms=42.0, transport_backlog=7),
        fields=("pg_latency_ms", "transport_backlog"),
    )
    # 生產者 B（perf baseline）：cpu／ram 欄位——不得抹掉 A 的量測
    plane.observe_merge(
        LoadSignals(cpu_pct=55.0, ram_pct=61.0),
        fields=("cpu_pct", "ram_pct"),
    )
    signals = plane.signals
    assert signals.pg_latency_ms == 42.0
    assert signals.transport_backlog == 7
    assert signals.cpu_pct == 55.0
    assert signals.ram_pct == 61.0


def test_observe_merge_unknown_field_ignored():
    plane = AdaptiveDataPlane()
    plane.observe_merge(
        LoadSignals(cpu_pct=10.0),
        fields=("cpu_pct", "nonexistent_field"),
    )
    assert plane.signals.cpu_pct == 10.0
    assert not hasattr(plane.signals, "nonexistent_field")


def test_observe_merge_unlisted_field_not_written():
    """fields 之外的欄位即使帶值也不覆寫（欄位所有權）。"""
    plane = AdaptiveDataPlane()
    plane.observe(LoadSignals(pg_latency_ms=99.0, cpu_pct=20.0))
    plane.observe_merge(
        LoadSignals(cpu_pct=80.0, pg_latency_ms=999.0),
        fields=("cpu_pct",),
    )
    assert plane.signals.cpu_pct == 80.0
    assert plane.signals.pg_latency_ms == 99.0  # 未被覆寫


def test_perf_baseline_job_feeds_plane(monkeypatch):
    """perf-baseline tick 結尾把系統 cpu/ram 餵進 plane（merge 不覆寫他人）。"""
    import tasks.perf_baseline_job as job
    import core_system.model_resource_manager  # noqa: F401  (ensure src-core import path)

    from shared_layer.adaptive import get_plane
    import shared_layer.adaptive as adaptive_pkg

    plane = AdaptiveDataPlane()
    plane.observe_merge(
        LoadSignals(pg_latency_ms=33.0), fields=("pg_latency_ms",)
    )
    # patch the plane factory used inside _observe_adaptive_plane
    import shared_layer.adaptive
    monkeypatch.setattr(
        "shared_layer.adaptive.get_plane", lambda: plane
    )
    job._observe_adaptive_plane()
    assert plane.signals.pg_latency_ms == 33.0
    assert plane.signals.cpu_pct >= 0.0
    assert plane.signals.ram_pct >= 0.0


def _mgr(tmp_path, gpu_free=None, ram_free=None):
    from core_system.model_resource_manager import ModelResourceManager

    return ModelResourceManager(
        ledger_path=tmp_path / "ledger.jsonl",
        gpu_free_fn=(lambda: gpu_free) if gpu_free is not None else None,
        ram_free_fn=(lambda: ram_free) if ram_free is not None else None,
        interactive_headroom_mb=0,
    )


def test_resource_manager_publishes_model_load_pct(tmp_path, monkeypatch):
    """ModelResourceManager admit/release → model_load_pct merge 注入。"""
    plane = AdaptiveDataPlane()
    plane.observe_merge(
        LoadSignals(pg_latency_ms=33.0), fields=("pg_latency_ms",)
    )
    monkeypatch.setattr("shared_layer.adaptive.get_plane", lambda: plane)

    mgr = _mgr(tmp_path, gpu_free=None, ram_free=6000.0)
    mgr.request_load("embedding", "emb-1", ram_mb=3000)
    # managed 3000 / (3000 + 6000 free) = 33.33%
    assert abs(plane.signals.model_load_pct - 33.333) < 0.01
    assert plane.signals.pg_latency_ms == 33.0  # 不互踩

    mgr.release("emb-1")
    assert plane.signals.model_load_pct == 0.0


def test_resource_manager_telemetry_unavailable_no_write(tmp_path, monkeypatch):
    """遙測全缺 → 不寫 model_load_pct（fail-closed，既有值保留）。"""
    plane = AdaptiveDataPlane()
    plane.observe_merge(
        LoadSignals(model_load_pct=55.0), fields=("model_load_pct",)
    )
    monkeypatch.setattr("shared_layer.adaptive.get_plane", lambda: plane)

    mgr = _mgr(tmp_path, gpu_free=None, ram_free=None)
    # vram_mb=0/ram_mb=0 → admit 不需遙測，但 _observe_plane 兩池皆缺 → 不寫
    d = mgr.request_load("embedding", "emb-1", ram_mb=0, vram_mb=0)
    assert d.admitted
    assert plane.signals.model_load_pct == 55.0


def test_resource_manager_vram_share(tmp_path, monkeypatch):
    """VRAM 池佔比高於 RAM 時取 max（free 於 admit 後下降→share 上升）。"""
    plane = AdaptiveDataPlane()
    monkeypatch.setattr("shared_layer.adaptive.get_plane", lambda: plane)

    from core_system.model_resource_manager import ModelResourceManager

    gpu_free = [8100.0]
    mgr = ModelResourceManager(
        ledger_path=tmp_path / "ledger.jsonl",
        gpu_free_fn=lambda: gpu_free[0],
        ram_free_fn=lambda: 8000.0,
        interactive_headroom_mb=0,
    )
    d = mgr.request_load("fast_chat", "chat-8b", vram_mb=4000, ram_mb=2000)
    assert d.admitted
    # admit 當下：vram 4000/(4000+8100)=33.1%；ram 2000/(2000+8000)=20% → 33.1
    assert abs(plane.signals.model_load_pct - 33.06) < 0.1
    # GPU free 下降後再 admit 一台 → vram share 成為 max
    gpu_free[0] = 1000.0
    d2 = mgr.request_load("fast_chat", "chat-2", vram_mb=900, ram_mb=0)
    assert d2.admitted
    # vram (4000+900)/(4900+1000)=83.1%；ram 2000/10000=20% → 83.1
    assert abs(plane.signals.model_load_pct - 83.05) < 0.1


class _FakeStatus:
    name = "IDLE"


class _FakeInfo:
    transaction_status = _FakeStatus()


class _FakeConn:
    closed = False
    info = _FakeInfo()

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class _EmptyThenRaise:
    """get_nowait/get 皆 Empty → 觸發滿池逾時路徑。"""

    def get_nowait(self):
        raise Empty

    def get(self, timeout=0):
        raise Empty

    def put_nowait(self, item):
        pass

    def qsize(self):
        return 0


class _EmptyThenConn:
    def __init__(self):
        self.conn = _FakeConn()
        self.returned = []

    def get_nowait(self):
        raise Empty

    def get(self, timeout=0):
        return self.conn

    def put_nowait(self, item):
        self.returned.append(item)

    def qsize(self):
        return 0


def _bare_manager(idle):
    from queue import Empty as _E  # noqa: F401
    from threading import Lock

    from shared_layer.database.connection import ConnectionManager

    mgr = object.__new__(ConnectionManager)
    mgr._idle = idle
    mgr._lock = Lock()
    mgr._connection_count = 1
    mgr._dedicated_count = 0
    mgr._max_size = 1
    mgr._opened = True
    mgr._pool_wait_timeouts = 0
    return mgr


def test_pool_wait_timeout_published(tmp_path, monkeypatch):
    """滿池逾時 → pool_wait_timeouts+1 ＋ pg_wait_ms 注入（Empty 仍向上傳）。"""
    plane = AdaptiveDataPlane()
    monkeypatch.setattr("shared_layer.adaptive.get_plane", lambda: plane)

    mgr = _bare_manager(_EmptyThenRaise())
    with pytest.raises(Empty):
        with mgr.connection():
            pass
    assert plane.signals.pool_wait_timeouts == 1
    assert plane.signals.pg_wait_ms >= 0.0


def test_pool_wait_success_publishes_wait_ms(tmp_path, monkeypatch):
    """滿池等待後成功取得 → pg_wait_ms 更新、逾時計數不變。"""
    plane = AdaptiveDataPlane()
    monkeypatch.setattr("shared_layer.adaptive.get_plane", lambda: plane)

    idle = _EmptyThenConn()
    mgr = _bare_manager(idle)
    with mgr.connection() as conn:
        assert conn is idle.conn
    assert plane.signals.pool_wait_timeouts == 0
    assert plane.signals.pg_wait_ms >= 0.0
    assert idle.returned == [idle.conn]  # 連線歸還池


def test_qdrant_search_publishes_latency(tmp_path, monkeypatch):
    """QdrantCanonicalRuntime.search → qdrant_latency_ms 注入。"""
    import asyncio
    from types import SimpleNamespace

    plane = AdaptiveDataPlane()
    monkeypatch.setattr("shared_layer.adaptive.get_plane", lambda: plane)

    from core_system.rag.rag_qdrant import QdrantCanonicalRuntime

    class _Resp:
        points = []

    class _Client:
        def query_points(self, **kwargs):
            return _Resp()

    rt = object.__new__(QdrantCanonicalRuntime)
    rt.config = SimpleNamespace(
        collection_name="col", top_k=5, score_threshold=0.1
    )
    rt.client = _Client()
    rt._healthy = True

    hits = asyncio.run(
        rt.search(query_vector=[0.1, 0.2], module_ids=("mod-1",))
    )
    assert hits == []
    assert plane.signals.qdrant_latency_ms >= 0.0


def test_qdrant_search_failure_silent_on_plane_error(tmp_path, monkeypatch):
    """plane 拋錯不影響檢索主流程（失敗靜默）。"""
    import asyncio
    from types import SimpleNamespace

    class _BadPlane:
        def observe_merge(self, *a, **k):
            raise RuntimeError("plane-down")

    monkeypatch.setattr(
        "shared_layer.adaptive.get_plane", lambda: _BadPlane()
    )

    from core_system.rag.rag_qdrant import QdrantCanonicalRuntime

    class _Resp:
        points = []

    class _Client:
        def query_points(self, **kwargs):
            return _Resp()

    rt = object.__new__(QdrantCanonicalRuntime)
    rt.config = SimpleNamespace(
        collection_name="col", top_k=5, score_threshold=0.1
    )
    rt.client = _Client()
    rt._healthy = True

    hits = asyncio.run(
        rt.search(query_vector=[0.1], module_ids=("mod-1",))
    )
    assert hits == []  # 檢索本身成功


def test_rag_health_check_feeds_backlog_and_degraded(monkeypatch):
    """CanonicalRagPipeline.health_check → qdrant_backlog/degraded/
    degraded_seconds 欄位級注入（欄位所有權：不覆寫他人生產者）。"""
    import asyncio
    from types import SimpleNamespace

    from core_system.rag.pipeline import CanonicalRagPipeline
    from core_system.rag.rag_qdrant import RagPipelineConfig

    cfg = RagPipelineConfig(
        qdrant_url="http://unused",
        qdrant_api_key=None,
        collection_name="col",
        postgresql_dsn="postgresql://unused",
    )
    pipe = CanonicalRagPipeline(cfg)
    pipe.qdrant = SimpleNamespace(
        is_healthy=lambda: False, last_error=None, collection_error=None
    )

    async def _outbox_stats():
        return {"pending": 7, "retry": 0, "dead_letter": 0}

    pipe.postgresql = SimpleNamespace(
        is_healthy=lambda: False,
        outbox_stats=_outbox_stats,
        reconciliation_status=None,
    )
    # DEGRADED：stores 不健康 → attempt_recovery 早退、維持降級態
    pipe._state_machine.evaluate_startup(
        qdrant_healthy=False,
        postgresql_healthy=False,
        index_state_matches=False,
    )

    plane = AdaptiveDataPlane()
    plane.observe_merge(LoadSignals(cpu_pct=50.0), fields=("cpu_pct",))
    monkeypatch.setattr("shared_layer.adaptive.get_plane", lambda: plane)

    asyncio.run(pipe.health_check())

    assert plane.signals.qdrant_backlog == 7
    assert plane.signals.degraded is True
    assert plane.signals.degraded_seconds >= 0.0
    assert plane.signals.cpu_pct == 50.0  # 不互踩


def test_rag_health_check_canonical_reports_not_degraded(monkeypatch):
    """CANONICAL 態：degraded=False、degraded_seconds=0、backlog 仍實測。"""
    import asyncio
    from types import SimpleNamespace

    from core_system.rag.pipeline import CanonicalRagPipeline
    from core_system.rag.rag_qdrant import RagPipelineConfig

    cfg = RagPipelineConfig(
        qdrant_url="http://unused",
        qdrant_api_key=None,
        collection_name="col",
        postgresql_dsn="postgresql://unused",
    )
    pipe = CanonicalRagPipeline(cfg)
    pipe.qdrant = SimpleNamespace(
        is_healthy=lambda: True, last_error=None, collection_error=None
    )

    async def _outbox_stats():
        return {"pending": 0}

    pipe.postgresql = SimpleNamespace(
        is_healthy=lambda: True,
        outbox_stats=_outbox_stats,
        reconciliation_status=None,
    )
    pipe._state_machine.evaluate_startup(
        qdrant_healthy=True,
        postgresql_healthy=True,
        index_state_matches=True,
    )

    plane = AdaptiveDataPlane()
    monkeypatch.setattr("shared_layer.adaptive.get_plane", lambda: plane)

    asyncio.run(pipe.health_check())

    assert plane.signals.qdrant_backlog == 0
    assert plane.signals.degraded is False
    assert plane.signals.degraded_seconds == 0.0
