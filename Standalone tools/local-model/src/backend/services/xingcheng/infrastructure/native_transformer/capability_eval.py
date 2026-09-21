"""Phase 5G — 原生能力評估套件 ``star-capability-eval/v1``。

九類能力（zh-TW／en／math／code／reading／multi_turn／context_tracking／
instruction／tool_call_format）的獨立評估：

- 評估全程只用受測 checkpoint 本身——第三方模型不得參與（無 teacher 路徑）。
- 六欄 metadata：model_id／model_version(checkpoint sha)／tokenizer_sha256／
  dataset_version(suite id+sha)／inference_backend(device+dtype)／quantization。
- 資料不重疊：每筆題目的正規化文字 sha256 與語料 manifest 逐一比對，
  命中訓練語料即拒絕該題（fail-closed 計入 ``overlap_rejected``）。
- 回歸閘門：``compare_reports`` 對照 baseline，任何類別通過率下降即 fail。

    python -m xingcheng.infrastructure.native_transformer.capability_eval \
        --checkpoint <final.pt> --suite <suite.json> \
        [--corpus-manifest <manifest.json>] [--baseline <report.json>] [--save]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import torch

from .checkpoint import load_checkpoint

SUITE_FORMAT = "star-capability-suite/v1"
REPORT_FORMAT = "star-capability-eval/v1"

CATEGORIES = (
    "zh-TW", "en", "math", "code", "reading",
    "multi_turn", "context_tracking", "instruction", "tool_call_format",
)


def _norm_text(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def _text_sha(text: str) -> str:
    return hashlib.sha256(_norm_text(text).encode("utf-8")).hexdigest()


def load_suite(path: str | Path) -> dict[str, Any]:
    spec_path = Path(path).resolve()
    suite = json.loads(spec_path.read_text(encoding="utf-8"))
    if suite.get("format_version") != SUITE_FORMAT:
        raise ValueError("capability suite format_version mismatch")
    if not suite.get("suite_id") or not isinstance(suite.get("items"), list):
        raise ValueError("capability suite requires suite_id and items[]")
    for item in suite["items"]:
        if item.get("category") not in CATEGORIES:
            raise ValueError(f"unknown category: {item.get('category')}")
        if item.get("check") not in (
            "ppl_max", "contains", "first_int", "regex", "tool_call", "choice",
        ):
            raise ValueError(f"unknown check: {item.get('check')}")
    canonical = json.dumps(suite, ensure_ascii=False, sort_keys=True)
    suite["suite_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return suite


def verify_no_overlap(suite: Mapping[str, Any], manifest_path: str | Path) -> dict[str, Any]:
    """每筆題目文字 sha256 與語料逐文件雜湊比對；命中即 fail-closed。

    語料逐文件雜湊取自 manifest 指向的 train/val jsonl 記錄（``sha256``
    欄位為正規化後文件內容雜湊）。檔案缺失或不可讀時回傳
    ``overlap_free=False``（fail-closed，不假設安全）。
    """
    mpath = Path(manifest_path).resolve()
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    corpus_hashes: set[str] = set()
    try:
        for split in ("train", "val"):
            entry = manifest.get(split) or {}
            rel = entry.get("path")
            if not rel:
                continue
            for line in (mpath.parent / str(rel)).read_text(
                encoding="utf-8"
            ).splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("sha256"):
                    corpus_hashes.add(str(rec["sha256"]))
                # 額外保護：直接把文件正規化文字雜湊再算一次（版本相容）
                if rec.get("text"):
                    corpus_hashes.add(_text_sha(str(rec["text"])))
    except (OSError, json.JSONDecodeError):
        return {"rejected_items": [], "overlap_free": False,
                "error": "corpus jsonl unreadable"}
    rejected: list[str] = []
    for item in suite["items"]:
        for field in ("prompt", "eval_text", "expected"):
            text = str(item.get(field) or "")
            if text and _text_sha(text) in corpus_hashes:
                rejected.append(str(item.get("id")))
                break
    return {"rejected_items": rejected, "overlap_free": not rejected,
            "corpus_documents_checked": len(corpus_hashes)}


def _perplexity(model, tokenizer, device, text: str, block: int = 64) -> float:
    ids = list(tokenizer.encode(text, add_bos=False, add_eos=False))
    if len(ids) < 9:
        return float("nan")
    block = min(block, len(ids) - 1)
    total, batches = 0.0, 0
    model.eval()
    with torch.no_grad():
        for start in range(0, len(ids) - block, block):
            batch = torch.tensor([ids[start:start + block]], dtype=torch.long, device=device)
            total += float(model(batch, labels=batch)["loss"].item())
            batches += 1
    return float(math.exp(min(20.0, total / batches))) if batches else float("nan")


def _generate(
    model, tokenizer, device, prompt: str, max_new: int,
    eos_override: int | None = None,
) -> str:
    from .inference.generate import Generator
    from .inference.sampler import Sampler, SamplingConfig

    eos = int(eos_override if eos_override is not None
              else getattr(tokenizer, "eos_id", 2) or 2)
    pad = int(getattr(tokenizer, "pad_id", 0) or 0)
    generator = Generator(
        model,
        sampler=Sampler(SamplingConfig(do_sample=False, eos_token_id=eos, pad_token_id=pad)),
        device=device,
    )
    ids = list(tokenizer.encode(prompt, add_bos=True, add_eos=False)) or [
        int(getattr(tokenizer, "bos_id", 1) or 1)
    ]
    out = generator.generate(
        torch.tensor([ids], dtype=torch.long, device=device), max_new_tokens=max_new
    )
    # Generator.generate 只回傳新生成段（不含 prompt）——不得再切 prefix 長度。
    return tokenizer.decode(out[0].tolist(), skip_special=True)


def _check_item(
    model, tokenizer, device, item: Mapping[str, Any], *,
    chat_mode: bool = False,
) -> dict[str, Any]:
    kind = str(item["check"])
    max_new = int(item.get("max_new_tokens") or 32)
    detail: dict[str, Any] = {"id": item.get("id"), "check": kind}
    try:
        if kind == "ppl_max":
            ppl = _perplexity(model, tokenizer, device, str(item["eval_text"]))
            detail["perplexity"] = ppl
            detail["passed"] = math.isfinite(ppl) and ppl <= float(item["max"])
            return detail
        prompt = str(item["prompt"])
        if chat_mode:
            # SFT 權重以 chat_format 角色標記訓練：生成類探針走
            # render_conversation 的 generation prompt，並於 <|eot|> 停止。
            from .chat_format import ChatMessage, render_conversation

            prompt = render_conversation(
                [ChatMessage("user", prompt)], add_generation_prompt=True,
            )
            eot = getattr(tokenizer, "eot_id", None)
            if eot is None:
                enc = tokenizer.encode("<|eot|>", add_bos=False, add_eos=False)
                eot = int(enc[0]) if len(enc) == 1 else None
            reply = _generate(model, tokenizer, device, prompt, max_new,
                              eos_override=eot)
            detail["prompt_mode"] = "chat_format"
        else:
            reply = _generate(model, tokenizer, device, prompt, max_new)
        detail["reply"] = reply[:200]
        if kind == "contains":
            detail["passed"] = str(item["expected"]) in reply
        elif kind == "choice":
            allowed = item.get("allowed") or [item.get("expected")]
            detail["passed"] = any(str(a) in reply for a in allowed)
        elif kind == "first_int":
            match = re.search(r"-?\d+", reply)
            detail["passed"] = bool(match) and int(match.group(0)) == int(item["expected"])
        elif kind == "regex":
            detail["passed"] = bool(re.search(str(item["pattern"]), reply))
        elif kind == "tool_call":
            from .chat_format import split_tool_call

            _, calls = split_tool_call(reply)
            detail["passed"] = bool(
                calls and calls[0].get("name") == item.get("tool_name")
            )
    except Exception as exc:  # noqa: BLE001 - 評估單題失敗不拖垮整個套件
        detail["passed"] = False
        detail["error"] = str(exc)[:200]
    return detail


def evaluate_checkpoint(
    checkpoint_path: str | Path,
    suite: Mapping[str, Any],
    *,
    device: str = "cpu",
    overlap_manifest: str | Path | None = None,
) -> dict[str, Any]:
    """對 checkpoint 執行九類能力評估；回傳 `star-capability-eval/v1` 報告。"""
    bundle = load_checkpoint(checkpoint_path, map_location=device)
    model, tokenizer = bundle["model"], bundle["tokenizer"]
    model.to(device).eval()
    # SFT 權重以 chat_format 訓練；raw prompt 續寫不是其介面。
    chat_mode = bool(
        (bundle.get("metadata") or {}).get("phase") == "supervised-fine-tuning"
    )

    overlap = {"overlap_free": True, "rejected_items": []}
    if overlap_manifest:
        overlap = verify_no_overlap(suite, overlap_manifest)

    rejected = set(overlap["rejected_items"])
    results: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORIES}
    for item in suite["items"]:
        if item.get("id") in rejected:
            results[str(item["category"])].append(
                {"id": item.get("id"), "check": item.get("check"),
                 "passed": False, "skipped": "train-eval-overlap"}
            )
            continue
        results[str(item["category"])].append(
            _check_item(model, tokenizer, torch.device(device), item,
                        chat_mode=chat_mode)
        )

    categories = {}
    for cat, items in results.items():
        run = [i for i in items if not i.get("skipped")]
        passed = sum(1 for i in run if i.get("passed"))
        categories[cat] = {
            "items": len(items),
            "evaluated": len(run),
            "passed": passed,
            "pass_rate": round(passed / len(run), 4) if run else None,
        }

    param = next(model.parameters(), None)
    return {
        "format_version": REPORT_FORMAT,
        "suite_id": suite.get("suite_id"),
        "suite_sha256": suite.get("suite_sha256"),
        "model_id": "xingcheng-native-transformer",
        "model_version": str(bundle.get("state_sha256") or "")[:16],
        "checkpoint_sha256": bundle.get("state_sha256"),
        "tokenizer_sha256": getattr(tokenizer, "sha256", None)
        or getattr(tokenizer, "fingerprint", None),
        "dataset_version": f"{suite.get('suite_id')}@{str(suite.get('suite_sha256'))[:16]}",
        "inference_backend": f"{device}/{'bf16' if param is not None and param.dtype == torch.bfloat16 else 'fp32'}",
        "quantization": bundle.get("quantization") or "none",
        "categories": categories,
        "items": {c: results[c] for c in CATEGORIES if results[c]},
        "overlap": overlap,
        "third_party_used": False,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def compare_reports(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """回歸閘門：任一類別 pass_rate 下降 → passed=False（fail-closed）。"""
    regressions = []
    for cat in CATEGORIES:
        base = (baseline.get("categories") or {}).get(cat) or {}
        cand = (candidate.get("categories") or {}).get(cat) or {}
        b_rate, c_rate = base.get("pass_rate"), cand.get("pass_rate")
        if b_rate is not None and c_rate is not None and c_rate < b_rate - 1e-9:
            regressions.append({
                "category": cat, "baseline": b_rate, "candidate": c_rate,
            })
    return {
        "passed": not regressions,
        "regressions": regressions,
        "baseline_suite": baseline.get("suite_id"),
        "candidate_suite": candidate.get("suite_id"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--suite", required=True)
    ap.add_argument("--corpus-manifest", default=None)
    ap.add_argument("--baseline", default=None, help="舊報告 JSON，執行回歸比對")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--tool-root", default=".")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    suite = load_suite(args.suite)
    report = evaluate_checkpoint(
        args.checkpoint, suite, device=args.device,
        overlap_manifest=args.corpus_manifest,
    )
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        report["regression_gate"] = compare_reports(baseline, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.save:
        out_dir = Path(args.tool_root) / "xingcheng" / "runtime" / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"capability-eval-{ts}.json"
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"saved: {path.name}")


if __name__ == "__main__":
    main()
