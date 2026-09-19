"""星澄教師蒸餾資料產生器（建置藍圖 Phase 3 的資料面）。

Ollama 本地模型僅為教師：依第一方主題產生對話候選，全部先經既有品質閘門
（``StarOllamaTrainingGate``）審核，通過者才落地為 ``star-transformer-sft/v1``
快照，供原生 SFT 訓練。教師權重永遠不進入星澄的推論路徑。
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
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


def _gate():
    try:
        from ...application.gpt_training_gate import StarOllamaTrainingGate
    except ImportError:  # pragma: no cover - 直接以套件路徑執行時
        from xingcheng.application.gpt_training_gate import StarOllamaTrainingGate

    return StarOllamaTrainingGate()


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
) -> dict[str, Any]:
    """以教師模型產生候選，經品質閘門後回傳已驗證範例與統計。"""
    gate = _gate()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    started = time.time()

    for index, topic in enumerate(topics):
        if max_examples and len(accepted) >= max_examples:
            break
        model = models[index % len(models)]
        try:
            answer = _call_teacher(
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
        response_digest = gate.digest(answer)
        verdict = gate.evaluate(
            [
                {
                    "candidate_id": f"teacher-{index}",
                    "intent": topic.intent,
                    "input_text": user_input,
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
    parser.add_argument("--val-permille", type=int, default=150)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[9]
    topics = build_grounded_topics(root, maximum=args.max_topics)
    if not topics:
        raise SystemExit("NO_GROUNDED_TOPICS")
    result = generate_distillation_examples(
        topics,
        endpoint=args.endpoint,
        models=tuple(args.model) if args.model else DEFAULT_TEACHER_MODELS,
        timeout=args.timeout,
        num_predict=args.num_predict,
        temperature=args.temperature,
        top_p=args.top_p,
        max_examples=args.max_examples,
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
