"""星澄自有 Tokenizer：byte-level BPE 訓練與載入。

訓練與推論使用成熟 tokenizers 函式庫（Rust 實作），但詞表、合併規則與
雜湊全部由星澄自有語料產生並凍結為本地 artefact；`state_dict()` 內嵌完整
tokenizer.json，使 checkpoint 可離線重建，不依賴外部服務。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

PAD_ID = 0
BOS_ID = 1
EOS_ID = 2
UNK_ID = 3
SYSTEM_ID = 4
USER_ID = 5
ASSISTANT_ID = 6
TOOL_ID = 7
EOT_ID = 8  # END_OF_TURN
SPECIAL_TOKENS: tuple[str, ...] = (
    "<|pad|>",
    "<|bos|>",
    "<|eos|>",
    "<|unk|>",
    "<|system|>",
    "<|user|>",
    "<|assistant|>",
    "<|tool|>",
    "<|eot|>",
)
TOKENIZER_KIND = "byte-level-bpe"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _require_tokenizers():
    try:
        import tokenizers  # noqa: F401
    except ImportError as error:  # pragma: no cover - 環境缺依賴時 fail-closed
        raise RuntimeError("TOKENIZERS_LIBRARY_REQUIRED") from error
    return tokenizers


def _enforce_vocab_cap(tokenizer_path: Path, vocab_size: int) -> None:
    """HF BpeTrainer 可能多出 token；硬性截斷到目標大小（id 必須 < vocab_size）。"""
    data = json.loads(tokenizer_path.read_text(encoding="utf-8"))
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, dict):
        return
    vocab = model.get("vocab")
    if not isinstance(vocab, dict):
        return
    if len(vocab) <= vocab_size:
        return
    # id 依序配置（special → byte alphabet → merges），因此超出的尾端
    # token 對應尾端的合併規則，兩者一起移除即可保持一致。
    excess = len(vocab) - vocab_size
    merges = model.get("merges")
    if isinstance(merges, list) and excess <= len(merges):
        model["merges"] = merges[:-excess] if excess else merges
    model["vocab"] = {
        token: int(index)
        for token, index in vocab.items()
        if int(index) < vocab_size
    }
    tokenizer_path.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def train_bpe(
    texts: Iterable[str],
    output_dir: str | Path,
    *,
    vocab_size: int = 8_192,
    min_frequency: int = 2,
    corpus_manifest: Mapping[str, Any] | None = None,
) -> dict:
    """在自有語料上訓練 byte-level BPE，凍結詞表並輸出雜湊。

    ``corpus_manifest`` 記入語料身分（dataset_id／root_sha256 等），使
    artifact 可回溯源語料版本——同一 tokenizer 在不同語料代際上重訓
    會產生不同雜湊，沒有 provenance 就無法區分語料漂移與訓練不確定。
    """
    tokenizers = _require_tokenizers()
    from tokenizers import decoders, models, pre_tokenizers, trainers

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)

    backend = tokenizers.Tokenizer(models.BPE(unk_token=SPECIAL_TOKENS[3]))
    backend.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    backend.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(
        vocab_size=vocab_size,
        min_frequency=min_frequency,
        special_tokens=list(SPECIAL_TOKENS),
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        show_progress=False,
    )
    backend.train_from_iterator(texts, trainer=trainer)

    tokenizer_path = target / "tokenizer.json"
    backend.save(str(tokenizer_path))
    _enforce_vocab_cap(tokenizer_path, int(vocab_size))
    backend = tokenizers.Tokenizer.from_file(str(tokenizer_path))
    digest = hashlib.sha256(tokenizer_path.read_bytes()).hexdigest()
    manifest = {
        "created_at": _iso_now(),
        "kind": TOKENIZER_KIND,
        "vocab_size": backend.get_vocab_size(),
        "min_frequency": min_frequency,
        "special_tokens": list(SPECIAL_TOKENS),
        "tokenizer_sha256": digest,
        "tokenizer_file": tokenizer_path.name,
    }
    if isinstance(corpus_manifest, Mapping):
        manifest["corpus"] = {
            key: corpus_manifest[key]
            for key in (
                "dataset_id",
                "dataset_version",
                "dataset_root_sha256",
                "documents",
                "characters",
                "license",
            )
            if key in corpus_manifest
        }
    (target / "tokenizer_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


class NativeBPETokenizer:
    """星澄自有 BPE tokenizer（與 XingChengTokenizer 介面相容）。"""

    kind = TOKENIZER_KIND

    def __init__(self, backend: Any, vocab_size: int) -> None:
        self._backend = backend
        self.vocab_size = int(vocab_size)
        self.pad_id = PAD_ID
        self.bos_id = BOS_ID
        self.eos_id = EOS_ID
        self.unk_id = UNK_ID
        self.system_id = SYSTEM_ID
        self.user_id = USER_ID
        self.assistant_id = ASSISTANT_ID
        self.tool_id = TOOL_ID
        self.eot_id = EOT_ID

    @classmethod
    def train(
        cls,
        texts: Iterable[str],
        output_dir: str | Path,
        *,
        vocab_size: int = 8_192,
        min_frequency: int = 2,
    ) -> "NativeBPETokenizer":
        manifest = train_bpe(
            texts, output_dir, vocab_size=vocab_size, min_frequency=min_frequency
        )
        tokenizer = cls.load(output_dir)
        if tokenizer.vocab_size != manifest["vocab_size"]:
            raise ValueError("TOKENIZER_VOCAB_MISMATCH")
        return tokenizer

    @classmethod
    def load(cls, directory: str | Path) -> "NativeBPETokenizer":
        tokenizers = _require_tokenizers()
        path = Path(directory) / "tokenizer.json"
        if not path.is_file():
            raise FileNotFoundError(f"tokenizer artifact missing: {path}")
        backend = tokenizers.Tokenizer.from_file(str(path))
        return cls(backend, backend.get_vocab_size())

    @classmethod
    def from_state(cls, state: dict) -> "NativeBPETokenizer":
        tokenizers = _require_tokenizers()
        payload = str(state.get("tokenizer_json") or "")
        if not payload:
            raise ValueError("TOKENIZER_STATE_INCOMPLETE")
        backend = tokenizers.Tokenizer.from_str(payload)
        tokenizer = cls(backend, backend.get_vocab_size())
        declared = state.get("tokenizer_sha256")
        if declared and tokenizer.state_dict()["tokenizer_sha256"] != declared:
            raise ValueError("TOKENIZER_STATE_HASH_MISMATCH")
        return tokenizer

    @classmethod
    def from_config(cls, config: Any) -> "NativeBPETokenizer":  # pragma: no cover
        raise ValueError("BPETOKENIZER_REQUIRES_ARTIFACT")

    def state_dict(self) -> dict:
        payload = self._backend.to_str()
        return {
            "kind": TOKENIZER_KIND,
            "vocab_size": self.vocab_size,
            "tokenizer_json": payload,
            "tokenizer_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }

    def __len__(self) -> int:
        return self.vocab_size

    def encode(
        self,
        text: str,
        *,
        add_bos: bool = True,
        add_eos: bool = False,
        max_length: int | None = None,
    ) -> list[int]:
        ids = list(self._backend.encode(text, add_special_tokens=False).ids)
        if add_bos:
            ids.insert(0, self.bos_id)
        if add_eos:
            ids.append(self.eos_id)
        if max_length is not None and len(ids) > max_length:
            ids = ids[:max_length]
            if add_eos and ids:
                ids[-1] = self.eos_id
        return ids

    def encode_batch(
        self,
        texts: Sequence[str],
        *,
        add_bos: bool = True,
        add_eos: bool = True,
        max_length: int | None = None,
    ) -> list[list[int]]:
        return [
            self.encode(t, add_bos=add_bos, add_eos=add_eos, max_length=max_length)
            for t in texts
        ]

    def decode(self, ids: Iterable[int], *, skip_special: bool = True) -> str:
        return self._backend.decode(
            [int(token) for token in ids], skip_special_tokens=skip_special
        )


def _iter_corpus_texts(corpus_dir: Path, limit: int) -> Iterator[str]:
    count = 0
    for name in ("train.jsonl", "val.jsonl"):
        path = corpus_dir / name
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                yield str(json.loads(line)["text"])
                count += 1
                if limit and count >= limit:
                    return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄自有 Tokenizer 訓練")
    parser.add_argument("--corpus-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--vocab-size", type=int, default=8_192)
    parser.add_argument("--min-frequency", type=int, default=2)
    parser.add_argument("--max-documents", type=int, default=0)
    args = parser.parse_args(argv)
    corpus_dir = Path(args.corpus_dir)
    corpus_manifest: dict[str, Any] | None = None
    manifest_path = corpus_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            corpus_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            corpus_manifest = None
    manifest = train_bpe(
        _iter_corpus_texts(corpus_dir, args.max_documents),
        args.output,
        vocab_size=args.vocab_size,
        min_frequency=args.min_frequency,
        corpus_manifest=corpus_manifest,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ASSISTANT_ID",
    "BOS_ID",
    "EOS_ID",
    "EOT_ID",
    "NativeBPETokenizer",
    "PAD_ID",
    "SPECIAL_TOKENS",
    "SYSTEM_ID",
    "TOKENIZER_KIND",
    "TOOL_ID",
    "UNK_ID",
    "USER_ID",
    "train_bpe",
]
