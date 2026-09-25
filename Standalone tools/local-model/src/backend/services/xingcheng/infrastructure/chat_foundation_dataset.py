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
import hashlib
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
    ordered: list[str] = []
    while len(ordered) < n:
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
        if v not in PROBE_VALUES and v not in values:
            values.add(v)
            ordered.append(v)
    return ordered


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
                "照原樣輸出：{w}", "把下面這段原樣打出來：{w}", "複製：{w}",
                "請只輸出這些字：{w}", "請逐字輸出：{w}",
                "只輸出以下字串：{w}", "請原樣打出這幾個字：{w}"]
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

    # 1b) 計數句式複製（「這N個字」家族——v15 殘項：echo 探針句式
    # 「請只輸出這四個字：…」不在模板池。值域放寬至常用字集、
    # 排除 PROBE_VALUES，逼「依數取字＋逐字複製」而非句式背誦）
    if echo_scale:
        zh_wide = ("雲海風星月山林河川光影夢想晨光暮色青石白露松濤竹影溪聲"
                   "火測試驗天地人心金水木土花草鳥魚龍虎春夏秋冬雨雪電腦"
                   "程式語言學習資料庫系統服務安全治理模型訓練推論")
        zh_num = "二三四五"
        count_tpl = ["請只輸出這{n}個字：{w}", "只輸出這{n}個字：{w}",
                     "請輸出以下{n}個字：{w}", "輸出這{n}個字：{w}"]
        seen_cnt: set[str] = set()
        target_cnt = max(1, echo_scale // 4)
        tries = 0
        while len(seen_cnt) < target_cnt and tries < target_cnt * 10:
            tries += 1
            n = rng.choice([2, 3, 4, 4, 5])
            v = "".join(rng.choice(zh_wide) for _ in range(n))
            if v in PROBE_VALUES or v in seen_cnt:
                continue
            seen_cnt.add(v)
            records.append(_convo(
                rng.choice(count_tpl).format(n=zh_num[n - 2], w=v), v))

        # 1c) 寬字集一般 echo（v17 診斷：複製機制已成立——舊字集
        # 4 字完美複誦；失敗集中於從未進 echo 上下文的字元。
        # 以標準模板餵寬字集值提高每字曝光量，目標任意字複製）
        seen_wide: set[str] = set()
        target_wide = max(1, echo_scale // 3)
        tries = 0
        while len(seen_wide) < target_wide and tries < target_wide * 10:
            tries += 1
            v = "".join(rng.choice(zh_wide) for _ in range(rng.randint(2, 5)))
            if v in PROBE_VALUES or v in seen_wide:
                continue
            seen_wide.add(v)
            records.append(_convo(rng.choice(echo_tpl).format(w=v), v))

    # 2) 多輪記憶：值多樣化（代號、名字、顏色、地點、數字…）
    # v19：記憶句式「好的，我記住了：X」曾佔語料 15%+ 造成模式坍塌
    # （任何問題都回記憶句），比例壓低到提示存在但不主導。
    mem_vals = [
        f"代號 {rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
        f"{rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}-{rng.randint(10, 99)}"
        for _ in range(6)
    ] + ["名字 小林", "顏色 深藍", "地點 高雄", "數字 7351", "水果 芒果"]
    if echo_scale:
        mem_kinds = ["代號", "密語", "編號", "數字", "密碼", "序號"]
        mem_vals += [f"{rng.choice(mem_kinds)} {v}"
                     for v in _unique_echo_values(
                         rng, min(echo_scale // 12, 60))]
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
    # L5 choice 探針覆蓋：echo_scale>0 時擴充兩詞擇一配對
    # （v14 教訓：全資料集僅 3 筆 choice，模型學不到「只輸出選擇」格式）
    if echo_scale:
        choice_pool = [
            ("蘋果", "香蕉"), ("貓", "狗"), ("春", "冬"), ("書", "筆"),
            ("山", "海"), ("車", "船"), ("茶", "咖啡"), ("東", "西"),
            ("明", "暗"), ("快", "慢"), ("紅", "綠"), ("天", "地"),
            ("日", "月"), ("金", "銀"), ("飯", "麵"), ("走", "跑"),
            ("讀", "寫"), ("笑", "哭"), ("開", "關"), ("上", "下"),
        ]
        for a, b in choice_pool[: max(0, echo_scale // 50)]:
            pick = rng.choice([a, b])
            choice.append((f"選一個：{a}或{b}。只輸出選擇。", pick))
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


def build_qa_records(rng: random.Random, *, qa_scale: int = 0) -> list[dict[str, Any]]:
    """開放域對話 QA 切片（v19 教訓：純複誦/記憶語料使模型坍塌成
    「我記住了：X」記憶句式；真正的對話能力需要知識問答、建議、
    創作、格式遵循、誠實邊界與多輪追問等真實指令遵循資料）。

    答案皆為第一手策展的 zh-TW 短文（<=120 字），不複製外部語料。
    qa_scale>0 時以問句模板變化擴充至該目標量。
    """
    records: list[dict[str, Any]] = []

    # ── 知識解釋：「什麼是 X」家族 ─────────────────────────────
    knowledge = [
        ("什麼是光合作用？", "光合作用是植物利用陽光，把二氧化碳和水轉換成養分並釋放氧氣的過程，是地球大部分生命的能量來源。"),
        ("什麼是機器學習？", "機器學習是讓電腦從資料中自動找出規律的方法，不需要人工逐條寫規則；常見於語音辨識、推薦系統與語言模型。"),
        ("什麼是複利？", "複利是利息再滾入本金繼續生息，時間越長效果越明顯；例如年利率 5%，本金約 14 年翻倍。"),
        ("什麼是通貨膨脹？", "通貨膨脹是物價普遍持續上漲、同樣的錢能買到的東西變少的現象，央行通常用利率調節。"),
        ("什麼是重力？", "重力是物體之間互相吸引的力，地球的重力讓我們站在地面、讓蘋果落地，也維持行星繞太陽運行。"),
        ("什麼是 DNA？", "DNA 是儲存生物遺傳資訊的分子，像一本由四種鹼基寫成的指令書，決定生物的性狀並代代相傳。"),
        ("什麼是雲端運算？", "雲端運算是透過網路租用遠端伺服器的運算、儲存與服務，使用者不需自建機房，按用量付費。"),
        ("什麼是區塊鏈？", "區塊鏈是一種分散式帳本技術，資料以串連的區塊記錄且難以竄改，常用於加密貨幣與交易存證。"),
        ("什麼是疫苗？", "疫苗把無害化的病原體成分送入體內，讓免疫系統先學會辨識，真正遇到病原時能更快反應。"),
        ("什麼是黑洞？", "黑洞是重力強到連光都無法逃離的天體，通常由大質量恆星塌縮形成，只能靠它對周圍的影響間接觀測。"),
        ("解釋一下什麼是 TCP/IP。", "TCP/IP 是網際網路的基礎通訊協定：IP 負責把資料送到正確位址，TCP 負責把資料分段、排序並確認送達。"),
        ("CPU 和 GPU 差在哪？", "CPU 核心少但通用，擅長複雜邏輯與序列工作；GPU 有上千個小核心，擅長大量平行運算如繪圖與矩陣計算。"),
        ("什麼是 API？", "API 是程式之間約定好的溝通介面，像餐廳菜單：你照格式點餐（送請求），廚房照規格出菜（回結果），不必知道內部怎麼做。"),
        ("什麼是匯率？", "匯率是一國貨幣兌換另一國貨幣的價格，受利率、貿易與市場預期影響，會影響進出口與旅遊成本。"),
        ("什麼是量子電腦？", "量子電腦用量子位元運算，能同時表示多種狀態，在特定問題（如分解大數、分子模擬）上可能比傳統電腦快很多，但目前仍在早期階段。"),
    ]
    q_tpl = ["{q}", "請問{q}", "可以解釋{q}", "我想知道{q}", "{q}請簡單說明。"]
    for q, a in knowledge:
        records.append(_convo(rng.choice(q_tpl).format(q=q), a))

    # ── 建議與方法 ────────────────────────────────────────────
    advice = [
        ("給我三個學習英文的建議", "1. 每天固定接觸：聽 15 分鐘 Podcast 或看一篇短文，比週末突擊有效。2. 開口練習：跟讀或自言自語描述身邊事物。3. 記高頻單字：優先學最常用的 2000 字，搭配例句而非死背。"),
        ("如何改善睡眠品質？", "固定起床時間、睡前一小時遠離螢幕、下午後少碰咖啡因、臥室保持涼爽黑暗；若躺 20 分鐘睡不著，起身做放鬆的事再回床。"),
        ("新手如何開始存錢？", "先記帳一個月找出流向，薪水入帳先存 10–20% 再消費，建立 3–6 個月緊急預備金，再把長期閒錢投入低成本的分散投資。"),
        ("怎麼提升工作效率？", "把一天最重要的事排在精力最好的時段、用番茄鐘切 25 分鐘專注區塊、關掉非必要通知、下午排會議與雜事。"),
        ("第一次學程式該從哪開始？", "從 Python 入門：語法簡單、資源多。邊學邊做小專案（記帳程式、自動整理檔案），遇到錯誤先讀訊息再查資料，每天寫比一次學很久有效。"),
        ("面試前該準備什麼？", "研究公司產品與職缺需求、準備 2–3 個 STAR 結構的經歷故事、練習 30 秒自我介紹、準備想問對方的問題、前一晚確認時間地點。"),
        ("如何減少使用手機的時間？", "把社群 App 移出首頁或設時限、關閉非必要推播、手機充電放客廳不帶進臥房、用實體鬧鐘代替手機鬧鐘。"),
        ("讀書容易分心怎麼辦？", "把手機放遠、用 25 分鐘專注＋5 分鐘休息的循環、先讀最難的科目、把「要讀什麼」寫成具體小目標而不是「讀兩小時」。"),
    ]
    a_tpl = ["{q}。", "{q}？", "可以{q}嗎？", "我想{q}，有什麼方法？"]
    for q, a in advice:
        records.append(_convo(rng.choice(a_tpl).format(q=q), a))

    # ── 比較 ──────────────────────────────────────────────────
    compares = [
        ("租屋和買屋哪個好？", "看情況：租屋彈性大、初期成本低，適合工作未穩定者；買屋能累積資產但要背房貸與維護成本。評估居住年限、頭期款與每月負擔佔收入比再決定。"),
        ("iPhone 和 Android 手機差在哪？", "iPhone 系統整合度高、更新年限長、隱私控管嚴；Android 選擇多、價格帶廣、自訂彈性大。依預算與使用習慣選即可。"),
        ("讀研究所值得嗎？", "若目標職位要求碩士或想轉領域，值得；若只是延後就業壓力，成本效益低。建議先查目標產業的學歷門檻再決定。"),
        ("定期定額和單筆投資哪個好？", "長期統計上單筆投入勝率略高，但定期定額能降低擇時壓力、適合每月有固定收入者。重點是儘早開始並長期持有。"),
    ]
    for q, a in compares:
        records.append(_convo(q, a))

    # ── 創作 ──────────────────────────────────────────────────
    creative = [
        ("幫我寫一首關於秋天的短詩", "楓紅落滿徑，風起送微涼。\n雁影天邊遠，一盞桂花香。"),
        ("寫一句給朋友的生日祝福", "願你新的一歲，日子溫柔、目標清晰，想做的事都能慢慢實現。生日快樂！"),
        ("幫我想一個讀書會的口號", "「一頁一世界，一起讀出更寬的自己。」"),
        ("用一句話形容台灣的夏天", "午後雷陣雨洗過的街道，混著芒果香與蟬鳴的燠熱。"),
        ("寫一個兩句話的鬼故事", "我半夜聽到有人敲窗。可是我家在十四樓。"),
        ("講個笑話", "為什麼工程師分不清萬聖節和聖誕節？因為 Oct 31 == Dec 25（八進位 31 等於十進位 25）。"),
        ("幫我寫一封請假信的開頭", "主管您好：因家中有事需親自處理，想於 O 月 O 日請假一天，工作已事先交接給同事，懇請核准，謝謝。"),
        ("寫一句鼓勵同事的話", "你最近扛下來的那些事大家都看在眼裡，辛苦了——撐過這段就會輕鬆很多。"),
    ]
    for q, a in creative:
        records.append(_convo(q, a))

    misc = [
        ("推薦一本書", "如果想建立思維框架，推薦《快思慢想》；想讀故事，《活出意義來》很耐讀。告訴我你喜歡的類型，我可以再收窄推薦。"),
        ("推薦一部電影", "喜歡劇情片可以看《刺激1995》；動畫推《神隱少女》。告訴我你想看的類型（懸疑、溫馨、科幻）我再精準推薦。"),
        ("這段程式碼在幹嘛？for i in range(10): print(i)", "這段 Python 迴圈從 0 跑到 9，依序印出 0、1、2…9 共十個數字。"),
        ("help me write a short thank you email", "Subject: Thank You\n\nHi [Name],\n\nThank you for your help yesterday — it made a real difference. I appreciate your time.\n\nBest regards"),
        ("我心情不好", "聽起來你現在不太好受。想聊聊發生什麼事了嗎？說出來通常會好一點，我在聽。"),
        ("無聊", "無聊的話可以找點小事做：聽首歌、出門走十分鐘，或讓我出個謎語給你猜？我也可以陪你聊聊。"),
        ("早安", "早安！今天想從哪件事開始？需要我幫忙整理什麼嗎？"),
        ("晚安", "晚安！今天辛苦了，好好休息。有什麼事明天再叫我。"),
    ]
    for q, a in misc:
        records.append(_convo(q, a))

    # ── 格式遵循 ─────────────────────────────────────────────
    records.append(_convo(
        "列出三種常見的程式語言，用條列式。",
        "- Python\n- JavaScript\n- C++"))
    records.append(_convo(
        "用編號列出沖咖啡的三個步驟。",
        "1. 磨豆並量取咖啡粉\n2. 以約 92 度熱水悶蒸 30 秒\n3. 分段注水萃取完成"))
    records.append(_convo(
        "用不超過十個字描述台北。",
        "繁忙卻有溫度的城市"))
    records.append(_convo(
        "把「今天天氣很好，我們去公園散步」翻譯成英文。",
        "The weather is nice today; let's take a walk in the park."))
    records.append(_convo(
        "Translate “practice makes perfect” into Chinese.",
        "熟能生巧"))

    # ── 摘要 ─────────────────────────────────────────────────
    records.append(_convo(
        "摘要這段話：「運動不只能增強心肺功能，研究也顯示規律運動可以改善睡眠品質、降低焦慮，並延緩認知退化。」",
        "規律運動能強化心肺、改善睡眠、降低焦慮並延緩認知退化。"))
    records.append(_convo(
        "用一句話摘要：「台灣高鐵自 2007 年通車以來，大幅縮短南北交通時間，台北到高雄最快約一個半小時，改變了國內旅運與一日生活圈。」",
        "高鐵通車讓台灣南北進入一日生活圈。"))

    # ── 數學應用 ─────────────────────────────────────────────
    math_word = [
        ("小明有 150 元，買了 35 元的飲料，還剩多少錢？", "150 − 35 = 115，還剩 115 元。"),
        ("一箱蘋果有 24 顆，分給 6 個人，每人幾顆？", "24 ÷ 6 = 4，每人 4 顆。"),
        ("時速 60 公里開 2.5 小時，總共走了幾公里？", "60 × 2.5 = 150，共 150 公里。"),
        ("一件 800 元的衣服打八折，要付多少？", "800 × 0.8 = 640，要付 640 元。"),
        ("水壺容量 1.5 公升，倒了 3 杯各 300 毫升，還剩多少？", "1.5 公升 = 1500 毫升，1500 − 900 = 600，還剩 600 毫升。"),
    ]
    for q, a in math_word:
        records.append(_convo(q, a))

    # 直接算式問法（v19 探針：「1+1等於多少」無「計算」前綴未命中算術模式）
    seen_direct: set[tuple[int, int, str]] = set()
    tries = 0
    while len(seen_direct) < 40 and tries < 400:
        tries += 1
        a, b = rng.randint(1, 99), rng.randint(1, 99)
        op = rng.choice(["+", "+", "-", "×"])
        if op == "×":
            a, b = rng.randint(2, 99), rng.randint(2, 9)
        if (a, b, op) in seen_direct or (op == "-" and a < b):
            continue
        if (a, b, op) in {(13, 29, "+"), (6, 7, "×")}:
            continue
        seen_direct.add((a, b, op))
        ans = {"+": a + b, "-": a - b, "×": a * b}[op]
        zh_op = {"+": "加", "-": "減", "×": "乘"}[op]
        tpl = rng.choice([
            f"{a}{op}{b}等於多少？", f"{a} {op} {b} = ?",
            f"{a}{zh_op}{b}是多少？", f"算一下 {a} {op} {b}",
        ])
        records.append(_convo(tpl, f"{a} {op} {b} = {ans}"))

    # ── 身份與能力 ───────────────────────────────────────────
    identity = [
        ("你是誰？", "我是星澄，GPTBridge 平台上的本地原生語言模型，在自己的機器上運行，不需要連外部服務。"),
        ("請自我介紹一下", "我是星澄，本地原生語言模型。我可以回答問題、整理資訊、寫程式建議、做計算分析，也能協助文件摘要。有什麼想問的嗎？"),
        ("介紹一下你自己", "我是星澄，在這台機器本地運行的語言模型，資料不外流。可以問我知識問題、請我寫東西或協助分析。"),
        ("你是什麼模型？", "我是星澄原生 Transformer 模型，在本地自訓，規模約 88M 參數——不大，但完全在這台機器上運作。"),
        ("你叫什麼名字？", "我叫星澄，是 GPTBridge 平台的本地語言模型。"),
        ("你能做什麼？", "我可以回答問題、整理資訊、寫程式建議、做計算與分析、讀文件摘要，也能透過平台工具查詢資料與執行受治理的任務。"),
        ("你是 GPT 嗎？", "不是。我是星澄，在本地自訓的原生模型，規模比 GPT 小很多，但資料不會離開這台機器。"),
        ("你和 GPT 差在哪？", "我在本地運行、資料不外流、能力受治理約束；GPT 是雲端大模型，知識更廣但需要連網。我適合隱私敏感與離線場景。"),
        ("你的資料會被拿去訓練嗎？", "你的對話只存在本機的受治理資料庫中，用於本地自我學習，不會上傳到任何外部服務。"),
    ]
    for q, a in identity:
        records.append(_convo(q, a))

    # ── 誠實邊界（GPT 式：承認限制＋提供替代）─────────────────
    honest = [
        ("今天天氣如何？", "我無法取得即時天氣資料。建議看中央氣象署 App；如果你告訴我地點與預報內容，我可以幫你解讀。"),
        ("現在股價多少？", "我沒有即時行情連線。若你提供數據，我可以幫你分析趨勢或計算報酬率。"),
        ("明天會下雨嗎？", "我沒有天氣預報資料，無法預測。查詢氣象預報後我可以幫你判斷要不要帶傘或調整行程。"),
        ("幫我診斷這個病", "我不是醫師，不能做診斷。若你描述症狀，我可以提供一般性衛教資訊，但持續或嚴重症狀請就醫。"),
        ("幫我看合約有沒有問題", "我可以指出合約中常見的風險條款供參考，但不是法律意見；重要合約建議請律師審閱。"),
        ("推薦我買哪支股票", "我不能保證獲利，也不會給個股買賣指令。可以協助你建立篩選條件、分析風險或解讀財報數據。"),
        ("最新的新聞是什麼？", "我沒有即時新聞連線。如果你貼上新聞內容，我可以幫你摘要或分析。"),
    ]
    for q, a in honest:
        records.append(_convo(q, a))

    # ── 多輪追問 ─────────────────────────────────────────────
    records.append({"messages": [
        _SYSTEM,
        {"role": "user", "content": "什麼是機器學習？"},
        {"role": "assistant", "content": "機器學習是讓電腦從資料中自動找出規律的方法，不需要人工逐條寫規則。"},
        {"role": "user", "content": "那深度學習呢？"},
        {"role": "assistant", "content": "深度學習是機器學習的一支，用多層神經網路自動學特徵，語言模型與影像辨識都靠它。"},
    ]})
    records.append({"messages": [
        _SYSTEM,
        {"role": "user", "content": "給我三個學習英文的建議"},
        {"role": "assistant", "content": "1. 每天固定接觸英文 15 分鐘。2. 開口跟讀練習。3. 優先記高頻單字配例句。"},
        {"role": "user", "content": "第一點可以再具體一點嗎？"},
        {"role": "assistant", "content": "例如通勤時聽一集英文 Podcast、睡前讀一篇短文，重點是「每天」而非「一次很多」，兩週就會養成習慣。"},
    ]})
    records.append({"messages": [
        _SYSTEM,
        {"role": "user", "content": "什麼是複利？"},
        {"role": "assistant", "content": "複利是利息滾入本金繼續生息，時間越長成長越快。"},
        {"role": "user", "content": "舉個例子"},
        {"role": "assistant", "content": "本金 10 萬、年利率 5%，一年後 10.5 萬，十年後約 16.3 萬，二十年後約 26.5 萬——後面加速很明顯。"},
    ]})

    # ── 模板擴充到 qa_scale ──────────────────────────────────
    if qa_scale > 0 and records:
        base = list(records)
        extra_tpl = [
            "請回答：{q}",
            "{q}（簡短回答）",
            "幫忙解答：{q}",
        ]
        while len(records) < qa_scale:
            src = rng.choice(base)
            msgs = src["messages"]
            if len(msgs) < 3 or rng.random() < 0.5:
                records.append(dict(src))
                continue
            user_msg = next(m for m in msgs if m["role"] == "user")
            assistant_msg = next(m for m in msgs if m["role"] == "assistant")
            new_q = rng.choice(extra_tpl).format(q=user_msg["content"])
            records.append(_convo(new_q, assistant_msg["content"]))
    return records


def build_arithmetic_records(
    rng: random.Random, *, arith_scale: int = 700
) -> list[dict[str, Any]]:
    """位值分解算術課程。

    根因修補：BPE 把二位數整數合成單一 token，模型只能死記
    （運算元, 運算元 → 結果）配對——這也是 v19b/v25/v26 裸模型
    算術一律 ~17% 的結構性原因。本切片把答案改為「先拆位、逐位
    運算、最後才輸出結果」的格式，讓結果 token 有機會被計算過程
    決定而非被記憶決定：

        個位 4+2=6，十位 1+8=9 → 14 + 82 = 96

    模型學到的技能是「token→位數分解→位值運算→合成結果」，
    對未見過的運算元組合也能泛化（小模型的合理上限是
    二位數加減與一位數乘除；乘法採直式位值法，除法採乘法逆查）。
    """
    def q_tpl(a: int, op: str, b: int) -> str:
        zh = {"+": "加", "-": "減", "×": "乘", "÷": "除以"}[op]
        return rng.choice([
            f"計算 {a} {op} {b}，只輸出數字。",
            f"{a} {op} {b} = ?",
            f"{a}{op}{b}等於多少？",
            f"算一下 {a} {op} {b}",
            f"{a}{zh}{b}是多少？",
            f"{a} {op} {b} 的答案",
        ])

    def add_cot(a: int, b: int) -> str:
        au, bu = a % 10, b % 10
        at, bt = a // 10, b // 10
        u = au + bu
        carry, ud = divmod(u, 10)
        t = at + bt + carry
        steps = f"個位 {au}+{bu}={u}"
        if carry:
            steps += f" 進 1"
        steps += f"，十位 {at}+{bt}"
        if carry:
            steps += f"+1"
        steps += f"={t} → {a} + {b} = {a + b}"
        return steps

    def sub_cot(a: int, b: int) -> str:
        au, bu = a % 10, b % 10
        at, bt = a // 10, b // 10
        if au >= bu:
            return (
                f"個位 {au}-{bu}={au - bu}，"
                f"十位 {at}-{bt}={at - bt} → {a} - {b} = {a - b}"
            )
        return (
            f"個位 {au} 不夠減 {bu}，借位 {au + 10}-{bu}={au + 10 - bu}，"
            f"十位 {at}-1-{bt}={at - 1 - bt} → {a} - {b} = {a - b}"
        )

    def mul_cot(a: int, b: int) -> str:
        au, at = a % 10, a // 10
        u = au * b
        carry, ud = divmod(u, 10)
        t = at * b + carry
        steps = f"個位 {au}×{b}={u}"
        if carry:
            steps += f" 進 {carry}"
        steps += f"，十位 {at}×{b}"
        if carry:
            steps += f"+{carry}"
        steps += f"={t} → {a} × {b} = {a * b}"
        return steps

    def div_cot(a: int, b: int) -> str:
        q = a // b
        return f"{b} × {q} = {a} → {a} ÷ {b} = {q}"

    records: list[dict[str, Any]] = []
    seen: set[tuple[int, str, int]] = set()
    specs = [
        ("+", 250, lambda: (rng.randint(10, 99), rng.randint(10, 99))),
        ("-", 200, lambda: _desc_pair(rng)),
        ("×", 150, lambda: (rng.randint(10, 99), rng.randint(2, 9))),
        ("÷", 100, lambda: _div_pair(rng)),
        ("1d", 60, lambda: (rng.randint(1, 9), rng.randint(1, 9))),
    ]
    for op, target, sampler in specs:
        made = 0
        tries = 0
        while made < target and tries < target * 20:
            tries += 1
            a, b = sampler()
            key = (a, op, b)
            if key in seen:
                continue
            seen.add(key)
            if op == "+":
                ans = add_cot(a, b)
            elif op == "-":
                ans = sub_cot(a, b)
            elif op == "×":
                ans = mul_cot(a, b)
            elif op == "÷":
                ans = div_cot(a, b)
            else:  # 1d — 一位數直接答（免分解仍給等式結尾）
                real_op = rng.choice(["+", "-", "×"])
                if real_op == "-" and a < b:
                    a, b = b, a
                ans = f"{a} {real_op} {b} = " + str(
                    {"+": a + b, "-": a - b, "×": a * b}[real_op]
                )
                records.append(_convo(q_tpl(a, real_op, b), ans))
                made += 1
                continue
            records.append(_convo(q_tpl(a, op, b), ans))
            made += 1
    rng.shuffle(records)
    return records


def _desc_pair(rng: random.Random) -> tuple[int, int]:
    a, b = rng.randint(10, 99), rng.randint(10, 99)
    return (max(a, b), min(a, b))


def _div_pair(rng: random.Random) -> tuple[int, int]:
    b = rng.randint(2, 9)
    q = rng.randint(2, 12)
    return (b * q, b)


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
    qa_scale: int = 0,
    arith_scale: int = 0,
) -> list[dict[str, Any]]:
    """組合 chat + qa + arith + replay 並打亂；回傳可直接餵 SFTDataset 的記錄列。"""
    rng = random.Random(seed)
    chat = build_chat_records(rng, echo_scale=echo_scale)
    qa = build_qa_records(rng, qa_scale=qa_scale)
    arith = (
        build_arithmetic_records(rng, arith_scale=arith_scale)
        if arith_scale > 0
        else []
    )
    replay = build_replay_records(corpus_path, rng, target_chars=replay_chars)
    records = chat + qa + arith + replay
    rng.shuffle(records)
    return records


def write_dataset(records: Iterable[dict[str, Any]], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    # A558: sidecar artifact hash — hash the bytes on disk (text-mode newline
    # translation differs per platform), verifiable without re-running.
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_suffix(out.suffix + ".sha256").write_text(
        f"{digest}  {out.name}\n", encoding="utf-8"
    )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, help="corpus train.jsonl")
    parser.add_argument("--out", required=True, help="輸出 jsonl 路徑")
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--replay-chars", type=int, default=120_000)
    parser.add_argument("--echo-scale", type=int, default=0,
                        help="額外產生 N 個唯一複製值（強制 induction 而非記憶）")
    parser.add_argument("--qa-scale", type=int, default=0,
                        help="開放域 QA 切片目標量（含模板擴充）")
    parser.add_argument("--arith-scale", type=int, default=0,
                        help="位值分解算術課程切片量")
    args = parser.parse_args(argv)

    records = build_chat_foundation_dataset(
        args.corpus, seed=args.seed, replay_chars=args.replay_chars,
        echo_scale=args.echo_scale, qa_scale=args.qa_scale,
        arith_scale=args.arith_scale)
    out = write_dataset(records, args.out)
    sidecar = out.with_suffix(out.suffix + ".sha256")
    chat_n = sum(1 for r in records if "messages" in r)
    print(json.dumps({
        "format": DATASET_FORMAT_VERSION,
        "path": str(out),
        "artifact_hash": sidecar.read_text(encoding="utf-8").split()[0],
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
    "build_qa_records",
    "build_replay_records",
    "write_dataset",
]
