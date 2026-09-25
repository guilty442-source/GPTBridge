"""Readiness capability matrix aggregation tests.

The six capability flags are a pure projection of the existing A67
readiness signals (dependency probes, backend runtime, governed
maintenance gate); these tests pin that mapping without probing real
services.
"""

from __future__ import annotations

from tasks import readiness_gate
from tasks.readiness_gate import CapabilityReadiness, ReadinessGate


class _Governance:
    def runtime_integrity_ready(self) -> bool:
        return True


class _App:
    def __init__(self) -> None:
        self.command_router = object()
        self.governance = _Governance()
        self.toolbox_service = object()
        self.maintenance_ready = True
        self._authenticated_ipc_connections = 1
        self.startup_dead = False


def _patch_probe(monkeypatch, reachable: dict[str, bool]) -> None:
    class _Result:
        def __init__(self, service: str) -> None:
            self.service = service
            self.reachable = reachable.get(service, False)

    def _probe(service: str, *, timeout: float = 0.75):
        return _Result(service)

    monkeypatch.setattr(readiness_gate, "probe_registered_local_service", _probe)


def test_all_capabilities_ready_when_signals_are_ready(monkeypatch) -> None:
    _patch_probe(
        monkeypatch, {"postgresql": True, "vectord": True, "ollama": True}
    )
    snapshot = ReadinessGate(_App()).evaluate()
    capabilities = snapshot.capabilities
    assert capabilities == CapabilityReadiness(
        information_ready=True,
        data_ready=True,
        semantic_ready=True,
        model_ready=True,
        tool_runtime_ready=True,
        git_maintenance_ready=True,
    )
    assert snapshot.as_dict()["capabilities"] == capabilities.as_dict()


def test_missing_semantic_index_degrades_information_only(monkeypatch) -> None:
    _patch_probe(
        monkeypatch, {"postgresql": True, "vectord": False, "ollama": True}
    )
    capabilities = ReadinessGate(_App()).evaluate().capabilities
    assert capabilities.data_ready is True
    assert capabilities.semantic_ready is False
    assert capabilities.information_ready is False
    assert capabilities.model_ready is True


def test_model_and_maintenance_gates_are_independent(monkeypatch) -> None:
    # Â§10.7 on-demandï¼šmodel_ready = ?¯é? OR å·²å?è£å¯?‰èµ·?”â€?
    # æ­¤è?æ¨¡æ“¬?Œæœªå®‰è??æ??½ä»¤ model_ready ??False??
    import core_system.ollama_demand as ollama_demand

    monkeypatch.setattr(ollama_demand, "ollama_installed", lambda: False)
    _patch_probe(
        monkeypatch, {"postgresql": True, "vectord": True, "ollama": False}
    )
    app = _App()
    app.maintenance_ready = False
    capabilities = ReadinessGate(app).evaluate().capabilities
    assert capabilities.model_ready is False
    assert capabilities.git_maintenance_ready is False
    assert capabilities.information_ready is True


def test_model_ready_when_ollama_installed_but_idle(monkeypatch) -> None:
    # on-demand å¸¸æ?ï¼šæ??™æœª?‹è?ä½†å¯?·è?æª”å??????½å?å°±ç?ï¼ˆdeferredï¼‰ã€?
    import core_system.ollama_demand as ollama_demand

    monkeypatch.setattr(ollama_demand, "ollama_installed", lambda: True)
    _patch_probe(
        monkeypatch, {"postgresql": True, "vectord": True, "ollama": False}
    )
    capabilities = ReadinessGate(_App()).evaluate().capabilities
    assert capabilities.model_ready is True


def test_tool_runtime_requires_backend_and_broker(monkeypatch) -> None:
    _patch_probe(
        monkeypatch, {"postgresql": True, "vectord": True, "ollama": True}
    )
    app = _App()
    app.toolbox_service = None
    assert ReadinessGate(app).evaluate().capabilities.tool_runtime_ready is False
    app.toolbox_service = object()
    app.command_router = None
    assert ReadinessGate(app).evaluate().capabilities.tool_runtime_ready is False


def test_qdrant_migration_window_backend(monkeypatch) -> None:
    # A610 transition: VECTOR_BACKEND=qdrant keeps qdrant as the semantic
    # probe source for the bounded migration window; vectord reachability
    # is irrelevant while that window is open.
    monkeypatch.setenv("VECTOR_BACKEND", "qdrant")
    _patch_probe(
        monkeypatch, {"postgresql": True, "vectord": False, "qdrant": True, "ollama": True}
    )
    capabilities = ReadinessGate(_App()).evaluate().capabilities
    assert capabilities.semantic_ready is True
    assert capabilities.information_ready is True


def test_rust_backend_ignores_qdrant_reachability(monkeypatch) -> None:
    # Post-takeover default: qdrant up/down does not drive semantic_ready;
    # only vectord does.
    monkeypatch.delenv("VECTOR_BACKEND", raising=False)
    _patch_probe(
        monkeypatch, {"postgresql": True, "vectord": True, "qdrant": False, "ollama": True}
    )
    capabilities = ReadinessGate(_App()).evaluate().capabilities
    assert capabilities.semantic_ready is True
    assert capabilities.information_ready is True
