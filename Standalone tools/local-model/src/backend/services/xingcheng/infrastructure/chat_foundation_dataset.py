"""``star-chat-foundation/v1``：對話基礎 SFT 資料集產線（Phase 5）。

產出混合 replay 資料集：
- chat 記錄（star-chat-format messages）：招呼／回合邊界、逐字複製
  （句子、英文、數字、代號等**多樣內容類型**——v4 教訓：單一「代號」
  模式主導會讓模型學會「回覆一個代號」而非「從上下文複製」）、
  限定回答、多輪記憶、算術、tool_call、一般短答。
- general replay：corpus 文本切塊，prompt=短前綴（被 mask）、
  completion=後續——保住一般語言能力，防止災難性遺忘（v3/v4 教訓）。

預設比例：replay 字元量 ≈ chat token 量的 3 倍（120k 字元）。
所有產出皆 deterministic（seeded）；probe 值（星火測試／QZ-88 等）一律
排除，避免訓練集污染評測。

用法：
    python -m xingcheng.infrastructure.chat_foundation_dataset \
        --corpus xingcheng/runtime/corpus-v1/train.jsonl \
        --out xingcheng/runtime/sft/chat-foundation-v7-mixed.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any, Iterable, Sequence

DATASET_FORMAT_VERSION = "star-chat-foundation/v1"

# maturity.py L5/L6 探針用到的值——絕對不得出現在訓練集
PROBE_VALUES = frozenset({"星火測試", "QZ-88", "13 + 29", "6 × 7"})

_SYSTEM = {"role": "system", "content": "你是星澄，一個本地模型。簡短回答。"}


def _convo(user: str, assistant: str, *, system: bool = True,
           extra_turns: Sequence[dict] | None = None) -> dict[str, Any]:
    msgs = ([_SYSTEM] if system else []) + [
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]
    if extra_turns:
        msgs.extend(extra_turns)
    return {"messages": msgs}


def _unique_echo_values(rng: random.Random, n: int) -> list[str]:
    """產生 n 個互不相同的複製目標。

    v8 教訓：固定小值池（數十個）時模型可以靠背誦值集合矇混——
    eval ppl 下降但 echo/memory 探針仍失敗。值空間 >> 樣本數時，
    記憶策略不可行，梯度壓力才會逼出「從上下文複製」的 induction 行為。
    """
    zh_chars = "雲海風星月山林河川光影夢想晨光暮色青石白露松濤竹影溪聲"
    en_words = ["ember", "quartz", "harbor", "falcon", "cipher", "meadow",
                "lantern", "vertex", "willow", "cobalt", "signal", "prism"]
    values: set[str] = set()
    while len(values) < n:
        kind = rng.randrange(6)
        if kind == 0:      # 隨機代號
            v = f"{rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}-{rng.randint(100, 9999)}"
        elif kind == 1:    # 中文詞組合
            v = "".join(rng.choice(zh_chars) for _ in range(rng.randint(2, 4)))
        elif kind == 2:    # 英文詞組合
            v = f"{rng.choice(en_words)} {rng.choice(en_words)}"
        elif kind == 3:    # 數字
            v = f"{rng.uniform(1, 9999):.{rng.randint(0, 4)}f}"
        elif kind == 4:    # 日期
            v = f"{rng.randint(2020, 2030)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        else:              # 混合短句
            v = f"{rng.choice(en_words)}{rng.randint(10, 999)}{rng.choice(zh_chars)}"
        if v not in PROBE_VALUES:
            values.add(v)
    return list(values)


def build_chat_records(rng: random.Random, *,
                       echo_scale: int = 0) -> list[dict[str, Any]]:
    """chat 記錄（多樣化複製內容；echo_scale>0 時以唯一值池擴充複製/記憶量）。"""
    records: list[dict[str, Any]] = []

    # 1) 逐字複製：多內容類型
    words = ["雲端漫步", "靜水深流", "破曉之光", "繁星點點", "資料庫",
             "人工智慧", "機會成本", "藍色的海", "七天", "transformer",
             "GPT", "token", "B-17", "xy-42", "2026-09-20", "3.14159",
             "no_reply", "ABC-123"]
    sents = ["今天天氣很好", "知識就是力量", "慢慢來比較快", "保持好奇",
             "step by step", "hello world", "測試一二三"]
    echo_tpl = ["請只輸出：{w}", "請重複：{w}", "只輸出「{w}」就好",
                "照原樣輸出：{w}", "把下面這段原樣打出來：{w}", "複製：{w}"]
    echo_values = words + sents
    if echo_scale:
        echo_values = echo_values + _unique_echo_values(rng, echo_scale)
    for w in echo_values:
        if w in PROBE_VALUES:
            continue
        records.append(_convo(rng.choice(echo_tpl).format(w=w), w))
        if rng.random() < 0.4:
            records.append(_convo(rng.choice(echo_tpl).format(w=w), w,
                                  system=False))

    # 2) 多輪記憶：值多樣化（代號、名字、顏色、地點、數字…）
    mem_vals = [
        f"代號 {rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
        f"{rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}-{rng.randint(10, 99)}"
        for _ in range(20)
    ] + ["名字 小林", "顏色 深藍", "地點 高雄", "數字 7351", "水果 芒果",
         "動物 海豚", "城市 台南", "編號 8842", "密語 月光", "日期 10月3日"]
    if echo_scale:
        mem_kinds = ["代號", "密語", "編號", "數字", "密碼", "序號"]
        mem_vals += [f"{rng.choice(mem_kinds)} {v}"
                     for v in _unique_echo_values(rng, echo_scale // 2)]
    memo_tpl = ["請記住：{v}", "記住這個{v}", "幫我記住{v}", "{v}，記住它"]
    recall_tpl = ["我剛才請你記住的是什麼？", "剛才的{vname}是什麼？",
                  "你記住了什麼？", "請告訴我我給你的值"]
    for v in mem_vals:
        vname, val = v.split(None, 1)
        if val in PROBE_VALUES:
            continue
        records.append({"messages": [
            _SYSTEM,
            {"role": "user", "content": rng.choice(memo_tpl).format(v=v)},
            {"role": "assistant", "content": f"好的，我記住了：{val}"},
            {"role": "user", "content": rng.choice(recall_tpl).format(vname=vname)},
            {"role": "assistant", "content": val},
        ]})

    # 3) 限定回答
    yn = [("地球是圓的嗎？", "是"), ("魚會飛嗎？", "否"), ("冰是熱的嗎？", "否"),
          ("一年有十二個月嗎？", "是"), ("鯨魚是魚類嗎？", "否"),
          ("貓會下蛋嗎？", "否"), ("太陽會發光嗎？", "是"), ("零大於一嗎？", "否")]
    for q, a in yn:
        records.append(_convo(f"只能回答「是」或「否」。{q}", a))
    choice = [("選一個：蘋果或月亮。請只輸出你的選擇。", "蘋果"),
              ("選一個：跑步或游泳。只輸出選擇。", "游泳"),
              ("紅色或藍色？只輸出一個。", "藍色")]
    for q, a in choice:
        records.append(_convo(q, a))

    # 4) 招呼／一般短答
    for g in ["你好", "早安", "嗨", "在嗎"]:
        records.append(
            _convo(g, "你好！我是星澄，有什麼可以幫你的嗎？"))
    gen = [("你是誰？", "我是星澄，GPTBridge 的本地原生語言模型。"),
           ("謝謝你。", "不客氣！有需要再叫我。"),
           ("今天要做什麼？", "把注意力放在當下最重要的一件事上。")]
    for q, a in gen:
        records.append(_convo(q, a))

    # 5) 算術（避開探針 13+29／6×7／9v4；唯一算式池逼模型真算而非背答案）
    seen: set[tuple[int, int, str]] = set()
    n_arith = 25 + (echo_scale // 8 if echo_scale else 0)
    tries = 0
    while len(seen) < n_arith and tries < n_arith * 8:
        tries += 1
        a, b = rng.randint(2, 99), rng.randint(2, 99)
        op = rng.choice(["+", "+", "-", "×"])  # 加法為主，乘法兩位×個位較可學
        if op == "×":
            b = rng.randint(2, 9)
        if ((a, b, op) in seen or (op == "-" and a < b)
                or (a, b, op) in {(13, 29, "+"), (6, 7, "×")}):
            continue
        seen.add((a, b, op))
        ans = {"+": a + b, "-": a - b, "×": a * b}[op]
        records.append(_convo(f"計算 {a} {op} {b}，只輸出數字。", str(ans)))

    # 5b) 比較（L6 compare 探針的訓練對應，值域唯一；模式簡單，覆蓋拉高）
    n_cmp = 8 + (echo_scale // 2 if echo_scale else 0)
    seen_cmp: set[tuple[int, int]] = set()
    tries = 0
    while len(seen_cmp) < n_cmp and tries < n_cmp * 8:
        tries += 1
        a, b = rng.randint(2, 999), rng.randint(2, 999)
        if a == b or (a, b) in seen_cmp or (a, b) in {(9, 4), (4, 9)}:
            continue
        seen_cmp.add((a, b))
        records.append(_convo(f"{a} 和 {b} 哪個大？只輸出較大的數字。",
                              str(max(a, b))))

    # 6) tool_call 格式
    tool_sys = {"role": "system", "content": (
        "你有一個工具 calculator。當需要計算時，只輸出：<tool_call>"
        '{"name": "calculator", "arguments": {"expression": "算式"}}'
        "</tool_call>，不要輸出其他內容。")}
    for _ in range(15):
        a, b = rng.randint(10, 900), rng.randint(10, 900)
        expr = f"{a} + {b}"
        records.append({"messages": [
            tool_sys,
            {"role": "user", "content": f"請用工具計算 {expr}。"},
            {"role": "assistant", "content": (
                f'<tool_call>{{"name": "calculator", "arguments": '
                f'{{"expression": "{expr}"}}}}</tool_call>')},
        ]})

    return records


def build_replay_records(
    corpus_path: str | Path,
    rng: random.Random,
    *,
    target_chars: int = 120_000,
    chunk_chars: int = 400,
    min_chars: int = 80,
) -> list[dict[str, Any]]:
    """一般語料 replay：prompt=短前綴（mask）、completion=後續。"""
    texts: list[str] = []
    for line in Path(corpus_path).open(encoding="utf-8"):
        try:
            texts.append(str(json.loads(line)["text"]))
        except Exception:
            continue
    rng.shuffle(texts)
    records: list[dict[str, Any]] = []
    collected = 0
    for text in texts:
        if collected >= target_chars:
            break
        t = text.strip()
        if len(t) < min_chars:
            continue
        chunk = t[:chunk_chars]
        cut = rng.randint(20, 60)
        records.append({"prompt": chunk[:cut], "completion": chunk[cut:]})
        collected += len(chunk)
    return records


def build_chat_foundation_dataset(
    corpus_path: str | Path,
    *,
    seed: int = 20260920,
    replay_chars: int = 120_000,
    echo_scale: int = 0,
) -> list[dict[str, Any]]:
    """組合 chat + replay 並打亂；回傳可直接餵 SFTDataset 的記錄列。"""
    rng = random.Random(seed)
    chat = build_chat_records(rng, echo_scale=echo_scale)
    replay = build_replay_records(corpus_path, rng, target_chars=replay_chars)
    records = chat + replay
    rng.shuffle(records)
    return records


def write_dataset(records: Iterable[dict[str, Any]], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, help="corpus train.jsonl")
    parser.add_argument("--out", required=True, help="輸出 jsonl 路徑")
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--replay-chars", type=int, default=120_000)
    parser.add_argument("--echo-scale", type=int, default=0,
                        help="額外產生 N 個唯一複製值（強制 induction 而非記憶）")
    args = parser.parse_args(argv)

    records = build_chat_foundation_dataset(
        args.corpus, seed=args.seed, replay_chars=args.replay_chars,
        echo_scale=args.echo_scale)
    out = write_dataset(records, args.out)
    chat_n = sum(1 for r in records if "messages" in r)
    print(json.dumps({
        "format": DATASET_FORMAT_VERSION,
        "path": str(out),
        "records": len(records),
        "chat": chat_n,
        "replay": len(records) - chat_n,
        "seed": args.seed,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DATASET_FORMAT_VERSION",
    "PROBE_VALUES",
    "build_chat_foundation_dataset",
    "build_chat_records",
    "build_replay_records",
    "write_dataset",
]
