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

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
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
    val_permille: int = 100

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
            rows = connection.execute(
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


def run_cycle(
    tool_root: str | Path,
    *,
    policy: SelfLearningPolicy | None = None,
    train_fn: Callable[..., dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """執行一次自我學習循環；回傳結構化結果（永不自行 raise 至呼叫端外）。"""
    from .self_learning_support import run_cycle_impl

    return run_cycle_impl(
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
