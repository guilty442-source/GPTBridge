"""工具呼叫 SFT 資料產生器（``star-tool-call/v1`` 訓練面）。

模型要學會產出 ``<tool_call>`` 標記，需要含標記的訓練樣本——
這不需要教師模型：工具集是固定的受管白名單，問題→工具呼叫的
對應是治理層定義的事實。本模組依 ``TOOL_COMMAND_MAP`` 產生
``star-transformer-sft/v1`` 相容快照（``messages`` 多輪格式，
``SFTDataset`` 只訓練 assistant 輪次）。

兩種樣本形態：
- 單輪：user 提問 → assistant 輸出 tool_call 標記（學會何時呼叫）。
- 多輪：user → assistant tool_call → tool 回填 → assistant 依
  證據回答（學會把工具結果轉成自然語言）。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .native_tool_orchestrator import GovernedToolExecutor, TOOL_COMMAND_MAP
from ..infrastructure.tool_call_format import (
    TOOL_CALL_FORMAT_VERSION,
    encode_tool_calls,
)

TOOL_CALL_DATASET_VERSION = "star-tool-call-dataset/v1"

#: 每個工具的繁中問法與範例參數；多輪形態附一筆合成工具結果。
_TOOL_EXAMPLES: dict[str, dict[str, Any]] = {
    "status": {
        "questions": ["系統現在狀態如何？", "星澄目前還好嗎？", "幫我看一下整體狀態"],
        "arguments": {},
        "result": {"ok": True, "model": "star-main-native-model", "generative_ai": True},
        "answer": "系統目前運作正常，模型與資料庫都已就緒。",
    },
    "platform_status": {
        "questions": ["平台狀態如何？", "git 和 RAG 都正常嗎？", "幫我檢查平台"],
        "arguments": {},
        "result": {"ok": True, "fully_local": True, "governance_source": "codex"},
        "answer": "平台狀態正常：全本地運作，治理來源為法典。",
    },
    "git_status": {
        "questions": ["git 現在有什麼變更？", "看一下儲存庫狀態", "目前工作區乾淨嗎？"],
        "arguments": {},
        "result": {"ok": True, "branch": "main", "clean": True},
        "answer": "儲存庫目前在 main 分支，工作區是乾淨的。",
    },
    "git_history": {
        "questions": ["最近的提交有哪些？", "看最近 5 筆 commit", "git 歷史給我"],
        "arguments": {"limit": 5},
        "result": {"ok": True, "commits": [{"sha": "d18a027", "message": "DPO executor"}]},
        "answer": "最近一筆提交是 d18a027，內容是 DPO executor 接線。",
    },
    "rag_query": {
        "questions": [
            "查一下文件裡怎麼說 KV cache",
            "在文件庫找啟動流程的資料",
            "檢索模型參數基準的說明",
        ],
        "arguments": {"question": "KV cache", "top_k": 6},
        "result": {"ok": True, "retrieved_count": 3, "citations": [{"source": "README"}]},
        "answer": "從本地文件檢索到 3 筆相關證據，來源包含 README。",
    },
    "knowledge_search": {
        "questions": [
            "搜尋知識庫：投資策略",
            "找一下關於治理的筆記",
            "知識庫裡有沒有 SFT 的說明",
        ],
        "arguments": {"query": "投資策略", "limit": 8},
        "result": {"ok": True, "count": 2},
        "answer": "知識庫中找到 2 筆相關條目。",
    },
    "memory_list": {
        "questions": ["你記得哪些事？", "列出你的記憶", "我之前教過你什麼？"],
        "arguments": {"limit": 20},
        "result": {"ok": True, "count": 3, "records": []},
        "answer": "目前記憶中有 3 筆已審核的項目。",
    },
    "sql_list_knowledge": {
        "questions": ["列出知識庫條目", "已存的知識有哪些？", "看一下知識表"],
        "arguments": {"limit": 20},
        "result": {"ok": True, "items": []},
        "answer": "知識表目前是空的。",
    },
    "diagnose_fault": {
        "questions": ["模型回覆很慢，幫我診斷", "診斷一下為什麼啟動失敗", "推理速度變慢了"],
        "arguments": {"symptom": "模型回覆速度變慢"},
        "result": {"ok": True, "matched_fault_code_ids": ["RESIDENCY"]},
        "answer": "診斷結果指向模型駐留競爭，建議檢查同時載入的模型數量。",
    },
    "search_investments": {
        "questions": ["查一下台積電的資料", "看一下市場資訊", "搜尋持股相關新聞"],
        "arguments": {"query": "台積電"},
        "result": {"ok": True, "results": []},
        "answer": "市場查詢已完成，結果列於證據區塊。",
    },
}


def _system_prompt() -> str:
    specs = GovernedToolExecutor.available_tool_specs()
    return (
        "你是星澄，本地 AI 助理。回答前若需要即時資料或系統狀態，"
        "先輸出工具呼叫標記再回答。可用工具："
        + json.dumps(specs, ensure_ascii=False)
    )


def _examples() -> list[dict[str, Any]]:
    system = _system_prompt()
    examples: list[dict[str, Any]] = []
    for name, spec in _TOOL_EXAMPLES.items():
        if name not in TOOL_COMMAND_MAP:
            continue
        call_markup = encode_tool_calls(
            [{"name": name, "arguments": spec["arguments"]}]
        )
        for question in spec["questions"]:
            # 單輪：學會「何時呼叫」
            examples.append(
                {
                    "intent": "tool_call",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": call_markup},
                    ],
                }
            )
        # 多輪：學會「工具結果 → 自然語言回答」
        first = spec["questions"][0]
        result_body = json.dumps(spec["result"], ensure_ascii=False)
        examples.append(
            {
                "intent": "tool_call",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": first},
                    {"role": "assistant", "content": call_markup},
                    {"role": "tool", "content": f"[{name}] {result_body}"},
                    {"role": "assistant", "content": spec["answer"]},
                ],
            }
        )
    return examples


def build_tool_call_snapshot(
    output_path: str | Path,
    *,
    val_permille: int = 100,
) -> dict[str, Any]:
    """工具呼叫訓練集 → ``star-transformer-sft/v1`` 相容快照。"""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for example in _examples():
        text = json.dumps(example["messages"], ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        split = (
            "validation"
            if int(digest[:8], 16) % 1000 < int(val_permille)
            else "train"
        )
        first_user = next(
            (m["content"] for m in example["messages"] if m["role"] == "user"), ""
        )
        last_assistant = next(
            (
                m["content"]
                for m in reversed(example["messages"])
                if m["role"] == "assistant"
            ),
            "",
        )
        records.append(
            {
                "source": f"tool-call-dataset:{TOOL_CALL_DATASET_VERSION}",
                "sha256": digest,
                "text": f"{first_user}\n\n{last_assistant}",
                "prompt": first_user,
                "completion": last_assistant,
                "messages": example["messages"],
                "intent": example["intent"],
                "quality_score": 0.95,
                "split": split,
            }
        )
    if len(records) > 1 and not any(r["split"] == "validation" for r in records):
        records[-1]["split"] = "validation"
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    file_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "format_version": "star-transformer-sft/v1",
        "dataset_kind": TOOL_CALL_DATASET_VERSION,
        "tool_call_format": TOOL_CALL_FORMAT_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "snapshot_path": str(target),
        "snapshot_sha256": file_digest,
        "examples": len(records),
        "train_count": sum(r["split"] == "train" for r in records),
        "validation_count": sum(r["split"] == "validation" for r in records),
        "teacher_models": [],
        "synthetic_governed": True,
    }
    (target.parent / "tool_call_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


__all__ = [
    "TOOL_CALL_DATASET_VERSION",
    "build_tool_call_snapshot",
]
