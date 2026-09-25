"""Mixture-of-Experts 層：Router + Experts + 輔助負載均衡 loss。

對應技術棧：PyTorch Tensor ops + Autograd，Router 為 nn.Linear，Experts 為 SwiGLU MLP。
設計遵循 Switch Transformer / GShard 的 token-choice 路由與 aux loss。
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..config import XingChengConfig
from .mlp import XingChengMLP


class XingChengMoE(nn.Module):
    """MoE 前饋層：每個 token 由 router 選 top-k 個專家加權組合。"""

    def __init__(self, config: XingChengConfig) -> None:
        super().__init__()
        self.config = config
        self.num_experts = int(config.moe_num_experts)
        self.top_k = int(config.moe_top_k)
        self.hidden_size = config.hidden_size
        # Router：hidden → num_experts
        self.router = nn.Linear(config.hidden_size, self.num_experts, bias=False)
        nn.init.normal_(self.router.weight, mean=0.0, std=config.initializer_range)
        # Experts：各自為獨立 MLP（SwiGLU 或標準）；細粒度專家用
        # moe_expert_intermediate_size（0 → 全尺寸）。
        expert_inter = int(
            config.moe_expert_intermediate_size or config.intermediate_size
        )
        self.experts = nn.ModuleList(
            [
                XingChengMLP(config, intermediate_size=expert_inter)
                for _ in range(self.num_experts)
            ]
        )
        # 共享專家（DeepSeek-MoE）：常駐啟用、權重 1.0，捕捉通用知識，
        # 讓路由專家承擔細粒度專精。
        self.num_shared = int(config.moe_num_shared_experts)
        shared_inter = int(
            config.moe_shared_intermediate_size or expert_inter
        )
        self.shared_experts = nn.ModuleList(
            [
                XingChengMLP(config, intermediate_size=shared_inter)
                for _ in range(self.num_shared)
            ]
        )

    def forward(self, hidden_states: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """回傳 (output, aux_loss)。

        hidden_states: (B, S, hidden) → output 同形状，aux_loss 為純量。
        同步更新負載指標供外部讀取（expert_load / router_entropy / collapse）。
        """
        b, s, h = hidden_states.shape
        x = hidden_states.view(-1, h)  # (N, hidden)  N=B*S
        n_tokens = x.size(0)

        # Router logits & probs（FP32 保持精確，見 quantizer 排除 router）
        router_logits = F.linear(x.float(), self.router.weight.float())  # (N, E)
        self._last_router_dtype = str(router_logits.dtype)
        # 訓練時加入微小噪聲促進探索，推論時確定性
        if self.training and self.config.moe_aux_loss_weight > 0:
            # 0.01 * N(0,1) 噪聲，方差極小，不影響主路由但打破對稱
            router_logits = router_logits + 0.01 * torch.randn_like(router_logits)
        router_probs = F.softmax(router_logits, dim=-1)  # (N, E)

        # Top-k 選擇（確定性：相同輸入→相同 topk，因無隨機且已排序）
        top_weights, top_indices = torch.topk(router_probs, self.top_k, dim=-1)  # (N, k)
        # Renormalize 在 top-k 內
        top_weights = top_weights / top_weights.sum(dim=-1, keepdim=True)
        top_weights = top_weights.to(x.dtype)

        # 依專家分組計算
        output = torch.zeros_like(x)
        for expert_id in range(self.num_experts):
            expert_mask = (top_indices == expert_id)  # (N, k)
            token_mask = expert_mask.any(dim=-1)  # (N,)
            if not token_mask.any():
                continue
            positions = torch.where(token_mask)[0]  # (M,)
            selected_weights = torch.zeros(positions.size(0), device=x.device, dtype=x.dtype)
            for k in range(self.top_k):
                k_mask = expert_mask[positions, k]
                if k_mask.any():
                    idx = positions[k_mask]
                    selected_weights[k_mask] = top_weights[idx, k]
            expert_input = x[positions]  # (M, hidden)
            expert_out = self.experts[expert_id](expert_input)  # (M, hidden)
            output[positions] += expert_out * selected_weights.unsqueeze(-1)

        # 共享專家：所有 token 常駐啟用（權重 1.0）。
        for shared in self.shared_experts:
            output = output + shared(x)

        output = output.view(b, s, h)

        # ── 指標計算（不影響梯度，僅供監控）───────────────────────
        with torch.no_grad():
            flat_indices = top_indices.view(-1)  # (N*k,)
            fraction = torch.bincount(flat_indices, minlength=self.num_experts).to(router_probs.dtype) / (n_tokens * self.top_k)
            # Expert Load: 每專家 token 佔比
            self._last_expert_load = fraction.detach().cpu()
            # Router Entropy: -sum(p log p) 均值，理想 ~log(E)（均勻） vs 0（坍塌）
            entropy = -(router_probs * (router_probs.clamp_min(1e-9).log())).sum(dim=-1).mean()
            self._last_router_entropy = float(entropy.item())
            # Expert Utilization: 比例 >1% 視為被使用
            self._last_utilized = int((fraction > 0.01).sum().item())
            # Collapse 檢測：任一專家 >50% 或 <1% 且連續多步
            self._last_collapse = bool((fraction > 0.5).any() or (fraction < 0.01).any())
            # 輔助 z-loss：鼓勵 logits 小而穩定（ST-MoE）
            z_loss = torch.logsumexp(router_logits, dim=-1).pow(2).mean()
            self._last_z_loss = float(z_loss.item())
        # 輔助 load-balancing loss：Switch Transformer 形式
        mean_prob = router_probs.mean(dim=0)  # (E,)
        aux_loss = self.num_experts * torch.sum(mean_prob * fraction)  # 理想 1.0
        # 合併 z-loss（權重 0.001，極小，僅穩定）
        aux_loss = aux_loss + 0.001 * z_loss
        self._last_aux_loss = float(aux_loss.item())
        return output, aux_loss

    # ── 指標讀取（正確性：外部可驗證所有 Expert 是否參與）─────────
    def expert_load(self) -> torch.Tensor | None:
        return getattr(self, "_last_expert_load", None)

    def router_entropy(self) -> float | None:
        return getattr(self, "_last_router_entropy", None)

    def is_collapsed(self) -> bool | None:
        return getattr(self, "_last_collapse", None)

    def utilization(self) -> int | None:
        return getattr(self, "_last_utilized", None)

    def metrics(self) -> dict[str, float | int | str | list[float]]:
        load = self.expert_load()
        return {
            "expert_load": load.tolist() if load is not None else [],
            "router_dtype": getattr(self, "_last_router_dtype", ""),
            "router_entropy": self.router_entropy() or 0.0,
            "aux_loss": getattr(self, "_last_aux_loss", 0.0),
            "z_loss": getattr(self, "_last_z_loss", 0.0),
            "utilized_experts": self.utilization() or 0,
            "is_collapsed": self.is_collapsed() or False,
            "num_experts": self.num_experts,
            "num_shared_experts": self.num_shared,
            "top_k": self.top_k,
        }


__all__ = ["XingChengMoE"]
