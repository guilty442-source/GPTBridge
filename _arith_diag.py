import json
import os
import re
import sys

sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")
sys.path.insert(
    0, r"E:\GPTBridge\Standalone tools\local-model\src\backend\services"
)

os.environ["XINGCHENG_CPP_CUDA"] = "0"

from xingcheng.infrastructure.native_transformer.cpp_runtime import (
    ensure_bundle,
    load_extension,
)

xc = load_extension()
from xingcheng.infrastructure.native_transformer.chat_format import (
    ChatMessage,
    render_conversation,
)

SYSTEM = "你是星澄，一個本地模型。簡潔回答。"

PROBES = [
    ("1+1等於多少？", "2"),
    ("2+3等於多少？", "5"),
    ("5+7等於多少？", "12"),
    ("9+6等於多少？", "15"),
    ("8-3等於多少？", "5"),
    ("10-4等於多少？", "6"),
    ("3×4等於多少？", "12"),
    ("6×7等於多少？", "42"),
    ("5×8等於多少？", "40"),
    ("12÷3等於多少？", "4"),
    ("56÷8等於多少？", "7"),
    ("15+27等於多少？", "42"),
    ("44+14等於多少？", "58"),
    ("100-37等於多少？", "63"),
    ("計算 14 + 82，只輸出數字。", "96"),
    ("計算 25 + 30，只輸出數字。", "55"),
    ("25 + 37 = ?", "62"),
    ("88 + 38 = ?", "126"),
    ("12 × 3 = ?", "36"),
    ("72 + 19 = ?", "91"),
    ("200 - 58 = ?", "142"),
    ("7 × 8 = ?", "56"),
    ("45 ÷ 9 = ?", "5"),
    ("33 + 66 = ?", "99"),
]

MODELS = {
    "v19b-dense(≈v25 base)": r"Standalone tools\local-model\xingcheng"
    r"\runtime\models\jobs\sft-88m-v19b-qa-20260924\latest.pt",
    "v26": r"Standalone tools\local-model\xingcheng\runtime\models\jobs"
    r"\sft-294m-moe16s1-v19-20260925\final.pt",
}


def rendered(prompt: str) -> str:
    return render_conversation(
        [ChatMessage("system", SYSTEM), ChatMessage("user", prompt)],
        add_generation_prompt=True,
    )


def extract_number(text: str) -> str:
    # 引擎未在 <|eot|> 停止時的續寫屬雜訊——只取首行答案。
    first = re.split(r"<\|eot\|>|\n", text, maxsplit=1)[0]
    m = re.findall(r"-?\d+", first)
    return m[-1] if m else ""


report = {}
for name, ckpt in MODELS.items():
    bundle = ensure_bundle(ckpt)
    engine = xc.NativeInferenceEngine()
    engine.load(bundle["output_dir"])
    sampling = xc.SamplingConfig()
    sampling.do_sample = False
    rows = []
    for prompt, expected in PROBES:
        ids = engine.encode(rendered(prompt))
        out_ids = engine.generate(ids, 24, sampling)
        text = engine.decode(list(out_ids))
        got = extract_number(text)
        rows.append(
            {
                "prompt": prompt,
                "expected": expected,
                "got": got,
                "raw": text,
                "ok": got == expected,
            }
        )
    engine.unload()
    acc = sum(r["ok"] for r in rows) / len(rows)
    report[name] = {"accuracy": round(acc, 3), "rows": rows}
    print(name, "accuracy", round(acc, 3))

json.dump(
    report, open("_arith_report.json", "w", encoding="utf-8"),
    ensure_ascii=False, indent=1,
)
