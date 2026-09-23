"""§10.7 模型資源排程（`star-model-resource/v1`）。

Model Resource Manager：角色策略＋資源閘門＋切換量測。

角色政策（藍圖 §10.7）：
- ``xingcheng_native``：既定保留（resident）
- ``fast_chat``：優先保留（resident）
- ``coding_large``：有需求才載入（on_demand）；**載入不得使互動角色失去回應**
- ``embedding``：索引期間按需（on_demand）
- ``reranker``：檢索時按需（on_demand）
- ``deep_reasoning``：排程或按需（scheduled）

資源閘門（fail-closed）：VRAM／RAM 不足 → ``admitted=False``，
不靜默載入；interactive 類角色保留 ``interactive_headroom_mb``
緩衝，大型模型載入不得吃掉它。

切換量測：載入時間、首 Token 延遲、tokens/s、RAM／VRAM 峰值、
context 使用量——append 到 ``runtime/state/model-resource-ledger.jsonl``，
「不得因模型較小就假設一定更快」——一切由量測證據說話。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

_logger = logging.getLogger("gptbridge.model_resource")

MANAGER_VERSION = "star-model-resource/v1"


class ModelRole(str, Enum):
    XINGCHENG_NATIVE = "xingcheng_native"
    FAST_CHAT = "fast_chat"
    CODING_LARGE = "coding_large"
    EMBEDDING = "embedding"
    RERANKER = "reranker"
    DEEP_REASONING = "deep_reasoning"


class Retention(str, Enum):
    RESIDENT = "resident"
    ON_DEMAND = "on_demand"
    SCHEDULED = "scheduled"


@dataclass(frozen=True)
class RolePolicy:
    retention: Retention
    priority: int  # 數字小者優先保留
    interactive: bool = False  # 互動路徑：不得被大型載入拖垮


DEFAULT_POLICIES: dict[ModelRole, RolePolicy] = {
    ModelRole.XINGCHENG_NATIVE: RolePolicy(Retention.RESIDENT, 10, interactive=True),
    ModelRole.FAST_CHAT: RolePolicy(Retention.RESIDENT, 20, interactive=True),
    ModelRole.EMBEDDING: RolePolicy(Retention.ON_DEMAND, 30),
    ModelRole.RERANKER: RolePolicy(Retention.ON_DEMAND, 40),
    ModelRole.DEEP_REASONING: RolePolicy(Retention.SCHEDULED, 50),
    ModelRole.CODING_LARGE: RolePolicy(Retention.ON_DEMAND, 60),
}


@dataclass
class LoadedModel:
    model_id: str
    role: str
    vram_mb: int
    ram_mb: int
    loaded_at: float = field(default_factory=time.time)


@dataclass
class AdmitDecision:
    admitted: bool
    reason: str
    role: str
    model_id: str


class ModelResourceManager:
    """受管模型載入排程器。

    ``gpu_status_fn`` 回傳具 ``free_mb`` 屬性的物件或 ``None``；
    ``ram_free_fn`` 回傳可用 RAM MB 或 ``None``。任一來源不可用時
    fail-closed（假設資源不足）。
    """

    def __init__(
        self,
        *,
        ledger_path: str | Path,
        gpu_free_fn: Optional[Callable[[], Optional[float]]] = None,
        ram_free_fn: Optional[Callable[[], Optional[float]]] = None,
        interactive_headroom_mb: int = 1024,
        policies: Optional[dict[ModelRole, RolePolicy]] = None,
    ) -> None:
        self._ledger_path = Path(ledger_path)
        self._gpu_free_fn = gpu_free_fn
        self._ram_free_fn = ram_free_fn
        self._interactive_headroom_mb = interactive_headroom_mb
        self._policies = dict(policies or DEFAULT_POLICIES)
        self._loaded: dict[str, LoadedModel] = {}

    # -- admission ----------------------------------------------------------

    def request_load(
        self,
        role: ModelRole | str,
        model_id: str,
        *,
        vram_mb: int = 0,
        ram_mb: int = 0,
    ) -> AdmitDecision:
        """資源閘門：通過才登錄為 loaded；失敗 fail-closed。"""
        cls = role if isinstance(role, ModelRole) else ModelRole(str(role))
        policy = self._policies[cls]

        gpu_free = self._gpu_free_fn() if self._gpu_free_fn else None
        ram_free = self._ram_free_fn() if self._ram_free_fn else None

        if vram_mb and gpu_free is None:
            return AdmitDecision(False, "gpu-telemetry-unavailable", cls.value, model_id)
        if ram_mb and ram_free is None:
            return AdmitDecision(False, "ram-telemetry-unavailable", cls.value, model_id)

        headroom = (
            self._interactive_headroom_mb if not policy.interactive else 0
        )
        if vram_mb and gpu_free is not None and vram_mb > gpu_free - headroom:
            return AdmitDecision(
                False,
                f"vram-insufficient: need {vram_mb}MB + {headroom}MB headroom, "
                f"free {gpu_free:.0f}MB",
                cls.value,
                model_id,
            )
        if ram_mb and ram_free is not None and ram_mb > ram_free - headroom:
            return AdmitDecision(
                False,
                f"ram-insufficient: need {ram_mb}MB + {headroom}MB headroom, "
                f"free {ram_free:.0f}MB",
                cls.value,
                model_id,
            )

        self._loaded[model_id] = LoadedModel(
            model_id=model_id, role=cls.value, vram_mb=vram_mb, ram_mb=ram_mb
        )
        self._observe_plane()
        return AdmitDecision(True, "admitted", cls.value, model_id)

    def release(self, model_id: str) -> bool:
        released = self._loaded.pop(model_id, None) is not None
        if released:
            self._observe_plane()
        return released

    def _observe_plane(self) -> None:
        """P4 adaptive plane 生產者：模型子系統記憶體壓力。

        ``model_load_pct``＝受管模型已佔 RAM／VRAM 各自對「已佔＋可用」
        池的比例取 max（遙測缺失的池略過；兩池皆不可得則不寫，
        fail-closed）。欄位級合併，不覆寫其他生產者的量測。失敗靜默：
        訊號只是提示，不得影響資源閘門主流程。
        """
        try:
            managed_ram = sum(m.ram_mb for m in self._loaded.values())
            managed_vram = sum(m.vram_mb for m in self._loaded.values())
            shares: list[float] = []
            if self._ram_free_fn is not None:
                ram_free = self._ram_free_fn()
                if ram_free is not None and managed_ram + ram_free > 0:
                    shares.append(managed_ram / (managed_ram + ram_free) * 100.0)
            if self._gpu_free_fn is not None:
                gpu_free = self._gpu_free_fn()
                if gpu_free is not None and managed_vram + gpu_free > 0:
                    shares.append(
                        managed_vram / (managed_vram + gpu_free) * 100.0
                    )
            if not shares:
                return
            from shared_layer.adaptive import LoadSignals, get_plane

            get_plane().observe_merge(
                LoadSignals(model_load_pct=max(shares)),
                fields=("model_load_pct",),
            )
        except Exception:
            pass

    def loaded(self) -> list[LoadedModel]:
        return list(self._loaded.values())

    # -- measurement ledger ---------------------------------------------------

    def record_measurement(
        self, model_id: str, role: ModelRole | str, metrics: dict[str, Any]
    ) -> None:
        """切換量測：load_time_s / ttft_ms / tokens_per_s / ram_peak_mb /
        vram_peak_mb / cpu_offload / context_used——append JSONL 帳本。"""
        cls = role if isinstance(role, ModelRole) else ModelRole(str(role))
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "manager_version": MANAGER_VERSION,
            "model_id": model_id,
            "role": cls.value,
            "metrics": metrics,
        }
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


_LEDGER_PATH = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "model-resource-ledger.jsonl"
)


def _gpu_free_mb() -> Optional[float]:
    """VRAM 遙測：GPU 協調器雙源查詢；無 GPU／查詢失敗 → None（fail-closed）。"""
    try:
        from shared_layer.adaptive.gpu_coordinator import query_gpu

        status = query_gpu()
        return status.free_mb if status is not None else None
    except Exception:
        return None


def _ram_free_mb() -> Optional[float]:
    """RAM 遙測：native 優先（P24）；不可用 → None（fail-closed）。"""
    try:
        from shared_layer.performance.process_metrics import system_memory_available_bytes

        available = system_memory_available_bytes()
        return available / (1024 * 1024) if available > 0 else None
    except Exception:
        return None


_SHARED: "ModelResourceManager | None" = None


def get_model_resource_manager() -> ModelResourceManager:
    """§10.7：共享受管排程器。lazy 建構，import 期間零副作用。"""
    global _SHARED
    if _SHARED is None:
        _SHARED = ModelResourceManager(
            ledger_path=_LEDGER_PATH,
            gpu_free_fn=_gpu_free_mb,
            ram_free_fn=_ram_free_mb,
        )
    return _SHARED


__all__ = [
    "AdmitDecision",
    "DEFAULT_POLICIES",
    "LoadedModel",
    "MANAGER_VERSION",
    "ModelResourceManager",
    "ModelRole",
    "Retention",
    "RolePolicy",
    "get_model_resource_manager",
]
