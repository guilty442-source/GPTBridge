"""Dense → MoE sparse upcycling：把 dense checkpoint 轉成 MoE 初始權重。

做法（Mixtral-style upcycling）：
- attention / norm / embedding / lm_head 直接複製。
- 每個 MoE 層的 dense FFN（SwiGLU gate/up/down）複製進 E 個專家；
  router 維持預設小常態初始化 → top-k softmax 接近均勻 →
  MoE 輸出 ≈ dense 輸出（專家初始全同，等權平均即原 FFN）。
- 訓練時 router 噪聲 + token 級 top-k 指派讓專家逐漸分化；
  aux load-balancing loss 由 ``moe_aux_loss_weight`` 自動併入。

產物：``<out>/init.pt``（可直接作 ``sft.py --init-checkpoint``）+
``<out>/tokenizer.json``（embedded tokenizer 落盤，供 sha 對齊檢查）。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import torch

from ..checkpoint import load_checkpoint, save_checkpoint
from ..modules.model import XingChengForCausalLM

_MLP_KEY = re.compile(
    r"^model\.layers\.(\d+)\.mlp\.(gate_proj|up_proj|down_proj)\.weight$"
)


def _split_dense_ffn(
    gate: torch.Tensor,
    up: torch.Tensor,
    down: torch.Tensor,
    chunks: int,
) -> list[tuple[torch.Tensor, torch.Tensor, torch.Tensor]]:
    """把 dense SwiGLU FFN 沿 intermediate 維度切成 ``chunks`` 個子專家。

    gate/up 切輸出列、down 切輸入行 — 子專家輸出加總與原 FFN
    **數學等價**（矩陣乘的輸入維可分配）。
    """
    inter = gate.size(0)
    if inter % chunks != 0:
        raise ValueError(
            f"UPCYCLE_SPLIT_NOT_DIVISIBLE: intermediate={inter} chunks={chunks}"
        )
    width = inter // chunks
    return [
        (
            gate[i * width:(i + 1) * width].clone(),
            up[i * width:(i + 1) * width].clone(),
            down[:, i * width:(i + 1) * width].clone(),
        )
        for i in range(chunks)
    ]


def upcycle_to_moe(
    source: str | Path,
    out_dir: str | Path,
    *,
    num_experts: int = 8,
    top_k: int = 2,
    layer_interval: int = 2,
    expert_intermediate_size: int = 0,
    num_shared_experts: int = 0,
    shared_intermediate_size: int = 0,
    diversify_noise: float = 1e-3,
) -> dict:
    """把 dense checkpoint 複製為 MoE 初始權重並回傳摘要。

    ``expert_intermediate_size`` < dense intermediate → 細粒度專家：
    dense FFN 沿 intermediate 維切成等寬子專家（輸出加總與原 FFN 等價），
    各專家輪流取子塊再加 ``diversify_noise`` 高斯噪聲打破對稱。
    ``num_shared_experts`` > 0 → 常駐共享專家（同法取子塊初始化）。
    """
    loaded = load_checkpoint(source, map_location="cpu")
    dense_model = loaded["model"]
    config = loaded["config"]
    if config.use_moe:
        raise ValueError("UPCYCLE_SOURCE_ALREADY_MOE")

    dense_inter = int(config.intermediate_size)
    expert_inter = int(expert_intermediate_size or dense_inter)
    shared_inter = int(shared_intermediate_size or expert_inter)
    if dense_inter % expert_inter != 0 or dense_inter % shared_inter != 0:
        raise ValueError(
            "UPCYCLE_INTER_NOT_DIVISIBLE: "
            f"dense={dense_inter} expert={expert_inter} shared={shared_inter}"
        )
    expert_chunks = dense_inter // expert_inter
    shared_chunks = dense_inter // shared_inter

    config.use_moe = True
    config.moe_num_experts = int(num_experts)
    config.moe_top_k = int(top_k)
    config.moe_layer_interval = int(layer_interval)
    config.moe_num_shared_experts = int(num_shared_experts)
    config.moe_expert_intermediate_size = expert_inter
    config.moe_shared_intermediate_size = shared_inter
    # 訓練 VRAM 緩衝：activation checkpoint 只在 training 模式啟用。
    config.use_activation_checkpoint = True

    moe_model = XingChengForCausalLM(config)
    dst = moe_model.state_dict()
    copied_shared = 0
    copied_expert = 0
    moe_layers: list[int] = []
    dense_state = dense_model.state_dict()
    layer_keys = {
        int(match.group(1))
        for key in dense_state
        if (match := _MLP_KEY.match(key))
    }
    generator = torch.Generator().manual_seed(42)

    def _noised(t: torch.Tensor) -> torch.Tensor:
        if diversify_noise <= 0:
            return t
        noise = torch.randn(t.shape, generator=generator, dtype=t.dtype)
        return t + noise * (float(t.std()) * diversify_noise)

    for key, tensor in dense_state.items():
        match = _MLP_KEY.match(key)
        is_moe_layer = match and int(match.group(1)) in {
            i for i in layer_keys if i % max(1, layer_interval) == 0
        }
        if is_moe_layer and match.group(2) == "gate_proj":
            layer_idx = int(match.group(1))
            moe_layers.append(layer_idx)
            gate = tensor
            up = dense_state[
                key.replace("gate_proj", "up_proj")
            ]
            down = dense_state[
                key.replace("gate_proj", "down_proj")
            ]
            expert_parts = _split_dense_ffn(gate, up, down, expert_chunks)
            # 近似等價分配：shared 專家取前 shared_used 個子塊（權重 1.0 常駐），
            # 路由專家輪轉覆蓋其餘 rem 子塊並把 down_proj 放大 rem 倍 ——
            # top-k softmax 權重和為 1，期望輸出 ≈ 剩餘子塊之和，
            # shared + routed ≈ 原 dense FFN 輸出。
            shared_used = min(num_shared_experts, expert_chunks) \
                if num_shared_experts else 0
            rem = list(range(shared_used, expert_chunks)) or [0]
            rem_scale = float(len(rem))
            for expert_id in range(num_experts):
                part = expert_parts[rem[expert_id % len(rem)]]
                for proj_name, value in zip(
                    ("gate_proj", "up_proj", "down_proj"), part
                ):
                    if proj_name == "down_proj":
                        value = value * rem_scale
                    dst_key = (
                        f"model.layers.{layer_idx}.mlp.experts."
                        f"{expert_id}.{proj_name}.weight"
                    )
                    dst[dst_key].copy_(_noised(value))
                    copied_expert += 1
            shared_parts = _split_dense_ffn(gate, up, down, shared_chunks)
            for shared_id in range(num_shared_experts):
                part = shared_parts[shared_id % shared_chunks]
                for proj_name, value in zip(
                    ("gate_proj", "up_proj", "down_proj"), part
                ):
                    dst_key = (
                        f"model.layers.{layer_idx}.mlp.shared_experts."
                        f"{shared_id}.{proj_name}.weight"
                    )
                    dst[dst_key].copy_(_noised(value))
                    copied_expert += 1
        elif match:
            if is_moe_layer:
                # MoE 層的 up/down_proj 已在 gate_proj 分支整體處理，
                # 此處略過（MoE 層無 mlp.{up,down}_proj 目標權重）。
                continue
            dst[key].copy_(tensor)
            copied_shared += 1
        else:
            dst[key].copy_(tensor)
            copied_shared += 1
    moe_model.load_state_dict(dst, strict=True)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    init_path = out / "init.pt"
    tokenizer = loaded.get("tokenizer")
    saved = save_checkpoint(
        init_path,
        moe_model,
        tokenizer=tokenizer,
        config=config,
        metadata={
            "upcycled_from": str(source),
            "upcycle": "dense-ffn-to-moe-experts",
            "moe_layers": moe_layers,
            "router_init": "default-normal-near-uniform",
            "expert_intermediate_size": expert_inter,
            "shared_intermediate_size": shared_inter,
            "num_shared_experts": num_shared_experts,
        },
    )
    tokenizer_path = out / "tokenizer.json"
    if tokenizer is not None:
        tokenizer_path.write_text(
            str(tokenizer.state_dict()["tokenizer_json"]), encoding="utf-8"
        )
    return {
        "init_checkpoint": str(init_path),
        "tokenizer_dir": str(out),
        "moe_layers": moe_layers,
        "num_experts": num_experts,
        "num_shared_experts": num_shared_experts,
        "top_k": top_k,
        "layer_interval": layer_interval,
        "expert_intermediate_size": expert_inter,
        "shared_intermediate_size": shared_inter,
        "copied_shared_tensors": copied_shared,
        "copied_expert_tensors": copied_expert,
        "total_params": moe_model.num_parameters(only_trainable=False),
        "file_sha256": saved["file_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dense → MoE sparse upcycling")
    parser.add_argument("--source", required=True, help="dense checkpoint .pt")
    parser.add_argument("--output", required=True, help="輸出目錄")
    parser.add_argument("--experts", type=int, default=8)
    parser.add_argument("--top-k", type=int, default=2)
    parser.add_argument("--layer-interval", type=int, default=2)
    parser.add_argument(
        "--expert-inter", type=int, default=0,
        help="細粒度專家 intermediate（0=dense 全尺寸；須整除 dense intermediate）",
    )
    parser.add_argument(
        "--shared-experts", type=int, default=0,
        help="DeepSeek-MoE 常駐共享專家數",
    )
    parser.add_argument(
        "--shared-inter", type=int, default=0,
        help="共享專家 intermediate（0=同 expert-inter）",
    )
    parser.add_argument("--diversify-noise", type=float, default=1e-3)
    args = parser.parse_args(argv)
    summary = upcycle_to_moe(
        args.source,
        args.output,
        num_experts=args.experts,
        top_k=args.top_k,
        layer_interval=args.layer_interval,
        expert_intermediate_size=args.expert_inter,
        num_shared_experts=args.shared_experts,
        shared_intermediate_size=args.shared_inter,
        diversify_noise=args.diversify_noise,
    )
    print(json.dumps({"event": "done", **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["upcycle_to_moe"]
