"""星澄教師蒸餾資料產生器（建置藍圖 Phase 3 的資料面）。

Ollama 本地模型僅為教師：依第一方主題產生對話候選，全部先經既有品質閘門
（``StarOllamaTrainingGate``）審核，通過者才落地為 ``star-transformer-sft/v1``
快照，供原生 SFT 訓練。教師權重永遠不進入星澄的推論路徑。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .sft import SFT_TEXT_SEPARATOR

DEFAULT_TEACHER_MODELS: tuple[str, ...] = (
    "glm4:9b",
    "rnj-1:8b-instruct-q4_K_M",
)

SYSTEM_PROMPT = (
    "你是星澄（Xingcheng）的教師模型。請用繁體中文，"
    "忠實改寫使用者提供的參考資料：只使用參考資料中出現的詞語與事實，"
    "不加入任何新名詞、新數字或新結論，不使用表情符號或條列裝飾。"
)


@dataclass(frozen=True)
class DistillationTopic:
    intent: str
    prompt: str
    reference: str = ""
    #: 對話類主題：參照僅用於教師生成與品質閘門，訓練 prompt 只存問題本身。
    store_prompt_without_reference: bool = False


_NON_PROSE_MARKERS = ("```", "mermaid", "flowchart", "-->", "|", "{", "}", "</")


def _paragraphs(text: str, *, minimum: int = 80, maximum: int = 600) -> list[str]:
    blocks: list[str] = []
    for raw in text.split("\n\n"):
        if any(marker in raw for marker in _NON_PROSE_MARKERS):
            continue
        block = " ".join(raw.split())
        if minimum <= len(block) <= maximum:
            blocks.append(block)
    return blocks


CONVERSATION_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("conversation", "你好"),
    ("conversation", "哈囉，你在嗎？"),
    ("conversation", "你是誰？請用兩句話說明你的定位。"),
    ("conversation", "你是什麼模型？"),
    ("conversation", "你和雲端大型語言模型有什麼不同？"),
    ("conversation", "請簡短介紹你自己。"),
    ("capabilities", "你可以幫我做什麼？請列三項。"),
    ("capabilities", "你的核心能力有哪些？"),
    ("capabilities", "你可以幫我寫程式碼嗎？"),
    ("capabilities", "你可以幫我分析投資組合嗎？"),
    ("capabilities", "你會使用工具嗎？"),
    ("status", "你的模型權重從哪裡來？"),
    ("status", "你的推論需要連線網路嗎？"),
    ("status", "你的模型是在哪裡訓練的？"),
    ("governance", "什麼是治理規則？"),
    ("governance", "你如何保護我的資料？"),
    ("governance", "你為什麼不能任意修改系統？"),
    ("governance", "你的回答會留下紀錄嗎？"),
    ("reasoning", "遇到不確定的問題時你會怎麼處理？"),
    ("reasoning", "如果資料不足，你會回答什麼？"),
    ("reasoning", "你如何避免編造不存在的事實？"),
    ("self_upgrade", "你會如何升級自己？"),
    ("self_upgrade", "升級模型時需要什麼程序？"),
    ("conversation", "謝謝你的說明。"),
    ("conversation", "早安"),
    ("conversation", "晚安"),
    ("conversation", "請自我介紹一下。"),
    ("conversation", "你叫什麼名字？"),
    ("conversation", "你是真人嗎？"),
    ("conversation", "你現在在執行什麼？"),
    ("conversation", "你今天狀態如何？"),
    ("conversation", "你在哪裡運行？"),
    ("conversation", "你的資料存在哪裡？"),
    ("conversation", "你可以離線工作嗎？"),
    ("conversation", "你會不會把我的資料傳到外部？"),
    ("capabilities", "你可以閱讀文件並回答問題嗎？"),
    ("capabilities", "你可以摘要長文章嗎？"),
    ("capabilities", "你可以整理表格資料嗎？"),
    ("capabilities", "你可以做數學計算嗎？"),
    ("capabilities", "你可以做統計分析嗎？"),
    ("capabilities", "你可以幫我除錯嗎？"),
    ("capabilities", "你可以搜尋網路資料嗎？"),
    ("capabilities", "你可以記住我說過的話嗎？"),
    ("capabilities", "你和專家模型怎麼分工？"),
    ("capabilities", "你什麼時候會交給其他專家模型？"),
    ("status", "你的模型有多大？"),
    ("status", "你的訓練資料來自哪裡？"),
    ("status", "你是用什麼框架實作的？"),
    ("status", "你的推論引擎包含哪些元件？"),
    ("status", "你支援多少上下文長度？"),
    ("status", "你支援量化嗎？"),
    ("status", "你怎麼保存模型版本？"),
    ("governance", "為什麼模型核心要與網路功能分離？"),
    ("governance", "誰可以修改你的程式碼？"),
    ("governance", "什麼是權限閘門？"),
    ("governance", "你為什麼要留下稽核紀錄？"),
    ("governance", "如果指令違反治理規則你會怎麼做？"),
    ("governance", "你可以自己決定升級嗎？"),
    ("governance", "你不可以執行哪些操作？"),
    ("governance", "什麼是權重替換流程？"),
    ("reasoning", "你如何判斷問題的難度？"),
    ("reasoning", "你如何拆解複雜任務？"),
    ("reasoning", "遇到互相矛盾的資料時你會怎麼做？"),
    ("reasoning", "你如何驗證自己的答案？"),
    ("reasoning", "缺少關鍵資訊時你會怎麼回應？"),
    ("reasoning", "你如何區分事實與推測？"),
    ("reasoning", "請說明你的解題步驟。"),
    ("self_upgrade", "自我升級需要通過哪些檢查？"),
    ("self_upgrade", "升級失敗時如何回復？"),
    ("self_upgrade", "誰批准你的升級提案？"),
    ("self_upgrade", "升級後如何驗證結果？"),
    ("reading", "閱讀文件時你如何保留引用來源？"),
    ("reading", "文件太長時你如何處理？"),
    ("reading", "你如何處理文件中的表格？"),
    ("reading", "遇到多份文件衝突時你會怎麼做？"),
    ("coding", "你產生程式碼後會做哪些檢查？"),
    ("coding", "你如何避免產生危險的程式碼？"),
    ("coding", "你支援哪些程式語言？"),
    ("coding", "你會如何處理測試失敗？"),
    ("memory", "你如何管理長期記憶？"),
    ("memory", "記憶需要經過審核嗎？"),
    ("memory", "我可以刪除你的記憶嗎？"),
    ("investment", "投資分析需要哪些輸入？"),
    ("investment", "你如何處理缺少的市場資料？"),
    ("investment", "投資分析的結論可以直接當作交易指令嗎？"),
    ("statistics", "統計分析時你如何處理異常值？"),
    ("data_organization", "整理資料時如何保留原始值？"),
    ("calculation", "計算結果如何避免被生成文字污染？"),
)


SMALL_TALK_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("你好", "您好，我是星澄，本機執行的原生生成式語言模型。", "conversation"),
    ("哈囉", "您好，我是星澄，本機執行的原生生成式語言模型。", "conversation"),
    ("早安", "早安，我是星澄，本機執行的原生生成式語言模型。", "conversation"),
    ("晚安", "晚安，我是星澄，本機執行的原生生成式語言模型。", "conversation"),
    ("你在嗎？", "我在，我是星澄，本機執行的原生生成式語言模型。", "conversation"),
    ("謝謝", "不客氣，我是星澄，原生模型能獨立完成基本語言模型推理。", "conversation"),
    ("謝謝你的說明。", "不客氣，我是星澄，神經網路核心為第一方原生實作。", "conversation"),
    ("再見", "再見，我是星澄，隨時可以為您服務。", "conversation"),
    ("請自我介紹。", "我是星澄，神經網路核心為第一方原生實作：自有 tokenizer、自有 Transformer 權重、自有訓練引擎與原生推論引擎。", "conversation"),
    ("你會說中文嗎？", "會，我是星澄，以繁體中文與您對話；神經網路核心為第一方原生實作。", "conversation"),
)


def build_small_talk_topics(
    root: str | Path,
    *,
    maximum: int = 20,
) -> list[DistillationTopic]:
    """第一方小型對話主題（問候／道謝）：答案同樣必須通過品質閘門。"""
    reference = _identity_reference(root)
    if not reference:
        return []
    return [
        DistillationTopic(
            intent=intent,
            prompt=question,
            reference=reference,
            store_prompt_without_reference=True,
        )
        for question, _answer, intent in SMALL_TALK_PAIRS[:maximum]
    ]


def _identity_reference(root: str | Path, *, maximum: int = 12) -> str:
    project_root = Path(root)
    candidates: list[str] = []
    seen: set[str] = set()
    for relative in (
        "governance_rule/codex/architecture-tool-local-model.md",
        "governance_rule/codex/architecture-project.md",
        "governance_rule/codex/architecture-rag.md",
        "governance_rule/codex/architecture-sql.md",
        "AGENTS.md",
        "Standalone tools/local-model/README.md",
        "Standalone tools/local-model/模型參數基準表.md",
    ):
        path = project_root / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for block in _paragraphs(text):
            if "星澄" not in block and "模型" not in block:
                continue
            key = block[:60]
            if key in seen:
                continue
            seen.add(key)
            candidates.append(block)
            if len(candidates) >= maximum:
                break
        if len(candidates) >= maximum:
            break
    return "\n".join(candidates)[:2_500]


def build_conversation_topics(
    root: str | Path,
    *,
    maximum: int = 40,
) -> list[DistillationTopic]:
    """以第一方身分文本為 grounding 的一般對話主題（問候／身分／能力）。"""
    reference = _identity_reference(root)
    if not reference:
        return []
    return [
        DistillationTopic(
            intent=intent,
            prompt=question,
            reference=reference,
            store_prompt_without_reference=True,
        )
        for intent, question in CONVERSATION_QUESTIONS[:maximum]
    ]


_SOURCE_PATTERNS: tuple[str, ...] = (
    "governance_rule/codex/*.md",
    "governance_rule/codex/*.txt",
    "docs/*.md",
    "Standalone tools/local-model/*.md",
    "AGENTS.md",
)


def build_grounded_topics(
    root: str | Path,
    *,
    per_document: int = 6,
    maximum: int = 30,
) -> list[DistillationTopic]:
    """從第一方法典／文件抽取段落，配成有參照的問答主題。"""
    project_root = Path(root)
    templates: tuple[tuple[str, str], ...] = (
        ("conversation", "請用兩句話改寫參考資料，只使用其中出現的詞語與事實。"),
        ("capabilities", "請改寫參考資料，說明其中的規則，不得加入任何新名詞或新事實。"),
        ("reasoning", "請忠實重述參考資料的內容，保留原本用語，不新增任何資訊。"),
    )
    paths: list[Path] = []
    for pattern in _SOURCE_PATTERNS:
        matches = sorted(project_root.glob(pattern))
        paths.extend(path for path in matches if path.is_file())

    topics: list[DistillationTopic] = []
    seen_blocks: set[str] = set()
    for path in paths:
        if len(topics) >= maximum:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        blocks = _paragraphs(text)
        if not blocks:
            continue
        step = max(1, len(blocks) // max(1, per_document))
        picked = 0
        for index in range(0, len(blocks), step):
            if len(topics) >= maximum or picked >= per_document:
                break
            block = blocks[index]
            if block in seen_blocks:
                continue
            seen_blocks.add(block)
            picked += 1
            intent, prompt = templates[len(topics) % len(templates)]
            topics.append(
                DistillationTopic(intent=intent, prompt=prompt, reference=block)
            )
    return topics


def _call_teacher(
    endpoint: str,
    model: str,
    user_prompt: str,
    *,
    timeout: float,
    num_predict: int,
    temperature: float,
    top_p: float,
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    from .teachers import assert_loopback_endpoint

    endpoint = assert_loopback_endpoint(endpoint)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "num_ctx": 4096,
            "num_predict": int(num_predict),
            "temperature": float(temperature),
            "top_p": float(top_p),
        },
    }
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    message = body.get("message") or {}
    content = str(message.get("content") or "").strip()
    if not content:
        thinking = str(message.get("thinking") or "").strip()
        if thinking:
            content = thinking
    return content


def _call_teacher_topic(
    endpoint: str,
    model: str,
    topic: "DistillationTopic",
    *,
    timeout: float,
    num_predict: int,
    temperature: float,
    top_p: float,
) -> str:
    user_prompt = topic.prompt
    if topic.reference:
        user_prompt += f"\n\n參考資料：\n{topic.reference}"
    return _call_teacher(
        endpoint,
        model,
        user_prompt,
        timeout=timeout,
        num_predict=num_predict,
        temperature=temperature,
        top_p=top_p,
    )


def _gate():
    try:
        from ...application.gpt_training_gate import StarOllamaTrainingGate
    except ImportError:  # pragma: no cover - 直接以套件路徑執行時
        from xingcheng.application.gpt_training_gate import StarOllamaTrainingGate

    return StarOllamaTrainingGate()


BATCH_SYSTEM_PROMPT = (
    "你是星澄（Xingcheng）的教師模型。請只根據使用者提供的參考資料，"
    "產生數組問答。每個回答都必須只使用參考資料中出現的詞語與事實，"
    "不得加入任何新名詞、新數字或新結論。只輸出 JSON，不要其他文字。"
)

_QUESTION_TEMPLATES: tuple[str, ...] = (
    "這段參考資料的重點是什麼？",
    "這段內容規範了哪些規則？",
    "根據這段資料，應該遵守什麼原則？",
    "這段資料說明了什麼限制？",
    "請說明這段內容的適用範圍。",
    "這段資料提到哪些角色或元件？",
    "這段內容要求什麼條件？",
    "這段資料如何處理例外情況？",
)


def _parse_qa_json(content: str) -> list[tuple[str, str]]:
    candidate = str(content or "").strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", candidate)
        if match is None:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, dict):
        parsed = parsed.get("examples") or parsed.get("qa") or parsed.get("pairs")
    if not isinstance(parsed, list):
        return []
    pairs: list[tuple[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        question = str(
            item.get("question") or item.get("input_text") or item.get("prompt") or ""
        ).strip()
        answer = str(
            item.get("answer") or item.get("target_text") or item.get("completion") or ""
        ).strip()
        if question and answer:
            pairs.append((question, answer))
    return pairs


def _batch_prompt(reference: str, question_count: int) -> str:
    questions = "\n".join(
        f"{position + 1}. {_QUESTION_TEMPLATES[position % len(_QUESTION_TEMPLATES)]}"
        for position in range(max(1, question_count))
    )
    return (
        f"參考資料：\n{reference}\n\n"
        f"請為下列每個問題產生一個答案（答案只能是參考資料的忠實改寫）：\n{questions}\n\n"
        '只輸出 JSON：{"examples":[{"question":"問題","answer":"答案"}]}'
    )


CONVERSATION_BATCH_SYSTEM_PROMPT = (
    "你是星澄（Xingcheng）的教師模型。請依序回答使用者的問題，"
    "每個答案都只使用參考資料中出現的詞語與事實，不加入任何新名詞、新數字或新結論，"
    "答案長度以一到三句為原則。只輸出 JSON，不要其他文字。"
)


def _conversation_batch_prompt(questions: Sequence[str], reference: str) -> str:
    numbered = "\n".join(
        f"{position + 1}. {question}" for position, question in enumerate(questions)
    )
    return (
        f"參考資料：\n{reference}\n\n"
        f"請依序回答下列 {len(questions)} 個問題，並以 JSON 回覆："
        '{"answers":["答案1","答案2",...]}\n\n'
        f"問題：\n{numbered}"
    )


def _parse_answers(content: str) -> list[str]:
    candidate = str(content or "").strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", candidate)
        if match is None:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, dict):
        parsed = parsed.get("answers") or parsed.get("responses")
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def generate_conversation_distillation(
    topics: Sequence[DistillationTopic],
    *,
    endpoint: str = "http://127.0.0.1:11434",
    models: Sequence[str] = DEFAULT_TEACHER_MODELS,
    timeout: float = 300.0,
    num_predict: int = 900,
    temperature: float = 0.3,
    top_p: float = 0.9,
    max_examples: int = 0,
    questions_per_call: int = 6,
) -> dict[str, Any]:
    """對話批次蒸餾：一次呼叫回答多題，題目與答案依序對齊。"""
    gate = _gate()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    started = time.time()
    reference = topics[0].reference if topics else ""
    if not reference:
        raise ValueError("CONVERSATION_DISTILLATION_REQUIRES_REFERENCE")

    chunk_size = max(1, int(questions_per_call))
    for chunk_index in range(0, len(topics), chunk_size):
        if max_examples and len(accepted) >= max_examples:
            break
        chunk = topics[chunk_index : chunk_index + chunk_size]
        questions = [topic.prompt for topic in chunk]
        model = models[(chunk_index // chunk_size) % len(models)]
        try:
            content = _call_teacher(
                endpoint,
                model,
                _conversation_batch_prompt(questions, reference),
                timeout=timeout,
                num_predict=num_predict,
                temperature=temperature,
                top_p=top_p,
                system_prompt=CONVERSATION_BATCH_SYSTEM_PROMPT,
            )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            failures.append(
                {"chunk": chunk_index, "model": model, "error": str(error)[:200]}
            )
            continue
        answers = _parse_answers(content)
        if len(answers) < len(questions):
            failures.append(
                {
                    "chunk": chunk_index,
                    "model": model,
                    "error": f"ANSWER_COUNT_MISMATCH:{len(answers)}/{len(questions)}",
                }
            )
        pairs = [
            (question, answer)
            for question, answer in zip(questions, answers)
            if answer
        ]
        for offset, (question, answer) in enumerate(pairs):
            _accept_candidates(
                gate,
                [(question, answer)],
                intent=chunk[offset].intent,
                reference=reference,
                model=model,
                accepted=accepted,
                rejected=rejected,
                max_examples=max_examples,
            )

    return {
        "accepted": accepted,
        "rejected": rejected,
        "failures": failures,
        "elapsed_seconds": round(time.time() - started, 2),
        "teacher_models": list(models),
    }


def _accept_candidates(
    gate: Any,
    candidates: Sequence[tuple[str, str]],
    *,
    intent: str,
    reference: str,
    model: str,
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    max_examples: int,
) -> None:
    """把候選送入既有品質閘門；通過者附加教師來源後收集。"""
    if not candidates:
        return
    payload = [
        {
            "candidate_id": f"teacher-{len(accepted) + position}",
            "intent": intent,
            "input_text": question,
            "target_text": answer,
        }
        for position, (question, answer) in enumerate(candidates)
    ]
    verdict = gate.evaluate(
        payload,
        requested_intent=intent,
        reference_text=reference,
        response_digest=gate.digest("\n".join(answer for _, answer in candidates)),
        source_type="teacher-distillation",
        received_via="ollama-loopback-only",
    )
    for example in verdict.get("accepted") or []:
        if max_examples and len(accepted) >= max_examples:
            return
        accepted.append(
            {
                "intent": example["intent"],
                "prompt": example["input_text"],
                "completion": example["target_text"],
                "teacher_model": model,
                "quality_score": float(example.get("quality_score") or 0.0),
                "gate": example.get("validation") or {},
            }
        )
    for example in verdict.get("rejected") or []:
        rejected.append(
            {
                "intent": intent,
                "prompt": reference[:120],
                "completion": "",
                "teacher_model": model,
                "reasons": example.get("reasons") or [],
            }
        )


def generate_distillation_examples(
    topics: Sequence[DistillationTopic],
    *,
    endpoint: str = "http://127.0.0.1:11434",
    models: Sequence[str] = DEFAULT_TEACHER_MODELS,
    timeout: float = 300.0,
    num_predict: int = 220,
    temperature: float = 0.3,
    top_p: float = 0.9,
    max_examples: int = 0,
    questions_per_reference: int = 1,
) -> dict[str, Any]:
    """以教師模型產生候選，經品質閘門後回傳已驗證範例與統計。

    ``questions_per_reference > 1`` 時改用批次問答模式：一次呼叫為同一段
    參考資料產生多組問答，提升單位時間的合格樣本產出。
    """
    gate = _gate()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    started = time.time()
    batch_mode = int(questions_per_reference) > 1

    for index, topic in enumerate(topics):
        if max_examples and len(accepted) >= max_examples:
            break
        model = models[index % len(models)]
        if batch_mode:
            try:
                content = _call_teacher(
                    endpoint,
                    model,
                    _batch_prompt(topic.reference, int(questions_per_reference)),
                    timeout=timeout,
                    num_predict=num_predict,
                    temperature=temperature,
                    top_p=top_p,
                    system_prompt=BATCH_SYSTEM_PROMPT,
                )
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                failures.append(
                    {"intent": topic.intent, "prompt": topic.prompt, "model": model, "error": str(error)[:200]}
                )
                continue
            pairs = _parse_qa_json(content)
            if not pairs:
                failures.append(
                    {
                        "intent": topic.intent,
                        "prompt": topic.prompt,
                        "model": model,
                        "error": "TEACHER_OUTPUT_NOT_JSON",
                    }
                )
                continue
            _accept_candidates(
                gate,
                pairs,
                intent=topic.intent,
                reference=topic.reference,
                model=model,
                accepted=accepted,
                rejected=rejected,
                max_examples=max_examples,
            )
            continue
        try:
            answer = _call_teacher_topic(
                endpoint,
                model,
                topic,
                timeout=timeout,
                num_predict=num_predict,
                temperature=temperature,
                top_p=top_p,
            )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            failures.append(
                {"intent": topic.intent, "prompt": topic.prompt, "model": model, "error": str(error)[:200]}
            )
            continue
        if not answer:
            failures.append(
                {"intent": topic.intent, "prompt": topic.prompt, "model": model, "error": "EMPTY_TEACHER_OUTPUT"}
            )
            continue

        user_input = topic.prompt
        if topic.reference:
            user_input = f"{topic.prompt}\n\n參考資料：\n{topic.reference}"
        stored_prompt = (
            topic.prompt if topic.store_prompt_without_reference else user_input
        )
        response_digest = gate.digest(answer)
        verdict = gate.evaluate(
            [
                {
                    "candidate_id": f"teacher-{index}",
                    "intent": topic.intent,
                    "input_text": stored_prompt,
                    "target_text": answer,
                }
            ],
            requested_intent=topic.intent,
            reference_text=topic.reference,
            response_digest=response_digest,
            source_type="teacher-distillation",
            received_via="ollama-loopback-only",
        )
        if verdict.get("accepted"):
            for example in verdict["accepted"]:
                accepted.append(
                    {
                        "intent": example["intent"],
                        "prompt": example["input_text"],
                        "completion": example["target_text"],
                        "teacher_model": model,
                        "quality_score": float(example.get("quality_score") or 0.0),
                        "gate": example.get("validation") or {},
                    }
                )
        else:
            for example in verdict.get("rejected") or []:
                rejected.append(
                    {
                        "intent": topic.intent,
                        "prompt": topic.prompt,
                        "completion": answer,
                        "teacher_model": model,
                        "reasons": example.get("reasons") or [],
                    }
                )

    return {
        "accepted": accepted,
        "rejected": rejected,
        "failures": failures,
        "elapsed_seconds": round(time.time() - started, 2),
        "teacher_models": list(models),
    }


def build_distillation_snapshot(
    result: dict[str, Any],
    output_path: str | Path,
    *,
    val_permille: int = 150,
) -> dict[str, Any]:
    """把已驗證範例寫成 ``star-transformer-sft/v1`` 快照（含雜湊與切分）。"""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for example in result.get("accepted") or []:
        prompt = str(example["prompt"]).strip()
        completion = str(example["completion"]).strip()
        text = f"{prompt}{SFT_TEXT_SEPARATOR}{completion}"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        split = "validation" if int(digest[:8], 16) % 1000 < val_permille else "train"
        records.append(
            {
                "source": f"teacher-distillation:{example.get('teacher_model') or ''}",
                "sha256": digest,
                "text": text,
                "prompt": prompt,
                "completion": completion,
                "intent": str(example.get("intent") or ""),
                "quality_score": float(example.get("quality_score") or 0.0),
                "split": split,
            }
        )
    if not records:
        raise ValueError("DISTILLATION_DATASET_EMPTY")

    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    file_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "format_version": "star-transformer-sft/v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "snapshot_path": str(target),
        "snapshot_sha256": file_digest,
        "examples": len(records),
        "train_count": sum(record["split"] == "train" for record in records),
        "validation_count": sum(record["split"] == "validation" for record in records),
        "rejected": len(result.get("rejected") or []),
        "failures": len(result.get("failures") or []),
        "teacher_models": result.get("teacher_models") or [],
    }
    (target.parent / "distillation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="星澄教師蒸餾語料產生器")
    parser.add_argument("--output", required=True, help="SFT 快照輸出路徑")
    parser.add_argument("--root", default=None, help="第一方文件根目錄")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", action="append", default=None)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--num-predict", type=int, default=220)
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--max-examples", type=int, default=0)
    parser.add_argument("--max-topics", type=int, default=24)
    parser.add_argument("--questions-per-reference", type=int, default=1)
    parser.add_argument("--val-permille", type=int, default=150)
    parser.add_argument(
        "--topic-mode",
        default="grounded",
        choices=["grounded", "conversation"],
        help="grounded=文件段落改寫；conversation=一般對話問答",
    )
    parser.add_argument(
        "--questions-per-call",
        type=int,
        default=1,
        help="附對話批量問答模式：一次教師呼叫回答幾題（僅 conversation 模式適用）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[9]
    if args.topic_mode == "conversation":
        topics = build_conversation_topics(root, maximum=args.max_topics)
    else:
        topics = build_grounded_topics(root, maximum=args.max_topics)
    if not topics:
        raise SystemExit("NO_GROUNDED_TOPICS")
    if args.topic_mode == "conversation" and args.questions_per_call > 1:
        result = generate_conversation_distillation(
            topics,
            endpoint=args.endpoint,
            models=tuple(args.model) if args.model else DEFAULT_TEACHER_MODELS,
            timeout=args.timeout,
            num_predict=args.num_predict,
            temperature=args.temperature,
            top_p=args.top_p,
            max_examples=args.max_examples,
            questions_per_call=args.questions_per_call,
        )
    else:
        result = generate_distillation_examples(
            topics,
            endpoint=args.endpoint,
            models=tuple(args.model) if args.model else DEFAULT_TEACHER_MODELS,
            timeout=args.timeout,
            num_predict=args.num_predict,
            temperature=args.temperature,
            top_p=args.top_p,
            max_examples=args.max_examples,
            questions_per_reference=args.questions_per_reference,
        )
    manifest = build_distillation_snapshot(
        result, args.output, val_permille=args.val_permille
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("failures"):
        print(json.dumps({"failures": result["failures"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_TEACHER_MODELS",
    "DistillationTopic",
    "SYSTEM_PROMPT",
    "build_distillation_snapshot",
    "build_grounded_topics",
    "generate_distillation_examples",
    "main",
]
