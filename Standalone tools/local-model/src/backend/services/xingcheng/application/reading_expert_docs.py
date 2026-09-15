from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any, Iterable




class ReadingExpertDocsMixin:

    @classmethod
    def has_readable_content(cls, payload: dict[str, Any]) -> bool:
        if any(str(payload.get(field) or "").strip() for field in cls._READING_FIELDS):
            return True
        documents = payload.get("documents")
        return isinstance(documents, list) and any(
            bool(str(item if isinstance(item, str) else item.get("text") or item.get("content") or "").strip())
            for item in documents
            if isinstance(item, (str, dict))
        )

    @staticmethod
    def _normalize_text(value: Any) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[\t\f\v]+", " ", text)
        text = re.sub(r"[ ]{2,}", " ", text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip()

    @classmethod
    def _documents(cls, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        candidates: list[Any] = []
        supplied = payload.get("documents")
        if isinstance(supplied, list):
            candidates.extend(supplied[: cls.MAX_DOCUMENTS])
        if not candidates:
            for field in cls._READING_FIELDS:
                if str(payload.get(field) or "").strip():
                    candidates.append(
                        {
                            "id": payload.get("document_id") or "document-1",
                            "title": payload.get("title") or "提供的文件",
                            "text": payload[field],
                        }
                    )
                    break

        documents: list[dict[str, Any]] = []
        used_document_ids: set[str] = set()
        remaining = cls.MAX_TOTAL_CHARACTERS
        truncated = len(supplied) > cls.MAX_DOCUMENTS if isinstance(supplied, list) else False
        for index, candidate in enumerate(candidates, start=1):
            record = candidate if isinstance(candidate, dict) else {"text": candidate}
            text = cls._normalize_text(record.get("text") or record.get("content"))
            if not text or remaining <= 0:
                continue
            accepted = text[:remaining]
            truncated = truncated or len(accepted) < len(text)
            document_id = re.sub(
                r"[^A-Za-z0-9_.-]+", "-", str(record.get("id") or f"document-{index}")
            ).strip("-")[:80] or f"document-{index}"
            base_document_id = document_id
            duplicate_sequence = 2
            while document_id in used_document_ids:
                document_id = f"{base_document_id[:72]}-{duplicate_sequence}"
                duplicate_sequence += 1
            used_document_ids.add(document_id)
            documents.append(
                {
                    "document_id": document_id,
                    "title": str(record.get("title") or f"文件 {index}").strip()[:200],
                    "text": accepted,
                    "character_count": len(accepted),
                    "sha256": hashlib.sha256(accepted.encode("utf-8")).hexdigest(),
                }
            )
            remaining -= len(accepted)
        return documents, truncated

    @classmethod
    def _terms(cls, text: str) -> list[str]:
        normalized = str(text or "").casefold()
        latin = re.findall(r"[a-z][a-z0-9_-]{1,}|\d+(?:\.\d+)?%?", normalized)
        chinese_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
        chinese: list[str] = []
        for run in chinese_runs:
            chinese.extend(character for character in run if character not in cls._STOP_TERMS)
            chinese.extend(run[index : index + 2] for index in range(len(run) - 1))
        return [term for term in latin + chinese if term not in cls._STOP_TERMS]

    @staticmethod
    def _sentence_spans(text: str) -> list[tuple[int, int, str]]:
        spans: list[tuple[int, int, str]] = []
        for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text):
            sentence = match.group(0).strip()
            if len(sentence) < 2:
                continue
            leading = len(match.group(0)) - len(match.group(0).lstrip())
            spans.append((match.start() + leading, match.end(), sentence))
        return spans

    @classmethod
    def _chunks(cls, documents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for document in documents:
            text = str(document["text"])
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
                chunk_text = raw.strip()
                if chunk_text:
                    terms = cls._terms(chunk_text)
                    chunks.append(
                        {
                            "chunk_id": f"{document['document_id']}-chunk-{sequence}",
                            "document_id": document["document_id"],
                            "title": document["title"],
                            "character_start": start + leading,
                            "character_end": end,
                            "text": chunk_text,
                            "terms": set(terms),
                            "term_frequencies": Counter(terms),
                            "term_count": len(terms),
                        }
                    )
                    sequence += 1
                if end >= len(text):
                    break
                start = max(start + 1, end - cls.CHUNK_OVERLAP)
        return chunks

    @staticmethod
    def _outline(document: dict[str, Any]) -> list[dict[str, Any]]:
        patterns = (
            re.compile(r"^(#{1,6})\s+(.+)$"),
            re.compile(r"^第[一二三四五六七八九十百零〇0-9]+[章節]\s*(.*)$"),
            re.compile(r"^(\d+(?:\.\d+)*)[、.)．]\s*(.+)$"),
        )
        outline: list[dict[str, Any]] = []
        offset = 0
        for line in str(document["text"]).splitlines(keepends=True):
            stripped = line.strip()
            for pattern in patterns:
                match = pattern.match(stripped)
                if match:
                    level = len(match.group(1)) if match.group(1).startswith("#") else 1
                    title = match.group(match.lastindex or 1).strip() or stripped
                    outline.append(
                        {
                            "level": level,
                            "title": title[:200],
                            "character_start": offset + max(0, line.find(stripped)),
                        }
                    )
                    break
            offset += len(line)
        return outline[:100]
