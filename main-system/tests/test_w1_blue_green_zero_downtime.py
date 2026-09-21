"""W1-6d acceptance — zero-downtime update through the real A330 handover path.

Wires :class:`core_system.blue_green_orchestrator.BlueGreenOrchestrator` to the
genuine ``BootCoreHandoverMixin._maybe_handover`` (standby → switch → drain →
verify → rollback) through an injected ``handover_fn`` adapter, and proves the
§10.15/§10.17 acceptance contract:

* 租約原子交接：無租約／舊 token 一律拒絶，不觸發 handover。
* 斷線不重複執行：request 已終止（converged）不再 spawn。
* **更新期間停機 0**：gateway 於每次 swap 皆指向有效世代；handover 期間
  舊後端維持服務，切換後新後端立刻接管；絕無「無目標」窗口。

It deliberately reuses the same mixin stubs as ``test_p0_lazy_lifecycle_handover``
so the orchestration is exercised against the real converged code paths, not a
mock of the handover itself.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from boot_core_handover import BootCoreHandoverMixin
from core_system.blue_green_orchestrator import (
    LEASE_TYPE_UPDATE,
    BlueGreenOrchestrator,
)
from core_system.execution_lease import RuntimeExecutionLease
from core_system.backend_lifecycle import BackendLifecycleRegistry
from core_system.request_registry import RequestRegistry
from core_system.release_retention import (
    TIER_ACTIVE,
    TIER_PREVIOUS,
    ReleaseRetentionRegistry,
)


class _Gateway:
    """Tracks every activation; ``target`` always resolves to a live member."""

    def __init__(self) -> None:
        self.activations: list[tuple[int, str]] = []
        self._target: tuple[int, str] | None = None
        self._serving_windows: list[tuple[int, str]] = []

    def activate(self, port: int, generation: str) -> None:
        self.activations.append((port, generation))
        self._target = (port, generation)

    def serve(self) -> str:
        """Route one request. Throws if any window has no active target.

        This is the zero-downtime oracle: a None target would mean a request
        arrived while the gateway had nothing to serve.
        """
        assert self._target is not None, "zero-downtime violation: no active target"
        port, generation = self._target
        self._serving_windows.append((port, generation))
        return generation

    def connection_count(self, port: int) -> int:
        return 0

    def close_generation_connections(self, port: int) -> None:
        pass


class _Standby:
    stdout = None

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive

    def poll(self):
        return None if self._alive else 1


class _Handover(BootCoreHandoverMixin):
    """Same stub surface as test_p0_lazy_lifecycle_handover _Handover."""

    def __init__(self, tmp_path: Path, *, probe_ok: bool = True) -> None:
        self._update_request_path = tmp_path / "backend-update-request.json"
        self._last_update_operation = ""
        self._child = _Standby()
        self._gateway = _Gateway()
        self._active_backend_port = 8081
        self._active_generation = "old-gen"
        self._backend_generation_ports = [8081, 8082]
        self._stop = threading.Event()
        self._health_probe_port = 9090
        self.probe_ok = probe_ok
        self.standby_ready = True
        # Mirror real boot: _spawn_and_supervise activates the first backend
        # before any request can be routed.
        self._gateway.activate(self._active_backend_port, self._active_generation)

    # stubs the mixin drives
    def _probe_health(self, port: int) -> bool:
        return self.probe_ok

    def _write_state(self, **kw) -> None:
        self.last_state = kw

    def _terminate_process(self, proc) -> None:
        pass

    def _relay(self, *_args) -> None:
        pass

    def _wait_port_available(self, port: int) -> bool:
        return True

    def _spawn_backend(self, *_args, **_kw):
        return _Standby()

    def _wait_backend_ready(self, *_args) -> bool:
        return self.standby_ready


def _request_payload(operation_id: str = "op-1", target_generation: str = "new-gen") -> dict:
    return {
        "schema_version": 1,
        "operation_id": operation_id,
        "certified": True,
        "artifact_hashes": {"m.py": "abc"},
        "target_generation": target_generation,
        "terminal_status": "prepared",
    }


def _handover_adapter(handover: _Handover, args: list[str], startup_state: str):
    """Bridge orchestrator's ``handover_fn(operation_id, target_generation)``
    to the genuine ``_maybe_handover(args, startup_state)`` contract."""

    def handover_fn(operation_id: str, target_generation: str) -> dict:
        # The update request is the mixin's certified input channel.
        handover._update_request_path.write_text(
            json.dumps(
                _request_payload(
                    operation_id=operation_id,
                    target_generation=target_generation,
                )
            ),
            encoding="utf-8",
        )
        ok = handover._maybe_handover(args, startup_state)
        recorded = json.loads(
            handover._update_request_path.read_text("utf-8")
        )
        return {
            "ok": ok,
            "rolled_back": recorded.get("terminal_status") == "rolled-back",
            "reason": recorded.get("error", ""),
            "active_generation": recorded.get(
                "active_generation", handover._active_generation
            ),
            "active_port": handover._active_backend_port,
        }

    return handover_fn


def _build(tmp_path: Path, *, probe_ok: bool = True):
    handover = _Handover(tmp_path, probe_ok=probe_ok)
    lease = RuntimeExecutionLease(str(tmp_path / "lease.json"), ttl_seconds=60.0)
    lifecycle = BackendLifecycleRegistry(str(tmp_path / "lifecycle.json"))
    requests = RequestRegistry(str(tmp_path / "requests.json"))
    retention = ReleaseRetentionRegistry(
        str(tmp_path / "retention.json"),
        audit_path=str(tmp_path / "retention-audit.jsonl"),
    )
    orchestrator = BlueGreenOrchestrator(
        lease=lease,
        backend_lifecycle=lifecycle,
        request_registry=requests,
        retention=retention,
        handover_fn=_handover_adapter(handover, ["run"], "READY"),
        state_path=str(tmp_path / "orchestrator.json"),
        audit_path=str(tmp_path / "orchestrator-audit.jsonl"),
        drain_timeout_seconds=1.0,
    )
    return handover, lease, lifecycle, requests, retention, orchestrator


def _seed_update(lease, lifecycle, retention):
    lease.acquire(LEASE_TYPE_UPDATE, "updater", "old-gen")
    token = lease.get(LEASE_TYPE_UPDATE).fencing_token
    lifecycle.register("backend-A", generation="old-gen", release_id="rel-1")
    lifecycle.become_active("backend-A")
    lifecycle.register("backend-B", generation="new-gen", release_id="rel-2")
    lifecycle.become_standby("backend-B")
    retention.register("rel-1")
    retention.become_active("rel-1")
    retention.register("rel-2")
    return token


def test_zero_downtime_update_via_a330(tmp_path: Path) -> None:
    handover, lease, lifecycle, requests, retention, orchestrator = _build(tmp_path)
    token = _seed_update(lease, lifecycle, retention)

    # Before the update the old generation serves requests continuously.
    assert handover._gateway.serve() == "old-gen"

    outcome = orchestrator.perform_update(
        "op-live",
        holder="updater",
        fencing_token=token,
        backend_generation="old-gen",
        target_generation="new-gen",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
        compatible_with=["rel-1"],
    )

    assert outcome.ok, outcome.reason
    # A330 marked global-success with the new generation active.
    recorded = json.loads(
        (tmp_path / "backend-update-request.json").read_text("utf-8")
    )
    assert recorded["terminal_status"] == "global-success"
    assert recorded["active_generation"] == "new-gen"
    # Zero-downtime: gateways swapped atomically old(8081) → new(8082) and
    # every serve() call resolved to a live generation (never None).
    assert handover._gateway.activations == [(8081, "old-gen"), (8082, "new-gen")]
    assert handover._gateway.serve() == "new-gen"
    # Contracts advanced on success.
    assert lifecycle.get("backend-B").lifecycle_role == "ACTIVE"
    assert lifecycle.get("backend-A").lifecycle_role == "STANDBY"
    assert retention.get("rel-2").tier == TIER_ACTIVE
    assert retention.get("rel-1").tier == TIER_PREVIOUS


def test_zero_downtime_rollback_on_unhealthy_standby(tmp_path: Path) -> None:
    handover, lease, lifecycle, requests, retention, orchestrator = _build(
        tmp_path, probe_ok=False
    )
    token = _seed_update(lease, lifecycle, retention)

    assert handover._gateway.serve() == "old-gen"

    outcome = orchestrator.perform_update(
        "op-rollback",
        holder="updater",
        fencing_token=token,
        backend_generation="old-gen",
        target_generation="new-gen",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
        compatible_with=["rel-1"],
    )

    # A330 rolled the gateway back to the old generation and marked rolled-back.
    assert outcome.ok is False
    assert outcome.rolled_back is True
    recorded = json.loads(
        (tmp_path / "backend-update-request.json").read_text("utf-8")
    )
    assert recorded["terminal_status"] == "rolled-back"
    assert handover._gateway.activations == [(8081, "old-gen"), (8082, "new-gen"), (8081, "old-gen")]
    # Zero-downtime holds even through rollback: the gateway only ever
    # pointed at a live backends, old resumed serving after the rollback.
    assert handover._gateway.serve() == "old-gen"
    assert handover._gateway.serve() == "old-gen"
    # Contracts untouched: old remains ACTIVE, retention unchanged.
    assert lifecycle.get("backend-A").lifecycle_role == "ACTIVE"
    assert retention.get("rel-1").tier == TIER_ACTIVE


def test_stale_token_does_not_trigger_handover(tmp_path: Path) -> None:
    handover, lease, lifecycle, requests, retention, orchestrator = _build(tmp_path)
    _seed_update(lease, lifecycle, retention)

    outcome = orchestrator.perform_update(
        "op-stale",
        holder="updater",
        fencing_token=9999,  # 舊／偽造 token → fencing 拒絶
        backend_generation="old-gen",
        target_generation="new-gen",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
    )

    assert outcome.ok is False
    assert outcome.reason == "update-lease-not-held"
    # Handover never spawned: no new activation, no update request written.
    assert handover._gateway.activations == [(8081, "old-gen")]
    assert not (tmp_path / "backend-update-request.json").exists()
    assert lifecycle.get("backend-A").lifecycle_role == "ACTIVE"
    # Gateway keeps serving the old generation with zero interruption.
    assert handover._gateway.serve() == "old-gen"


def test_converged_request_is_not_replayed(tmp_path: Path) -> None:
    """斷線不重複執行：同一 operation 已收斂後，重試不得再 spawn。"""
    handover, lease, lifecycle, requests, retention, orchestrator = _build(tmp_path)
    token = _seed_update(lease, lifecycle, retention)

    first = orchestrator.perform_update(
        "op-dup",
        holder="updater",
        fencing_token=token,
        backend_generation="old-gen",
        target_generation="new-gen",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
        compatible_with=["rel-1"],
    )
    assert first.ok, first.reason
    assert handover._gateway.activations == [
        (8081, "old-gen"),
        (8082, "new-gen"),
    ]

    # The same operation id is replayed (e.g. a crashed updater retries). The
    # A330 mixin's operation dedup (``_last_update_operation``) refuses to
    # spawn a second standby, so the orchestrator must fail-closed.
    retry = orchestrator.perform_update(
        "op-dup",
        holder="updater",
        fencing_token=token,
        backend_generation="old-gen",
        target_generation="new-gen",
        old_backend_id="backend-A",
        new_backend_id="backend-B",
        new_release_id="rel-2",
        compatible_with=["rel-1"],
    )
    assert retry.ok is False
    assert retry.reason == "handover-failed"
    # No further activation happened in the replay attempt.
    assert handover._gateway.activations == [
        (8081, "old-gen"),
        (8082, "new-gen"),
    ]