import sys

sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")
sys.path.insert(
    0, r"E:\GPTBridge\Standalone tools\local-model\src\backend\services"
)

from xingcheng.infrastructure.native_transformer.lifecycle import (
    ModelLifecycle,
)

LIFECYCLE_DIR = (
    r"Standalone tools\local-model\xingcheng\runtime\models\lifecycle"
    r"\xingcheng-native"
)
CKPT = (
    r"Standalone tools\local-model\xingcheng\runtime\models\jobs"
    r"\sft-294m-moe16s1-v19-20260925\final.pt"
)

lc = ModelLifecycle.load_or_create(LIFECYCLE_DIR, "xingcheng-native")
entry = lc.register_artifact(
    "weights",
    CKPT,
    metadata={
        "architecture": "fine-grained-moe+shared (DeepSeek-MoE style)",
        "total_params": 294218496,
        "active_params_approx": 118000000,
        "num_experts": 16,
        "num_shared_experts": 1,
        "top_k": 4,
        "moe_layer_interval": 2,
        "moe_layers": [0, 2, 4, 6, 8, 10],
        "expert_intermediate_size": 1024,
        "shared_intermediate_size": 1024,
        "upcycle_source": "sft-88m-v19b-qa-20260924/latest.pt",
        "sft_job": "sft-294m-moe16s1-v19-20260925",
        "sft_steps": 700,
        "eval_perplexity": 1.0834,
        "parity_init": {
            "max_logit_diff": 0.0196,
            "argmax_agreement": 1.0,
        },
        "router_health": {
            "entropy_mean": 2.64,
            "entropy_max": 2.773,
            "utilized_mean": 15.5,
            "num_routed_experts": 16,
            "max_expert_load": 0.19,
        },
        "probes": "probes: factual-qa/identity/poem/weather-honest/"
        "advice/greeting ok; arithmetic & holdings-level investment "
        "handled by service-layer shell experts",
    },
    activate=True,
)
lc.save(LIFECYCLE_DIR)
print("registered version", entry["version"], entry["sha256"][:16])
print("active", lc.active_weights_version)
