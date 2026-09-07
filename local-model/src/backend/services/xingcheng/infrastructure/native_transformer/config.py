"""星澄原生模型設定。

`XingChengConfig` 集中所有模型超參數，並提供常用預設（small / base / large）。
所有模組都從這份設定讀取維度，確保整個模型可由單一資料描述。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Mapping


@dataclass
class XingChengConfig:
    """星澄 Transformer 解碼器設定。"""

    # ── 詞表與序列 ──────────────────────────────────────────────
    vocab_size: int = 32_000
    max_position_embeddings: int = 4_096
    bos_token_id: int = 1
    eos_token_id: int = 2
    pad_token_id: int = 0

    # ── 模型維度 ────────────────────────────────────────────────
    hidden_size: int = 512
    intermediate_size: int = 1_376          # FFN 內部維度（通常 ~2.7 × hidden）
    num_hidden_layers: int = 6
    num_attention_heads: int = 8
    num_key_value_heads: int = 8            # GQA / MQA：=num_attention_heads 為 MHA
    head_dim: int = 0                       # 0 → 自動 hidden_size // num_attention_heads

    # ── 正規化與激活 ────────────────────────────────────────────
    rms_norm_eps: float = 1e-6
    layer_norm_eps: float = 1e-5
    norm_type: str = "rmsnorm"              # "rmsnorm" | "layernorm"
    hidden_act: str = "silu"                # MLP 激活
    use_swiglu: bool = True                 # SwiGLU FFN

    # ── 位置編碼 ────────────────────────────────────────────────
    position_embedding_type: str = "rope"   # "rope" | "learned" | "alibi" | "none"
    rope_theta: float = 10_000.0
    rope_scaling: Mapping[str, Any] | None = None

    # ── Attention ───────────────────────────────────────────────
    attention_dropout: float = 0.0
    use_flash_attention: bool = True        # 透過 F.scaled_dot_product_attention
    attention_bias: bool = False            # Q/K/V projection bias

    # ── 殘差與 embedding ────────────────────────────────────────
    embed_dropout: float = 0.0
    tie_word_embeddings: bool = True        # LM Head 與 token embedding 共享權重

    # ── 初始化 ──────────────────────────────────────────────────
    initializer_range: float = 0.02

    # ── 推論 ────────────────────────────────────────────────────
    max_new_tokens: int = 512

    # ── 量化 ────────────────────────────────────────────────────
    quantization: str = "none"              # "none" | "int8" | "int4" | "fp8"

    # ── 執行後端 ────────────────────────────────────────────────
    # 由 runtime/backend.py 解析；此處僅記錄偏好。
    backend_preference: tuple[str, ...] = (
        "cublaslt",        # GEMM / Linear / MLP
        "cudnn",           # 卷積 / 部分 reduction
        "flashattention",  # Attention IO-aware kernel
        "triton",          # 自研 kernel (RMSNorm / RoPE / SwiGLU / KV Cache / Quant)
        "gluon",           # 更細緻 GPU 控制（Triton 不足時）
        "cuda",            # 極端瓶頸下沉
    )

    def __post_init__(self) -> None:
        if self.head_dim <= 0:
            object.__setattr__(self, "head_dim", self.hidden_size // self.num_attention_heads)
        if self.num_key_value_heads <= 0:
            object.__setattr__(self, "num_key_value_heads", self.num_attention_heads)
        if self.hidden_size % self.num_attention_heads != 0:
            raise ValueError(
                f"hidden_size={self.hidden_size} 必須能被 num_attention_heads="
                f"{self.num_attention_heads} 整除"
            )

    # ── 便利方法 ────────────────────────────────────────────────
    @property
    def is_gqa(self) -> bool:
        return self.num_key_value_heads != self.num_attention_heads

    @property
    def kv_dim(self) -> int:
        return self.num_key_value_heads * self.head_dim

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.rope_scaling is None:
            data["rope_scaling"] = None
        return data

    # ── 預設 ────────────────────────────────────────────────────
    @classmethod
    def small(cls) -> "XingChengConfig":
        """~25M 參數等級，適合 CPU 與單元測試。"""
        return cls(
            vocab_size=8_192,
            hidden_size=256,
            intermediate_size=704,
            num_hidden_layers=4,
            num_attention_heads=8,
            num_key_value_heads=4,
            max_position_embeddings=1_024,
        )

    @classmethod
    def base(cls) -> "XingChengConfig":
        """~120M 參數等級，入門 GPU / 邊緣裝置。"""
        return cls(
            vocab_size=32_000,
            hidden_size=768,
            intermediate_size=2_048,
            num_hidden_layers=12,
            num_attention_heads=12,
            num_key_value_heads=4,
            max_position_embeddings=2_048,
        )

    @classmethod
    def large(cls) -> "XingChengConfig":
        """~1B 參數等級，主力本地訓練 / 推理。"""
        return cls(
            vocab_size=64_000,
            hidden_size=2_048,
            intermediate_size=5_504,
            num_hidden_layers=24,
            num_attention_heads=16,
            num_key_value_heads=4,
            head_dim=128,
            max_position_embeddings=8_192,
            rope_theta=500_000.0,
        )
