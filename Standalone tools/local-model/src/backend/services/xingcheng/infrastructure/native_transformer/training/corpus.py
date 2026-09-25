"""星澄自有語料管線：只收第一方可稽核來源，輸出 JSONL 與 manifest。

來源一律限於本 repo 內、由星澄所有者持有的文字（法典、架構文件、第一方原始碼），
每個文件都記錄來源路徑與 sha256，建立可重現、可審計的訓練語料。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

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
    "governance_rule/permission",
)

# 路徑任一段命中即排除（權限憑證／密鑰／環境檔；DG-4 排除規則）。
EXCLUDED_PATH_PARTS: frozenset[str] = frozenset(
    {"secrets", "credentials", ".env", "private"}
)

MIN_DOCUMENT_CHARS = 64
MAX_DOCUMENT_CHARS = 2_000_000

# ---------------------------------------------------------------------------
# DG-1 Corpus Registry：來源必須登錄且啟用才能進語料（fail-closed）。
# 註冊檔：runtime/settings/corpus-registry.json（settings 與其他政策檔同層）。
# ---------------------------------------------------------------------------

ALLOWED_LICENSES: frozenset[str] = frozenset({"first-party-internal"})
DENIED_SENSITIVITY: frozenset[str] = frozenset(
    {"confidential", "secret", "restricted"}
)

CORPUS_REGISTRY_RELATIVE = "runtime/settings/corpus-registry.json"


def _default_registry_entries() -> list[dict[str, Any]]:
    """DEFAULT_SOURCES 的內建登錄檔（第一方內部授權）。"""
    return [
        {
            "source_id": f"builtin-{source.replace('/', '-')}",
            "path": source,
            "owner": "gptbridge",
            "license": "first-party-internal",
            "language": "auto",
            "sensitivity": "internal",
            "enabled": True,
        }
        for source in DEFAULT_SOURCES
    ]


def _tool_root() -> Path:
    return Path(__file__).resolve().parents[5]


def load_corpus_registry(root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """載入來源登錄檔；檔案不存在時以內建登錄檔補齊並落盤（可稽核）。

    回傳 {normalized_path_prefix: entry}。entry 鍵：source_id、path、
    owner、license、language、sensitivity、enabled。
    """
    registry_path = _tool_root() / CORPUS_REGISTRY_RELATIVE
    entries: list[dict[str, Any]] = []
    if registry_path.is_file():
        try:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
            entries = list(payload.get("sources") or [])
        except (OSError, json.JSONDecodeError):
            entries = []
    known = {
        str(e.get("path", "")).replace("\\", "/").strip("/") for e in entries
    }
    seeded = False
    for entry in _default_registry_entries():
        if entry["path"] not in known:
            entries.append(entry)
            seeded = True
    if not registry_path.is_file() or seeded:
        try:
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text(
                json.dumps(
                    {"format": "star-corpus-registry/v1", "sources": entries},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return {
        str(e.get("path", "")).replace("\\", "/").strip("/"): e
        for e in entries
        if e.get("path")
    }


def _registry_decision(
    registry: dict[str, dict[str, Any]], source: str
) -> tuple[bool, str]:
    """DG-1/DG-5：未登錄、停用、未授權或敏感級來源一律拒收。"""
    normalized = str(source).replace("\\", "/").strip("/")
    entry = registry.get(normalized)
    if entry is None:
        return False, "unregistered-source"
    if not entry.get("enabled", False):
        return False, "source-disabled"
    if str(entry.get("license", "")) not in ALLOWED_LICENSES:
        return False, "license-not-allowed"
    if str(entry.get("sensitivity", "")).lower() in DENIED_SENSITIVITY:
        return False, "sensitivity-denied"
    return True, ""


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
    """單一語料文件（已正規化）。

    DG-3/DG-6 欄位：``language`` 逐文件語言分類；``raw_sha256`` 為
    正規化前內容雜湊（原始 ↔ 正規化映射證據）；``simhash`` 供
    DG-7 近似去重與 DG-9 同源防洩漏分群。
    """

    source: str
    text: str
    sha256: str
    language: str = "general"
    raw_sha256: str = ""
    line_hashes: frozenset[int] = frozenset()
    source_id: str = ""

    @classmethod
    def create(cls, source: str, text: str) -> "CorpusDocument":
        return cls(source=source, text=text, sha256=_sha256_text(text))


_CODE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py", ".ts", ".tsx", ".sql", ".c", ".h", ".cpp", ".cs", ".fs",
        ".js", ".json",
    }
)


def classify_language(source: str, text: str) -> str:
    """DG-3 逐文件語言分類（確定性規則，可重現）。

    code：程式／資料檔後綴；zh-TW：CJK 字元占比 ≥15%；en：其餘以
    拉丁字母為主的文本；general：皆不符合（極短或符號為主）。
    """
    suffix = Path(source).suffix.casefold()
    if suffix in _CODE_SUFFIXES:
        return "code"
    if not text:
        return "general"
    sample = text[:4096]
    cjk = sum(1 for ch in sample if "一" <= ch <= "鿿")
    latin = sum(1 for ch in sample if "a" <= ch.lower() <= "z")
    total = len(sample)
    if cjk / total >= 0.15:
        return "zh-TW"
    if latin / total >= 0.5:
        return "en"
    return "general"


def _line_hashes(text: str) -> frozenset[int]:
    """DG-7 特徵集：有意義行（≥8 非空白字元）的 64-bit 雜湊集合。

    以「行」為特徵單位——程式碼的縮排字元 n-gram 會主導 simhash，
    行級雜湊集合的 Jaccard 才是內容相似度的誠實度量。
    """
    hashes: set[int] = set()
    for line in text.split("\n"):
        stripped = line.strip()
        if len(stripped) < 8:
            continue
        hashes.add(
            int.from_bytes(
                hashlib.blake2b(stripped.encode("utf-8"), digest_size=8).digest(),
                "little",
            )
        )
    return frozenset(hashes)


NEAR_DUP_JACCARD_THRESHOLD = 0.85


def find_near_duplicates(
    documents: Sequence[CorpusDocument],
    *,
    threshold: float = NEAR_DUP_JACCARD_THRESHOLD,
) -> dict[str, str]:
    """近似去重：行集合 Jaccard ≥ threshold 視為近似重複。

    回傳 {被捨棄來源: 保留來源}，決定性（依來源排序處理）。
    長度比 <0.5 或 >2 的文件不互相比較——近似重複必等長級。
    DG-9 防洩漏：近似重複整體剔除，不會跨 split 殘留。
    """
    ordered = sorted(documents, key=lambda d: (len(d.text), d.source))
    dropped: dict[str, str] = {}
    kept: list[CorpusDocument] = []
    for document in ordered:
        if not document.line_hashes:
            kept.append(document)
            continue
        duplicate_of: str | None = None
        doc_len = len(document.text)
        for existing in kept:
            if not existing.line_hashes:
                continue
            ratio = len(existing.text) / doc_len if doc_len else 1.0
            if not (0.5 <= ratio <= 2.0):
                continue
            inter = len(document.line_hashes & existing.line_hashes)
            if not inter:
                continue
            union = len(document.line_hashes) + len(existing.line_hashes) - inter
            if inter / union >= threshold:
                duplicate_of = existing.source
                break
        if duplicate_of is None:
            kept.append(document)
        else:
            dropped[document.source] = duplicate_of
    return dropped


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
            if any(part in EXCLUDED_PATH_PARTS for part in path.parts):
                continue
            yield path


def iter_documents(
    root: str | Path,
    *,
    sources: Sequence[str] = DEFAULT_SOURCES,
    suffixes: Sequence[str] = DEFAULT_SUFFIXES,
    enforce_registry: bool = True,
    rejections: dict[str, str] | None = None,
) -> Iterator[CorpusDocument]:
    """逐一讀出正規化後的第一方文件；無法讀取或過短者跳過。

    DG-1/DG-5：``enforce_registry`` 時，未登錄／停用／未授權／敏感級
    來源整體拒收（fail-closed），拒收原因寫入 ``rejections``。
    DG-6：非 code 文件套用 NFC 正規化並保留 raw_sha256 映射；
    code 文件不做 NFC（程式碼、路徑、專有名詞不得改寫）。
    """
    project_root = Path(root).resolve()
    registry = load_corpus_registry(project_root) if enforce_registry else {}
    admitted: list[str] = []
    protected = set(_rejected_sources(sources))
    for source in sources:
        normalized = str(source).replace("\\", "/").strip("/")
        if normalized in protected:
            if rejections is not None:
                rejections[normalized] = "protected-governance-source"
            continue
        if not enforce_registry:
            admitted.append(source)
            continue
        ok, reason = _registry_decision(registry, normalized)
        if ok:
            admitted.append(source)
        elif rejections is not None:
            rejections[normalized] = reason
    for path in _iter_files(project_root, admitted, suffixes):
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
        language = classify_language(relative, text)
        raw_sha = _sha256_text(text)
        normalized_text = text if language == "code" else unicodedata.normalize("NFC", text)
        normalized_sha = _sha256_text(normalized_text)
        source_key = relative.split("/", 1)[0] if "/" in relative else relative
        yield CorpusDocument(
            source=relative,
            text=normalized_text,
            sha256=normalized_sha,
            language=language,
            raw_sha256=raw_sha,
            line_hashes=_line_hashes(normalized_text),
            source_id=source_key,
        )


def build_corpus(
    root: str | Path,
    output_dir: str | Path,
    *,
    sources: Sequence[str] = DEFAULT_SOURCES,
    suffixes: Sequence[str] = DEFAULT_SUFFIXES,
    val_permille: int = 5,
    tokenizer: Any = None,
    tokenizer_dir: str | Path | None = None,
    enforce_registry: bool = True,
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

    rejections: dict[str, str] = {}
    kept_documents = list(
        iter_documents(
            project_root,
            sources=sources,
            suffixes=suffixes,
            enforce_registry=enforce_registry,
            rejections=rejections,
        )
    )

    # DG-7：精確去重（sha256）＋近似去重（simhash 漢明距離）。
    seen: set[str] = set()
    unique_documents: list[CorpusDocument] = []
    for document in kept_documents:
        if document.sha256 in seen:
            duplicates += 1
            continue
        seen.add(document.sha256)
        unique_documents.append(document)
    near_dup_map = find_near_duplicates(unique_documents)
    admitted_documents = [
        d for d in unique_documents if d.source not in near_dup_map
    ]

    language_stats: dict[str, int] = {}
    normalized_changed = 0
    with train_path.open("w", encoding="utf-8") as train_handle, val_path.open(
        "w", encoding="utf-8"
    ) as val_handle:
        for document in admitted_documents:
            documents += 1
            characters += len(document.text)
            source_counts[document.source] = source_counts.get(document.source, 0) + 1
            language_stats[document.language] = (
                language_stats.get(document.language, 0) + 1
            )
            if document.raw_sha256 and document.raw_sha256 != document.sha256:
                normalized_changed += 1

            # DG-9：固定演算法（sha256 前 8 碼 mod 1000 < permille），
            # 近似重複已在 DG-7 整體剔除，同源文件天然不跨 split。
            is_validation = (
                int(document.sha256[:8], 16) % 1000 < val_permille
            )
            record_payload: dict[str, Any] = {
                "source": document.source,
                "sha256": document.sha256,
                "language": document.language,
                "text": document.text,
            }
            if document.raw_sha256 != document.sha256:
                record_payload["raw_sha256"] = document.raw_sha256
            record = json.dumps(record_payload, ensure_ascii=False)
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
    # DG-11：資料集根雜湊＝train/val 內容雜湊＋文件數的複合摘要。
    dataset_root_sha = _sha256_text(
        f"train:{train_sha}\nval:{val_sha}\ndocuments:{documents}"
    )
    manifest = {
        "created_at": _iso_now(),
        "dataset_format": "star-corpus/v2",
        "dataset_id": f"star-corpus-{train_sha[:12]}",
        "dataset_version": "corpus-v2",
        "dataset_root_sha256": dataset_root_sha,
        "license": "first-party-internal",
        "language": "zh-TW+en+code",
        "language_stats": dict(sorted(language_stats.items())),
        "root": str(project_root),
        "sources": list(sources),
        "rejected_sources": sorted(rejections),
        "rejection_reasons": dict(sorted(rejections.items())),
        "suffixes": list(suffixes),
        "documents": documents,
        "duplicates_skipped": duplicates,
        "near_duplicates_skipped": len(near_dup_map),
        "near_duplicate_map": dict(sorted(near_dup_map.items())),
        "nfc_normalized_documents": normalized_changed,
        "split": {
            "algorithm": "sha256[:8] mod 1000 < val_permille",
            "val_permille": val_permille,
            "leakage_guard": "near-duplicates removed before split; "
            "each document is a whole source file",
        },
        "packing": {
            "applied_at": "training-time (pretrain.py pack_blocks)",
            "block_size_default": 512,
            "eos_strategy": "document-boundary",
        },
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
        if tokenizer_dir is not None:
            manifest["tokenizer_sha256"] = _tokenizer_dir_sha256(tokenizer_dir)
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _tokenizer_dir_sha256(tokenizer_dir: str | Path) -> str:
    """DG-10：綁定 tokenizer 資產雜湊（目錄內所有資產檔）。"""
    directory = Path(tokenizer_dir)
    digest = hashlib.sha256()
    for asset in sorted(directory.glob("*")):
        if asset.is_file():
            digest.update(asset.name.encode("utf-8"))
            digest.update(_file_sha256(asset).encode("utf-8"))
    return digest.hexdigest()


def verify_corpus(output_dir: str | Path) -> dict[str, Any]:
    """DG-11 完整性驗證：重算 train/val 雜湊與資料集根雜湊對比 manifest。"""
    target = Path(output_dir)
    manifest_path = target / "manifest.json"
    report: dict[str, Any] = {"ok": False, "checks": {}}
    if not manifest_path.is_file():
        report["checks"]["manifest"] = "missing"
        return report
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ok = True
    for split in ("train", "val"):
        info = manifest.get(split) or {}
        path = target / str(info.get("path", f"{split}.jsonl"))
        if not path.is_file():
            report["checks"][split] = "missing"
            ok = False
            continue
        actual_sha = _file_sha256(path)
        match = actual_sha == info.get("sha256")
        report["checks"][f"{split}_sha256"] = "match" if match else "MISMATCH"
        ok = ok and match
    documents = int(manifest.get("documents") or 0)
    expected_root = _sha256_text(
        f"train:{manifest['train']['sha256']}\n"
        f"val:{manifest['val']['sha256']}\n"
        f"documents:{documents}"
    )
    root_match = expected_root == manifest.get("dataset_root_sha256")
    report["checks"]["dataset_root_sha256"] = "match" if root_match else "MISMATCH"
    ok = ok and root_match
    report["ok"] = ok
    report["dataset_id"] = manifest.get("dataset_id")
    report["dataset_version"] = manifest.get("dataset_version")
    return report


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
    parser.add_argument("--output", default=None)
    parser.add_argument("--source", action="append", default=None)
    parser.add_argument("--suffix", action="append", default=None)
    parser.add_argument("--val-permille", type=int, default=5)
    parser.add_argument(
        "--tokenizer",
        default=None,
        help="tokenizer.json 所在目錄；提供時計算 token_count 並綁定 sha256",
    )
    parser.add_argument(
        "--verify",
        default=None,
        metavar="CORPUS_DIR",
        help="驗證既有語料完整性（DG-11），不建置",
    )
    args = parser.parse_args(argv)
    if args.verify:
        report = verify_corpus(args.verify)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.get("ok") else 1
    if not args.output:
        parser.error("--output is required unless --verify is used")
    tokenizer = None
    if args.tokenizer:
        from ..bpe import NativeBPETokenizer

        tokenizer = NativeBPETokenizer.load(args.tokenizer)
    manifest = build_corpus(
        args.root,
        args.output,
        sources=tuple(args.source) if args.source else DEFAULT_SOURCES,
        suffixes=tuple(args.suffix) if args.suffix else DEFAULT_SUFFIXES,
        val_permille=args.val_permille,
        tokenizer=tokenizer,
        tokenizer_dir=args.tokenizer,
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
    "classify_language",
    "find_near_duplicates",
    "iter_documents",
    "load_corpus_registry",
    "read_corpus",
    "verify_corpus",
]
