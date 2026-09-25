"""Expert Specialization Evaluation — ``star-expert-specialization/v1``。

回答「16 個專家都有被使用」之外的問題：**專家是否學會了語義分工**。

方法：對各領域（math/zh-TW/code/reading）的探針組做 forward，
收集每個 MoE 層的 ``expert_load``（專家流量分佈）與
``shared_rms``/``routed_rms``（共享/路由專家輸出範數比），計算：

- **per-layer 專家分佈**：domain × expert 流量矩陣
- **跨域 Jensen–Shannon divergence**：分佈差異 >0 才有分工；
  全域分佈相同（JS≈0）代表「均衡使用但無專精」
- **top-m 專家重疊**：各域最愛用專家的交集
- **集中度**：各域分佈的 entropy——專精化 → 熵低於 ln(E)
- **shared expert 貢獻比**：``shared_rms/(shared_rms+routed_rms)``，
  跨域是否一致（一致性高 → 承擔通用語言能力的證據）

報告不判定好壞，只產生觀測值與初步結論旗標；閘門語意屬
``capability_eval`` 的 ``expert_routing`` 類別。

    python -m xingcheng.infrastructure.native_transformer.expert_specialization \
        --checkpoint <final.pt> [--suite <suite.json>] \
        [--device cpu] [--save]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import torch

from .checkpoint import load_checkpoint

REPORT_FORMAT = "star-expert-specialization/v1"

# 預設域探針：每域 4 題、長度足以讓利用率指標有意義。
DEFAULT_DOMAINS: dict[str, list[str]] = {
    "math": [
        "請計算：17 + 25、38 + 57、64 - 29、81 ÷ 9、7 × 8，"
        "每題寫出位值分解過程與最終答案。",
        "小明有 145 元，買了 3 本各 28 元的筆記本，還剩多少錢？"
        "請逐步計算。",
        "計算下列各題並只輸出最終數字：23 + 45、90 - 37、12 × 4、"
        "56 ÷ 7、88 + 38、100 - 55。",
        "一個長方形長 12 公分寬 7 公分，面積是多少？周長是多少？"
        "請寫出計算過程。",
    ],
    "zh-TW": [
        "請用繁體中文詳細說明什麼是光合作用、它為什麼重要、"
        "過程中需要哪些物質、產生什麼產物，以及哪些生物會進行"
        "光合作用，並舉例說明它對地球生態系統的影響。",
        "請解釋台灣的四季氣候特徵，包含春夏秋冬各季節的溫度、"
        "降雨特性以及適合的活動建議。",
        "什麼是機器學習？它和傳統程式設計有什麼不同？"
        "請舉三個日常生活中的應用例子說明。",
        "請介紹繁體中文和簡體中文的主要差異，包括字形、用詞"
        "和使用地區的不同。",
    ],
    "code": [
        "def quicksort(arr):\n    if len(arr) <= 1:\n        return arr\n"
        "    pivot = arr[0]\n    left = [x for x in arr[1:] if x < pivot]\n"
        "    right = [x for x in arr[1:] if x >= pivot]\n"
        "    return quicksort(left) + [pivot] + quicksort(right)\n\n"
        "# 請解釋這段程式碼的時間複雜度",
        "def fibonacci(n):\n    memo = {}\n    def helper(k):\n"
        "        if k in memo:\n            return memo[k]\n"
        "        if k <= 1:\n            return k\n"
        "        memo[k] = helper(k - 1) + helper(k - 2)\n"
        "        return memo[k]\n    return helper(n)\n\n"
        "# 這段程式碼使用了什麼技巧？",
        "class Stack:\n    def __init__(self):\n        self.items = []\n"
        "    def push(self, item):\n        self.items.append(item)\n"
        "    def pop(self):\n        return self.items.pop()\n"
        "    def is_empty(self):\n        return len(self.items) == 0\n\n"
        "# 用這個 Stack 實作括號配對檢查",
        "import json\n\ndef load_config(path):\n"
        "    with open(path) as f:\n        return json.load(f)\n\n"
        "def validate(config, required):\n"
        "    return all(k in config for k in required)\n\n"
        "# 說明這兩個函式的用途與潛在錯誤",
    ],
    "reading": [
        "台灣的半導體產業在全球供應鏈中扮演關鍵角色，從晶圓代工、"
        "封裝測試到 IC 設計都有完整布局。台積電作為龍頭企業，其先進"
        "製程技術領先全球，客戶涵蓋蘋果、輝達等國際大廠。請摘要這段"
        "文字的重點並說明產業鏈上下游關係。",
        "根據研究，每天至少運動 30 分鐘可以顯著降低心血管疾病風險，"
        "改善睡眠品質，並提升整體心理健康。研究追蹤了超過一萬名受試者"
        "長達五年。請回答：這段文字的主旨是什麼？研究規模多大？",
        "太陽系有八大行星，依序為水星、金星、地球、火星、木星、"
        "土星、天王星和海王星。木星是最大的行星，水星是最小的行星。"
        "請問哪個行星最大？哪個離太陽最近？",
        "維他命 D 主要透過陽光照射皮膚合成，也可以從鮭魚、蛋黃和"
        "強化牛奶等食物攝取。缺乏維他命 D 可能導致骨骼軟化和免疫力"
        "下降。請列出文中提到的維他命 D 來源。",
    ],
}


def _moe_layers(model: Any) -> list[tuple[int, Any]]:
    backbone = getattr(model, "model", model)
    return [
        (i, layer.mlp)
        for i, layer in enumerate(getattr(backbone, "layers", []))
        if getattr(layer, "is_moe", False)
        and hasattr(getattr(layer, "mlp", None), "metrics")
    ]


def _capture(
    model: Any, tokenizer: Any, device: torch.device, prompt: str, max_len: int = 256
) -> list[dict[str, Any]]:
    """一次 forward，回傳各 MoE 層的 expert_load/shared/routed 指標。"""
    ids = list(tokenizer.encode(prompt, add_bos=True, add_eos=False))[:max_len]
    if not ids:
        return []
    with torch.no_grad():
        model(torch.tensor([ids], dtype=torch.long, device=device))
    out = []
    for layer_idx, mlp in _moe_layers(model):
        metrics = mlp.metrics()
        out.append(
            {
                "layer": layer_idx,
                "expert_load": [float(v) for v in metrics.get("expert_load") or []],
                "shared_rms": float(metrics.get("shared_rms") or 0.0),
                "routed_rms": float(metrics.get("routed_rms") or 0.0),
                "router_entropy": float(metrics.get("router_entropy") or 0.0),
                "utilized_experts": int(metrics.get("utilized_experts") or 0),
            }
        )
    return out


def _js_divergence(p: list[float], q: list[float]) -> float:
    """Jensen–Shannon divergence（nats，0=完全相同）。"""
    m = [(a + b) / 2 for a, b in zip(p, q)]
    def kl(x: list[float], y: list[float]) -> float:
        return sum(
            a * math.log(a / b) for a, b in zip(x, y) if a > 0 and b > 0
        )
    return (kl(p, m) + kl(q, m)) / 2


def evaluate(
    checkpoint_path: str | Path,
    *,
    domains: Mapping[str, list[str]] | None = None,
    device: str = "cpu",
    top_m: int = 4,
) -> dict[str, Any]:
    bundle = load_checkpoint(checkpoint_path, map_location=device)
    model, tokenizer = bundle["model"], bundle["tokenizer"]
    model.to(device).eval()
    dev = torch.device(device)
    domain_prompts = dict(domains or DEFAULT_DOMAINS)
    moe = _moe_layers(model)
    if not moe:
        return {
            "format_version": REPORT_FORMAT,
            "ok": False,
            "error": "no-moe-layers",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    layers = [i for i, _ in moe]
    num_experts = int(moe[0][1].num_experts)

    # ── 逐域逐層聚合 expert_load ────────────────────────────────
    per_domain: dict[str, dict[int, dict[str, Any]]] = {}
    for domain, prompts in domain_prompts.items():
        layer_acc: dict[int, dict[str, Any]] = {}
        for prompt in prompts:
            for capture in _capture(model, tokenizer, dev, prompt):
                li = capture["layer"]
                acc = layer_acc.setdefault(
                    li,
                    {
                        "load_sum": [0.0] * num_experts,
                        "shared_rms": [],
                        "routed_rms": [],
                        "entropy": [],
                        "utilized": [],
                    },
                )
                for e, v in enumerate(capture["expert_load"]):
                    acc["load_sum"][e] += v
                acc["shared_rms"].append(capture["shared_rms"])
                acc["routed_rms"].append(capture["routed_rms"])
                acc["entropy"].append(capture["router_entropy"])
                acc["utilized"].append(capture["utilized_experts"])
        layer_stats = {}
        n = max(1, len(prompts))
        for li, acc in layer_acc.items():
            mean_load = [v / n for v in acc["load_sum"]]
            total = sum(mean_load) or 1.0
            dist = [v / total for v in mean_load]
            dist_entropy = -sum(
                v * math.log(v) for v in dist if v > 0
            )
            top = sorted(range(len(dist)), key=lambda e: -dist[e])[:top_m]
            s_rms = sum(acc["shared_rms"]) / len(acc["shared_rms"])
            r_rms = sum(acc["routed_rms"]) / len(acc["routed_rms"])
            layer_stats[li] = {
                "expert_distribution": [round(v, 5) for v in dist],
                "distribution_entropy": round(dist_entropy, 4),
                "top_experts": top,
                "mean_router_entropy": round(
                    sum(acc["entropy"]) / len(acc["entropy"]), 4
                ),
                "mean_utilized": round(
                    sum(acc["utilized"]) / len(acc["utilized"]), 2
                ),
                "shared_rms": round(s_rms, 4),
                "routed_rms": round(r_rms, 4),
                "shared_share": round(
                    s_rms / (s_rms + r_rms), 4
                ) if s_rms + r_rms > 0 else None,
            }
        per_domain[domain] = layer_stats

    # ── 跨域比較：per-layer JS divergence + top-m 重疊 ──────────
    domain_names = list(domain_prompts)
    cross: dict[str, Any] = {}
    for li in layers:
        js_pairs = {}
        overlaps = {}
        for i, d1 in enumerate(domain_names):
            for d2 in domain_names[i + 1:]:
                p = per_domain[d1][li]["expert_distribution"]
                q = per_domain[d2][li]["expert_distribution"]
                js_pairs[f"{d1}|{d2}"] = round(_js_divergence(p, q), 4)
                t1 = set(per_domain[d1][li]["top_experts"])
                t2 = set(per_domain[d2][li]["top_experts"])
                overlaps[f"{d1}|{d2}"] = len(t1 & t2)
        mean_js = (
            sum(js_pairs.values()) / len(js_pairs) if js_pairs else 0.0
        )
        cross[li] = {
            "js_divergence": js_pairs,
            "mean_js": round(mean_js, 4),
            "top_overlap": overlaps,
        }

    # ── shared expert 跨域一致性 ────────────────────────────────
    shared_analysis = {}
    for li in layers:
        shares = {
            d: per_domain[d][li]["shared_share"]
            for d in domain_names
            if per_domain[d][li]["shared_share"] is not None
        }
        if shares:
            vals = list(shares.values())
            shared_analysis[li] = {
                "shared_share_by_domain": {k: round(v, 4) for k, v in shares.items()},
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "spread": round(max(vals) - min(vals), 4),
                "interpretation": (
                    "consistent" if max(vals) - min(vals) < 0.1 else "varies"
                ),
            }

    # ── 初步結論旗標（觀測值，不是通過/失敗）─────────────────────
    all_js = [c["mean_js"] for c in cross.values()]
    overall_js = sum(all_js) / len(all_js) if all_js else 0.0
    findings = {
        "mean_cross_domain_js": round(overall_js, 4),
        "specialization_signal": (
            "weak" if overall_js < 0.02
            else "moderate" if overall_js < 0.08 else "strong"
        ),
        "note": (
            "JS≈0 表示各域路由分佈相同：利用均衡但無語義分工；"
            "JS 越高代表專家分佈越專精。"
        ),
    }

    param = next(model.parameters(), None)
    return {
        "format_version": REPORT_FORMAT,
        "ok": True,
        "model_version": str(bundle.get("state_sha256") or "")[:16],
        "checkpoint_sha256": bundle.get("state_sha256"),
        "num_experts": num_experts,
        "moe_layers": layers,
        "top_m": top_m,
        "device": f"{device}/{'bf16' if param is not None and param.dtype == torch.bfloat16 else 'fp32'}",
        "domains": {
            d: {"prompts": len(prompts), "layers": per_domain[d]}
            for d, prompts in domain_prompts.items()
        },
        "cross_domain": {str(k): v for k, v in cross.items()},
        "shared_expert": shared_analysis,
        "findings": findings,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--suite", default=None, help="自訂 domains JSON")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--top-m", type=int, default=4)
    ap.add_argument("--tool-root", default=".")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    domains = None
    if args.suite:
        domains = json.loads(Path(args.suite).read_text(encoding="utf-8"))
    report = evaluate(
        args.checkpoint, domains=domains, device=args.device,
        top_m=args.top_m,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.save and report.get("ok"):
        out_dir = Path(args.tool_root) / "xingcheng" / "runtime" / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        sha = hashlib.sha256(
            json.dumps(report, sort_keys=True).encode("utf-8")
        ).hexdigest()[:8]
        path = out_dir / f"expert-specialization-{ts}-{sha}.json"
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"saved: {path.name}")


if __name__ == "__main__":
    main()
