"""32k／64k（及任意集合）詞表配比分析（P16）。

對同一語料集逐 tokenizer 計算壓縮與覆蓋指標，產出可重現的
``star-vocab-ratio/v1`` 報告：

- 每語言類別（zh-TW／en／code／general）的 fertility（chars/token）、
  token 總量、`<|unk|>` 率與平均 token 長度；
- 報告綁定語料 ``dataset_id``／``train_sha256`` 與各 tokenizer
  ``tokenizer_sha256``——語料或詞表漂移即產生不同報告雜湊，無法
  以舊報告冒充新語料的量測；
- 純量測，不訓練、不改寫任何 tokenizer 或語料。

用法::

    python -m xingcheng.infrastructure.native_transformer.training.vocab_ratio \
        --corpus xingcheng/runtime/corpus-v3 \
        --tokenizer xingcheng/runtime/tokenizers/xingcheng-bpe-32k-v1 \
        --tokenizer xingcheng/runtime/tokenizers/xingcheng-bpe-64k-mf1-v1 \
        --output xingcheng/runtime/logs/vocab-ratio-<ts>.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _measure_tokenizer(
    tokenizer_dir: Path,
    documents: list[dict[str, Any]],
    *,
    max_chars_per_doc: int = 0,
) -> dict[str, Any]:
    """Encode every document once; aggregate per-language statistics."""
    from ..bpe import NativeBPETokenizer

    tokenizer = NativeBPETokenizer.load(str(tokenizer_dir))
    manifest_path = tokenizer_dir / "tokenizer_manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError:
            manifest = {}

    unk_id = getattr(tokenizer, "unk_id", None)

    per_language: dict[str, dict[str, float]] = {}
    token_len_sum = 0.0
    token_len_count = 0
    total_tokens = 0
    total_chars = 0
    unk_tokens = 0
    # 子詞組成：token 表面前十常見前綴，供「配比」定性觀察
    token_counter: Counter[str] = Counter()

    for doc in documents:
        text = str(doc.get("text") or "")
        if max_chars_per_doc > 0:
            text = text[:max_chars_per_doc]
        if not text:
            continue
        language = str(doc.get("language") or "general")
        encoding = tokenizer._backend.encode(text)
        ids = list(encoding.ids)
        n_tokens = len(ids)
        n_chars = len(text)
        total_tokens += n_tokens
        total_chars += n_chars
        if unk_id is not None:
            unk_tokens += sum(1 for i in ids if i == unk_id)
        for tok in encoding.tokens[:5000]:
            token_counter[tok] += 1
            token_len_sum += len(tok)
            token_len_count += 1
        bucket = per_language.setdefault(
            language, {"documents": 0, "characters": 0, "tokens": 0}
        )
        bucket["documents"] += 1
        bucket["characters"] += n_chars
        bucket["tokens"] += n_tokens

    for language, bucket in per_language.items():
        tokens = bucket["tokens"] or 1
        bucket["chars_per_token"] = round(bucket["characters"] / tokens, 4)

    return {
        "tokenizer": tokenizer_dir.name,
        "vocab_size": int(getattr(tokenizer, "vocab_size", 0)),
        "tokenizer_sha256": manifest.get("tokenizer_sha256")
        or _file_sha256(tokenizer_dir / "tokenizer.json"),
        "trained_on_dataset": (manifest.get("corpus") or {}).get(
            "dataset_id"
        ),
        "documents": sum(b["documents"] for b in per_language.values()),
        "characters": total_chars,
        "tokens": total_tokens,
        "chars_per_token": round(total_chars / max(total_tokens, 1), 4),
        "unk_tokens": unk_tokens,
        "unk_rate": round(unk_tokens / max(total_tokens, 1), 6),
        "avg_token_surface_len": round(
            token_len_sum / max(token_len_count, 1), 4
        ),
        "per_language": per_language,
        "top_tokens": dict(token_counter.most_common(25)),
    }


def analyze(
    corpus_dir: str | Path,
    tokenizer_dirs: Iterable[str | Path],
    *,
    max_chars_per_doc: int = 0,
) -> dict[str, Any]:
    """Build the deterministic ``star-vocab-ratio/v1`` report."""
    corpus_dir = Path(corpus_dir)
    manifest = json.loads(
        (corpus_dir / "manifest.json").read_text(encoding="utf-8")
    )
    documents = [
        doc
        for doc in _iter_jsonl(corpus_dir / "train.jsonl")
    ]
    started = time.time()
    measurements = [
        _measure_tokenizer(
            Path(d), documents, max_chars_per_doc=max_chars_per_doc
        )
        for d in tokenizer_dirs
    ]
    report = {
        "schema": "star-vocab-ratio/v1",
        "created_at": _utc_now(),
        "corpus": {
            "dir": str(corpus_dir),
            "dataset_id": manifest.get("dataset_id"),
            "dataset_root_sha256": manifest.get("dataset_root_sha256"),
            "train_sha256": (manifest.get("train") or {}).get("sha256"),
            "documents": manifest.get("documents"),
        },
        "measurements": measurements,
        "elapsed_s": round(time.time() - started, 2),
    }
    canonical = json.dumps(
        {k: v for k, v in report.items() if k not in ("created_at", "elapsed_s")},
        ensure_ascii=False,
        sort_keys=True,
    )
    report["report_sha256"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄詞表配比分析")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--tokenizer", action="append", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--max-chars-per-doc",
        type=int,
        default=0,
        help="每文件取樣上限（0=全文）",
    )
    args = parser.parse_args(argv)
    report = analyze(
        args.corpus, args.tokenizer, max_chars_per_doc=args.max_chars_per_doc
    )
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["analyze", "main"]
