from __future__ import annotations

import csv
import html
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree


class LocalRagDocumentsMixin:
    """Document ingestion, text normalization, path validation, and chunking."""

    @staticmethod
    def _normalize_text(value: Any) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[\t\f\v]+", " ", text)
        text = re.sub(r"[ ]{2,}", " ", text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip()

    def _allowed_path(self, raw_path: Any) -> Path:
        path = Path(str(raw_path or "").strip())
        if not path.is_absolute():
            path = self.project_root / path
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise PermissionError("RAG_PATH_OUTSIDE_PROJECT") from exc
        if "governance_rule" in {part.casefold() for part in relative.parts}:
            raise PermissionError("RAG_GOVERNANCE_PATH_DENIED")
        return resolved

    def _path_files(self, values: Iterable[Any]) -> tuple[list[Path], list[dict[str, str]]]:
        files: list[Path] = []
        errors: list[dict[str, str]] = []
        for value in values:
            try:
                path = self._allowed_path(value)
            except (OSError, PermissionError, ValueError) as exc:
                errors.append({"path": str(value), "error": str(exc)})
                continue
            candidates = (
                [path]
                if path.is_file()
                else sorted(item for item in path.rglob("*") if item.is_file())
                if path.is_dir()
                else []
            )
            if not candidates:
                errors.append({"path": str(path), "error": "RAG_PATH_NOT_FOUND"})
            for candidate in candidates:
                try:
                    checked = self._allowed_path(candidate)
                except (OSError, PermissionError, ValueError) as exc:
                    errors.append({"path": str(candidate), "error": str(exc)})
                    continue
                if checked.suffix.casefold() in self.SUPPORTED_SUFFIXES and checked not in files:
                    files.append(checked)
                if len(files) >= self.MAX_FILES:
                    return files, errors
        return files, errors

    @staticmethod
    def _docx_text(data: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        paragraphs: list[str] = []
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        for paragraph in root.iter(namespace + "p"):
            content = "".join(
                node.text or "" for node in paragraph.iter(namespace + "t")
            ).strip()
            if content:
                paragraphs.append(content)
        return "\n\n".join(paragraphs)

    @staticmethod
    def _tabular_text(data: str, delimiter: str) -> str:
        rows = csv.reader(io.StringIO(data), delimiter=delimiter)
        return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)

    def _read_path(self, path: Path) -> str:
        if path.stat().st_size > self.MAX_FILE_BYTES:
            raise ValueError("RAG_FILE_TOO_LARGE")
        data = path.read_bytes()
        suffix = path.suffix.casefold()
        if suffix == ".docx":
            return self._normalize_text(self._docx_text(data))
        decoded = data.decode("utf-8-sig", errors="replace")
        if suffix == ".csv":
            decoded = self._tabular_text(decoded, ",")
        elif suffix == ".tsv":
            decoded = self._tabular_text(decoded, "\t")
        elif suffix in {".html", ".htm", ".xml"}:
            decoded = html.unescape(re.sub(r"<[^>]+>", " ", decoded))
        return self._normalize_text(decoded)

    def _documents(self, payload: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        records: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        supplied = payload.get("documents")
        if isinstance(supplied, list):
            for index, item in enumerate(supplied[: self.MAX_FILES], start=1):
                record = item if isinstance(item, dict) else {"text": item}
                text = self._normalize_text(record.get("text") or record.get("content"))
                if not text:
                    continue
                source = str(record.get("source") or record.get("id") or f"inline-{index}")[:500]
                records.append(
                    {
                        "source": source,
                        "title": str(record.get("title") or source)[:240],
                        "text": text[: self.MAX_DOCUMENT_CHARACTERS],
                    }
                )
        raw_paths = payload.get("paths")
        if not isinstance(raw_paths, list):
            raw_path = payload.get("path")
            raw_paths = [raw_path] if str(raw_path or "").strip() else []
        files, path_errors = self._path_files(raw_paths)
        errors.extend(path_errors)
        for path in files:
            try:
                text = self._read_path(path)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                errors.append({"path": str(path), "error": str(exc)})
                continue
            if not text:
                errors.append({"path": str(path), "error": "RAG_DOCUMENT_EMPTY"})
                continue
            records.append(
                {
                    "source": path.relative_to(self.project_root).as_posix(),
                    "title": path.name[:240],
                    "text": text[: self.MAX_DOCUMENT_CHARACTERS],
                }
            )
        return records[: self.MAX_FILES], errors

    @classmethod
    def _chunks(cls, text: str) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        start = 0
        sequence = 1
        while start < len(text):
            end = min(len(text), start + cls.CHUNK_CHARACTERS)
            if end < len(text):
                boundary = max(
                    text.rfind("\n", start + cls.CHUNK_CHARACTERS // 2, end),
                    text.rfind("。", start + cls.CHUNK_CHARACTERS // 2, end),
                    text.rfind(".", start + cls.CHUNK_CHARACTERS // 2, end),
                )
                if boundary > start:
                    end = boundary + 1
            raw = text[start:end]
            leading = len(raw) - len(raw.lstrip())
            content = raw.strip()
            if content:
                chunks.append(
                    {
                        "sequence": sequence,
                        "character_start": start + leading,
                        "character_end": end,
                        "content": content,
                    }
                )
                sequence += 1
            if end >= len(text):
                break
            start = max(start + 1, end - cls.CHUNK_OVERLAP)
        return chunks
