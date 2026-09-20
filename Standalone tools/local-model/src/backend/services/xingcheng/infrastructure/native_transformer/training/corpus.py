"""星澄自有語料管線：只收第一方可稽核來源，輸出 JSONL 與 manifest。

來源一律限於本 repo 內、由星澄所有者持有的文字（法典、架構文件、第一方原始碼），
每個文件都記錄來源路徑與 sha256，建立可重現、可審計的訓練語料。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

DEFAULT_SOURCES: tuple[str, ...] = (
    "governance_rule/execution",
    "docs",
    "Standalone tools",
    "main-system/src-core",
    "main-system/src-ui",
    "main-system/tests",
    "shared-layer/src",
    "shared-layer/tests",
)

DEFAULT_SUFFIXES: tuple[str, ...] = (
    ".md",
    ".py",
    ".ts",
    ".tsx",
    ".sql",
    ".json",
    ".txt",
)

EXCLUDED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git",
        ".worktrees",
        ".venv",
        "__pycache__",
        "runtime",
        "node_modules",
        "backups",
        "logs",
        "temp",
        "state",
        "dist",
        "build",
        "out",
        "release",
        "data",
        "locales",
    }
)

PROTECTED_SOURCE_PREFIXES: tuple[str, ...] = (
    "governance_rule/codex",
)

MIN_DOCUMENT_CHARS = 64
MAX_DOCUMENT_CHARS = 2_000_000


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_text(raw: str) -> str:
    text = raw.lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    normalized = "\n".join(lines)
    while "\n\n\n" in normalized:
        normalized = normalized.replace("\n\n\n", "\n\n")
    return normalized.strip()


@dataclass(frozen=True)
class CorpusDocument:
    """單一語料文件（已正規化）。"""

    source: str
    text: str
    sha256: str

    @classmethod
    def create(cls, source: str, text: str) -> "CorpusDocument":
        return cls(source=source, text=text, sha256=_sha256_text(text))


def _is_protected_source(root: Path, path: Path) -> bool:
    relative = path.resolve().relative_to(root).as_posix()
    return any(
        relative == prefix or relative.startswith(prefix + "/")
        for prefix in PROTECTED_SOURCE_PREFIXES
    )


def _rejected_sources(sources: Sequence[str]) -> tuple[str, ...]:
    rejected: list[str] = []
    for source in sources:
        normalized = str(source).replace("\\", "/").strip("/")
        if any(
            normalized == prefix or normalized.startswith(prefix + "/")
            for prefix in PROTECTED_SOURCE_PREFIXES
        ):
            rejected.append(normalized)
    return tuple(sorted(set(rejected)))


def _iter_files(root: Path, sources: Sequence[str], suffixes: Sequence[str]) -> Iterator[Path]:
    for source in sources:
        base = (root / source).resolve()
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if _is_protected_source(root, path):
                continue
            if path.suffix.casefold() not in suffixes:
                continue
            if any(part in EXCLUDED_DIRECTORIES for part in path.parts):
                continue
            yield path


def iter_documents(
    root: str | Path,
    *,
    sources: Sequence[str] = DEFAULT_SOURCES,
    suffixes: Sequence[str] = DEFAULT_SUFFIXES,
) -> Iterator[CorpusDocument]:
    """逐一讀出正規化後的第一方文件；無法讀取或過短者跳過。"""
    project_root = Path(root).resolve()
    for path in _iter_files(project_root, sources, suffixes):
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _normalize_text(raw)
        if len(text) < MIN_DOCUMENT_CHARS:
            continue
        if len(text) > MAX_DOCUMENT_CHARS:
            text = text[:MAX_DOCUMENT_CHARS]
        relative = path.relative_to(project_root).as_posix()
        yield CorpusDocument.create(relative, text)


def build_corpus(
    root: str | Path,
    output_dir: str | Path,
    *,
    sources: Sequence[str] = DEFAULT_SOURCES,
    suffixes: Sequence[str] = DEFAULT_SUFFIXES,
    val_permille: int = 5,
    tokenizer: Any = None,
) -> dict:
    """建立 train/val JSONL 與 manifest；同一內容只保留一次。

    manifest 帶資料集治理欄位：``dataset_id``（內容定址）、
    ``dataset_version``、``license``（本管線僅收第一方來源）、
    ``language``；傳入 ``tokenizer`` 時另計 ``token_count``。
    """
    project_root = Path(root).resolve()
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    train_path = target / "train.jsonl"
    val_path = target / "val.jsonl"
    documents = 0
    duplicates = 0
    train_documents = 0
    val_documents = 0
    characters = 0
    train_characters = 0
    val_characters = 0
    source_counts: dict[str, int] = {}

    with train_path.open("w", encoding="utf-8") as train_handle, val_path.open(
        "w", encoding="utf-8"
    ) as val_handle:
        for document in iter_documents(
            project_root, sources=sources, suffixes=suffixes
        ):
            if document.sha256 in seen:
                duplicates += 1
                continue
            seen.add(document.sha256)
            documents += 1
            characters += len(document.text)
            source_counts[document.source] = source_counts.get(document.source, 0) + 1

            is_validation = (
                int(document.sha256[:8], 16) % 1000 < val_permille
            )
            record = json.dumps(
                {
                    "source": document.source,
                    "sha256": document.sha256,
                    "text": document.text,
                },
                ensure_ascii=False,
            )
            if is_validation:
                val_handle.write(record + "\n")
                val_documents += 1
                val_characters += len(document.text)
            else:
                train_handle.write(record + "\n")
                train_documents += 1
                train_characters += len(document.text)

    train_sha = _file_sha256(train_path)
    val_sha = _file_sha256(val_path)
    manifest = {
        "created_at": _iso_now(),
        "dataset_format": "star-corpus/v1",
        "dataset_id": f"star-corpus-{train_sha[:12]}",
        "license": "first-party-internal",
        "language": "zh-TW+en+code",
        "root": str(project_root),
        "sources": list(sources),
        "rejected_sources": list(_rejected_sources(sources)),
        "rejection_reasons": {
            source: "protected-governance-source"
            for source in _rejected_sources(sources)
        },
        "suffixes": list(suffixes),
        "documents": documents,
        "duplicates_skipped": duplicates,
        "characters": characters,
        "train": {
            "path": train_path.name,
            "documents": train_documents,
            "characters": train_characters,
            "sha256": train_sha,
        },
        "val": {
            "path": val_path.name,
            "documents": val_documents,
            "characters": val_characters,
            "sha256": val_sha,
        },
        "source_file_counts": dict(sorted(source_counts.items())),
    }
    if tokenizer is not None:
        manifest["token_count"] = sum(
            len(tokenizer.encode(document.text, add_bos=False, add_eos=False))
            for document in read_corpus(train_path)
        ) + sum(
            len(tokenizer.encode(document.text, add_bos=False, add_eos=False))
            for document in read_corpus(val_path)
        )
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def read_corpus(path: str | Path) -> list[CorpusDocument]:
    """讀回 JSONL 語料（train 或 val）。"""
    documents: list[CorpusDocument] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            documents.append(
                CorpusDocument(
                    source=str(record["source"]),
                    text=str(record["text"]),
                    sha256=str(record["sha256"]),
                )
            )
    return documents


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[9]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄第一方語料建置")
    parser.add_argument("--root", default=str(DEFAULT_PROJECT_ROOT))
    parser.add_argument("--output", required=True)
    parser.add_argument("--source", action="append", default=None)
    parser.add_argument("--suffix", action="append", default=None)
    parser.add_argument("--val-permille", type=int, default=5)
    args = parser.parse_args(argv)
    manifest = build_corpus(
        args.root,
        args.output,
        sources=tuple(args.source) if args.source else DEFAULT_SOURCES,
        suffixes=tuple(args.suffix) if args.suffix else DEFAULT_SUFFIXES,
        val_permille=args.val_permille,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CorpusDocument",
    "DEFAULT_PROJECT_ROOT",
    "DEFAULT_SOURCES",
    "DEFAULT_SUFFIXES",
    "build_corpus",
    "iter_documents",
    "read_corpus",
]
