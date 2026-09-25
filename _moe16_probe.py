import json
import os
import sys

os.environ["XINGCHENG_NATIVE_ENGINE"] = "1"
os.environ["XINGCHENG_CPP_RUNTIME"] = "off"
os.environ["XINGCHENG_NATIVE_CHECKPOINT"] = (
    r"Standalone tools\local-model\xingcheng\runtime\models\jobs"
    r"\sft-294m-moe16s1-v19-20260925\final.pt"
)
sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")
sys.path.insert(
    0, r"E:\GPTBridge\Standalone tools\local-model\src\backend\services"
)

from xingcheng.infrastructure.native_engine import generate_via_native_engine

PROMPTS = [
    "什麼是光合作用？",
    "請自我介紹。",
    "幫我寫一首關於秋天的短詩。",
    "今天天氣如何？",
    "分析我的投資組合風險。",
    "給我三個學英文的建議。",
    "1+1等於多少？",
    "你好",
]

results = []
for prompt in PROMPTS:
    res = generate_via_native_engine(
        {
            "prompt": prompt,
            "dialogue_interactive": True,
            "max_tokens": 96,
            "temperature": 0.7,
            "top_p": 0.9,
            "seed": 42,
        }
    )
    results.append(
        {
            "prompt": prompt,
            "ok": res.get("ok"),
            "error": res.get("error_code"),
            "reply": (res.get("text") or res.get("reply") or "")[:400],
            "degraded": res.get("degraded"),
        }
    )

with open("_moe16_probe.json", "w", encoding="utf-8") as fh:
    json.dump(results, fh, ensure_ascii=False, indent=1)
print("done", sum(1 for r in results if r["ok"]))
