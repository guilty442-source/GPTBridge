"""星澄自我學習與自動升級（``star-self-learning/v1``）。

每個循環（全程留審計、fail-closed）：

  1. 收集已驗證範例（``language_training_example``：active 且品質達門檻）
  2. 與上次訓練後的總數比較；未達政策門檻 → idle（不訓練）
  3. 匯出 ``star-transformer-sft/v1`` 快照並登錄訓練資料集
  4. 以生命週期 active 權重為 init，排入 governed SFT job 並執行
  5. 對政策指定的評估套件評測；全部通過才登錄 adapter
  6. adapter：``validated`` → ``staged`` →（政策允許時）``activate``
  7. 更新狀態與生命週期；任何一步失敗都不變更 active 權重

政策檔：``runtime/settings/self-learning.json``（kill switch：``enabled=false``）。
狀態檔：``xingcheng/runtime/state/self-learning.json``。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure all required modules are importable (shared_layer -> governance_rule -> main-system)
_PROJECT_ROOT = Path(__file__).resolve().parents[8]
for _p in (
    str(_PROJECT_ROOT / "shared-layer" / "src"),
    str(_PROJECT_ROOT),
    str(_PROJECT_ROOT / "main-system" / "src-core"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import argparse
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as clock_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Callable, Mapping, Sequence

POLICY_FORMAT = "star-self-learning-policy/v1"
STATE_FORMAT = "star-self-learning-state/v1"
POLICY_RELATIVE = "runtime/settings/self-learning.json"
STATE_RELATIVE = "xingcheng/runtime/state/self-learning.json"
SNAPSHOT_RELATIVE = "xingcheng/runtime/state/self-learning"
LOG_RELATIVE = "xingcheng/runtime/logs"
MIN_QUALITY = 0.8
_SCOPE_MAP = {"main", "investment", "mathematical", "coding"}


@dataclass
class SelfLearningPolicy:
    enabled: bool = True
    min_new_examples: int = 24
    auto_activate: bool = True
    suites: tuple[str, ...] = ("star-native-eval-dialogue-v1",)
    max_steps: int = 400
    batch_size: int = 8
    grad_accum: int = 2
    max_length: int = 256
    lr: float = 5e-5
    warmup_steps: int = 20
    device: str = "cuda"
    gpu_required_mb: int = 0
    val_permille: int = 100
    training_timezone: str = "Asia/Taipei"
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    # §2.7 閘門預設關閉（0/disabled），受管設定檔明示開啟——沿用
    # min_new_examples 等欄位慣例：程式預設為後備值，生效值以政策檔為準。
    # §2.7-1 觸發政策：兩次訓練啟動的最小間隔（事件驅動＋最低間隔）
    min_interval_s: int = 0
    # §2.7-4/8 資源與防爆走：每日訓練嘗試次數上限（UTC 日計）
    max_cycles_per_day: int = 0
    # §2.7-8 防爆走：連續訓練失敗熔斷；達上限後 stop（政策檔調整才可復歸）
    max_consecutive_failures: int = 0
    # §2.7-4 與推論互斥：星澄推論引擎已載入（含閒置快取）時不啟動訓練；
    # 狀態無法判定時 fail-closed 阻斷
    inference_exclusion: bool = True
    # §2.7-2 合成／自我生成資料比例上限（防自我放大；0=停用）。
    # source_type 以任一前綴開頭者計入合成／自我生成。
    max_synthetic_ratio: float = 0.0
    synthetic_source_prefixes: tuple[str, ...] = (
        "synthetic",
        "self-distillation",
    )
    # §2.7-1 品質漂移護欄：範例池平均品質低於下限即停（0=停用）
    min_pool_avg_quality: float = 0.0
    # §2.7-3 課程選擇：依 maturity 未達項決定本循環課程
    # （預設關閉＝沿用固定 SFT；開啟時 maturity 狀態不可讀 → fail-closed）
    curriculum_enabled: bool = False
    # 課程→intent 過濾（空 dict＝不過濾）；過濾後無資料 → blocked
    curriculum_intent_map: dict[str, list[str]] = field(default_factory=dict)
    # §2.7-9 升級後 maturity 重測（預設關閉；結果記錄於報告與狀態）
    post_upgrade_maturity_recheck: bool = False
    maturity_recheck_device: str = "cpu"
    # §2.7-1 能力退化探針觸發：資料未達 min_new_examples 時，以探針套件
    # 的 baseline_metrics 量測現役權重；退化即降門檻觸發訓練。
    # 探針量測失敗 fail-closed＝不觸發（錯誤記錄於循環結果與狀態）。
    degradation_probe_enabled: bool = False
    degradation_probe_suite: str = "star-native-eval-dialogue-v1"
    # 退化觸發時仍要求的最少新範例數（預設 1；0 新例無課程可訓）
    degradation_min_examples: int = 1
    # §2.7-2 資料就緒閘：去重與汙染排除（預設開啟——資料安全屬性）
    # 去重：(input_text, target_text) 全等對跨 scope 只留首見（scope 字典序）。
    dedup_enabled: bool = True
    # 汙染排除：含 maturity 探針值（chat-foundation PROBE_VALUES）者一律剔除。
    exclude_probe_values: bool = True
    # §2.7-8 防爆走：單循環進入資料集的範例總數上限（0=不設限）；
    # 超限決定性截斷（scope 字典序→revision 序保留前 N），截斷量入帳。
    max_dataset_examples: int = 0
    # §2.7-8 訓練中資源超支即停：executor 內嵌預算計時（0=不設限）。
    # 超限時訓練器在步邊界跳出、summary 記 stopped_reason；循環層
    # fail-closed 記 resource-overspend，不評估不啟用。
    train_time_budget_s: int = 0
    train_vram_budget_mb: int = 0
    # §2.7-4 每循環 GPU 時間預算：訓練器逐步累計 CUDA 活躍秒數，步邊界
    # 超限即停（0=不設限）；與 wall-clock 預算各自獨立計量
    train_gpu_budget_s: int = 0
    # §2.7-1/8 GPU 不可用退避：EXECUTOR_GPU_BUSY 失敗時按下限指數退避
    # 下次嘗試（0=停用＝每輪照原間隔重試）。GPU 長期被佔時避免
    # 每循環空燒每日預算與 lifecycle FAILED 轉移噪音。
    gpu_busy_backoff_s: int = 0
    gpu_busy_backoff_cap_s: int = 0
    # §2.7-3 DPO 課程型別：paired 偏好對（品質閘門拒絕半邊＋owner 驗證
    # chosen 半邊）驅動的對齊循環。預設關閉，受管設定檔明示開啟；
    # 資料達閾時本循環優先走 DPO（paired 樣本稀缺且對齊價值高），
    # 未達閾完全不影響 SFT 路徑。
    dpo_enabled: bool = False
    # 距上次 DPO 循環至少需新增的 paired 對數（語意同 min_new_examples）
    dpo_min_new_pairs: int = 8
    # DPO KL 錨定強度（reference＝循環起點的現役權重凍結副本）
    dpo_beta: float = 0.1
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["format"] = POLICY_FORMAT
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SelfLearningPolicy":
        fields = {
            key: value
            for key, value in dict(data).items()
            if key in cls.__dataclass_fields__
        }
        if "suites" in fields and not isinstance(fields["suites"], tuple):
            fields["suites"] = tuple(str(item) for item in fields["suites"])
        if "synthetic_source_prefixes" in fields and not isinstance(
            fields["synthetic_source_prefixes"], tuple
        ):
            fields["synthetic_source_prefixes"] = tuple(
                str(item) for item in fields["synthetic_source_prefixes"]
            )

        return cls(**fields)


def policy_path(tool_root: str | Path) -> Path:
    return Path(tool_root) / POLICY_RELATIVE


def load_policy(tool_root: str | Path) -> SelfLearningPolicy:
    path = policy_path(tool_root)
    if not path.is_file():
        return SelfLearningPolicy()
    try:
        return SelfLearningPolicy.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return SelfLearningPolicy()


def training_window_status(
    policy: SelfLearningPolicy,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return whether autonomous training is allowed in Taipei local time.

    Invalid timezone or clock configuration is fail-closed.  A window whose
    start is later than its end crosses midnight (22:00→07:00 by default).
    """
    try:
        zone = ZoneInfo(policy.training_timezone)
        start_parts = tuple(int(part) for part in policy.quiet_hours_start.split(":", 1))
        end_parts = tuple(int(part) for part in policy.quiet_hours_end.split(":", 1))
        if len(start_parts) != 2 or len(end_parts) != 2:
            raise ValueError("time must be HH:MM")
        start = clock_time(*start_parts)
        end = clock_time(*end_parts)
        local = (now or datetime.now(timezone.utc)).astimezone(zone).time()
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return {
            "allowed": False,
            "reason": "invalid-training-window",
            "timezone": policy.training_timezone,
        }
    quiet = (local >= start or local < end) if start > end else start <= local < end
    return {
        "allowed": not quiet,
        "reason": "quiet-hours" if quiet else "allowed",
        "timezone": policy.training_timezone,
        "local_time": local.strftime("%H:%M"),
        "quiet_hours": f"{policy.quiet_hours_start}-{policy.quiet_hours_end}",
    }


def save_policy(tool_root: str | Path, policy: SelfLearningPolicy) -> dict[str, Any]:
    path = policy_path(tool_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = policy.to_dict()
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp, path)
    return payload


def _state_path(tool_root: str | Path) -> Path:
    return Path(tool_root) / STATE_RELATIVE


def load_state(tool_root: str | Path) -> dict[str, Any]:
    path = _state_path(tool_root)
    if not path.is_file():
        return {"format": STATE_FORMAT, "trained_example_total": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"format": STATE_FORMAT, "trained_example_total": 0}
    return data if isinstance(data, dict) else {"format": STATE_FORMAT}


def save_state(tool_root: str | Path, state: Mapping[str, Any]) -> None:
    path = _state_path(tool_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": STATE_FORMAT, **dict(state), "updated_at": _iso_now()}
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def collect_verified_examples(
    tool_root: str | Path, *, min_quality: float = MIN_QUALITY
) -> dict[str, list[dict[str, Any]]]:
    """讀取各角色資料庫中已驗證（active、品質達標）的訓練範例。"""
    models_dir = Path(tool_root) / "xingcheng" / "runtime" / "state" / "models"
    by_scope: dict[str, list[dict[str, Any]]] = {}
    if not models_dir.is_dir():
        return by_scope
    for path in sorted(models_dir.glob("*.sqlite3")):
        scope = path.stem
        if scope not in _SCOPE_MAP:
            continue
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            rows = connection.execute(  # sql-ok: one read-only query per scope database file
                "SELECT revision, example_id, intent, input_text, target_text, "
                "source_type, quality_score FROM language_training_example "
                "WHERE active = 1 AND quality_score >= ? ORDER BY revision",
                (float(min_quality),),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            connection.close()
        records: list[dict[str, Any]] = []
        for revision, example_id, intent, input_text, target_text, source_type, quality_score in rows:
            prompt = str(input_text or "").strip()
            completion = str(target_text or "").strip()
            if not prompt or not completion:
                continue
            records.append(
                {
                    "revision": int(revision or 0),
                    "example_id": str(example_id or ""),
                    "intent": str(intent or ""),
                    "input_text": prompt,
                    "target_text": completion,
                    "source_type": str(source_type or ""),
                    "quality_score": float(quality_score or 0.0),
                }
            )
        if records:
            by_scope[scope] = records
    return by_scope


def collect_preference_pairs(tool_root: str | Path) -> list[dict[str, Any]]:
    """讀取各角色資料庫中已配對（paired=1）的 chosen/rejected 偏好對。

    與 ``collect_verified_examples`` 同一資料面（唯讀 sqlite、同 scope
    白名單）；資料表不存在或損毀的資料庫一律略過（fail-closed 視為
    無資料而非錯誤）。回傳值直接餵給
    ``preference_dataset_bridge.build_pairs_snapshot``。"""
    models_dir = Path(tool_root) / "xingcheng" / "runtime" / "state" / "models"
    pairs: list[dict[str, Any]] = []
    if not models_dir.is_dir():
        return pairs
    for path in sorted(models_dir.glob("*.sqlite3")):
        if path.stem not in _SCOPE_MAP:
            continue
        try:
            connection = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True
            )
        except sqlite3.Error:
            continue
        try:
            rows = connection.execute(  # sql-ok: one read-only query per scope database file
                "SELECT revision, pair_id, intent, prompt_text, chosen_text,"
                " rejected_text FROM language_preference_pair"
                " WHERE paired = 1 ORDER BY revision",
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            connection.close()
        for revision, pair_id, intent, prompt, chosen, rejected in rows:
            prompt = str(prompt or "").strip()
            chosen = str(chosen or "").strip()
            rejected = str(rejected or "").strip()
            if not (prompt and chosen and rejected):
                continue
            pairs.append(
                {
                    "revision": int(revision or 0),
                    "pair_id": str(pair_id or ""),
                    "intent": str(intent or ""),
                    "prompt_text": prompt,
                    "chosen_text": chosen,
                    "rejected_text": rejected,
                    "scope": path.stem,
                }
            )
    pairs.sort(key=lambda record: (str(record["scope"]), int(record["revision"])))
    return pairs


def run_cycle(
    tool_root: str | Path,
    *,
    policy: SelfLearningPolicy | None = None,
    train_fn: Callable[..., dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """執行一次自我學習循環；回傳結構化結果（永不自行 raise 至呼叫端外）。

    retention 掃除在 ``self_learning_support.run_cycle`` 內執行，
    保證 CLI（``--run-once``／``--watch``）路徑同樣涵蓋（§10.67）。
    """
    from .self_learning_support import run_cycle as _run_cycle

    return _run_cycle(
        tool_root, policy=policy, train_fn=train_fn, force=force
    )


def main(argv: list[str] | None = None) -> int:
    from .self_learning_support import run_cli

    return run_cli(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "POLICY_FORMAT",
    "POLICY_RELATIVE",
    "STATE_FORMAT",
    "SelfLearningPolicy",
    "collect_verified_examples",
    "load_policy",
    "load_state",
    "main",
    "policy_path",
    "run_cycle",
    "save_policy",
    "save_state",
]
