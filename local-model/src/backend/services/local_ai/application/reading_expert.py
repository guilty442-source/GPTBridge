from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any, Iterable


class StarReadingExpert:
    """Bounded, source-attributed reading comprehension for supplied text."""

    MAX_DOCUMENTS = 32
    MAX_TOTAL_CHARACTERS = 500_000
    CHUNK_CHARACTERS = 1_400
    CHUNK_OVERLAP = 180
    MAX_CITATIONS = 8
    MIN_QUESTION_QUERY_COVERAGE = 0.3
    MIN_QUESTION_CHUNK_RELEVANCE = 0.08
    SUPPORTED_ACTIONS = frozenset({"summarize", "question_answer", "outline", "compare"})
    _READING_FIELDS = ("document_text", "text", "content")
    _STOP_TERMS = frozenset(
        {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "by",
            "for",
            "from",
            "in",
            "is",
            "it",
            "of",
            "on",
            "or",
            "that",
            "the",
            "this",
            "to",
            "was",
            "what",
            "when",
            "where",
            "which",
            "who",
            "why",
            "with",
            "一",
            "了",
            "之",
            "也",
            "什",
            "以",
            "何",
            "你",
            "依",
            "內",
            "其",
            "到",
            "和",
            "在",
            "如",
            "是",
            "有",
            "本",
            "根",
            "據",
            "摘",
            "文",
            "章",
            "的",
            "與",
            "要",
            "請",
            "讀",
            "這",
            "重",
            "點",
        }
    )

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

    @classmethod
    def _rank_chunks(cls, chunks: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        query_term_list = cls._terms(query)
        query_terms = set(query_term_list)
        query_frequencies = Counter(query_term_list)
        document_frequency = Counter(
            term for chunk in chunks for term in set(chunk["terms"])
        )
        chunk_count = max(1, len(chunks))
        average_length = (
            sum(int(chunk.get("term_count") or 0) for chunk in chunks) / chunk_count
            if chunks
            else 1.0
        )
        k1 = 1.5
        b = 0.75
        ranked: list[dict[str, Any]] = []
        for chunk in chunks:
            overlap = query_terms & chunk["terms"]
            coverage = len(overlap) / max(1, len(query_terms))
            frequencies = chunk.get("term_frequencies")
            term_frequencies = frequencies if isinstance(frequencies, Counter) else Counter()
            document_length = max(1, int(chunk.get("term_count") or 0))
            bm25 = 0.0
            for term in overlap:
                frequency = int(term_frequencies.get(term) or 0)
                inverse_document_frequency = math.log(
                    1
                    + (chunk_count - int(document_frequency[term]) + 0.5)
                    / (int(document_frequency[term]) + 0.5)
                )
                saturation = frequency + k1 * (
                    1 - b + b * document_length / max(1.0, average_length)
                )
                bm25 += (
                    inverse_document_frequency
                    * frequency
                    * (k1 + 1)
                    / max(0.0001, saturation)
                    * max(1, int(query_frequencies[term]))
                )
            normalized_bm25 = bm25 / (bm25 + 1.0) if bm25 > 0 else 0.0
            exact = 1.0 if query.strip() and query.casefold() in chunk["text"].casefold() else 0.0
            ranked.append(
                {
                    **chunk,
                    "relevance": round(
                        normalized_bm25 * 0.6 + coverage * 0.3 + exact * 0.1,
                        4,
                    ),
                    "bm25_score": round(bm25, 4),
                    "matched_terms": sorted(overlap)[:40],
                }
            )
        return sorted(
            ranked,
            key=lambda item: (
                -float(item["relevance"]),
                str(item["document_id"]),
                int(item["character_start"]),
            ),
        )

    @classmethod
    def _summary_sentences(
        cls, documents: list[dict[str, Any]], *, maximum: int
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        frequencies = Counter(
            term for document in documents for term in cls._terms(str(document["text"]))
        )
        for document in documents:
            spans = cls._sentence_spans(str(document["text"]))
            for index, (start, end, sentence) in enumerate(spans):
                terms = set(cls._terms(sentence))
                lexical = sum(frequencies[term] for term in terms) / max(1, len(terms))
                position = 2.0 if index == 0 else 1.0 / (1 + index * 0.05)
                records.append(
                    {
                        "document_id": document["document_id"],
                        "title": document["title"],
                        "character_start": start,
                        "character_end": end,
                        "text": sentence,
                        "score": lexical + position,
                    }
                )
        selected = sorted(records, key=lambda item: -float(item["score"]))[: maximum * 3]
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in selected:
            signature = re.sub(r"\W+", "", str(item["text"]).casefold())
            if not signature or signature in seen:
                continue
            seen.add(signature)
            unique.append(item)
            if len(unique) >= maximum:
                break
        return sorted(
            unique,
            key=lambda item: (str(item["document_id"]), int(item["character_start"])),
        )

    @staticmethod
    def _chunk_for_span(
        chunks: list[dict[str, Any]], document_id: str, start: int
    ) -> dict[str, Any] | None:
        return next(
            (
                chunk
                for chunk in chunks
                if chunk["document_id"] == document_id
                and int(chunk["character_start"]) <= start < int(chunk["character_end"])
            ),
            None,
        )

    @classmethod
    def _citation(
        cls, item: dict[str, Any], chunks: list[dict[str, Any]], citation_id: str
    ) -> dict[str, Any]:
        chunk = cls._chunk_for_span(
            chunks, str(item["document_id"]), int(item["character_start"])
        )
        return {
            "citation_id": citation_id,
            "document_id": item["document_id"],
            "title": item["title"],
            "chunk_id": chunk["chunk_id"] if chunk else "",
            "character_start": int(item["character_start"]),
            "character_end": int(item["character_end"]),
            "quote": str(item["text"])[:300],
        }

    @classmethod
    def _answer_sentences(
        cls, ranked_chunks: list[dict[str, Any]], query: str
    ) -> tuple[list[dict[str, Any]], float, float]:
        query_terms = set(cls._terms(query))
        candidates: list[dict[str, Any]] = []
        for chunk in ranked_chunks[:12]:
            for relative_start, relative_end, sentence in cls._sentence_spans(
                str(chunk["text"])
            ):
                terms = set(cls._terms(sentence))
                overlap = query_terms & terms
                coverage = len(overlap) / max(1, len(query_terms))
                if not overlap:
                    continue
                if not cls._question_anchor_supported(query, sentence):
                    continue
                candidates.append(
                    {
                        "document_id": chunk["document_id"],
                        "title": chunk["title"],
                        "character_start": int(chunk["character_start"]) + relative_start,
                        "character_end": int(chunk["character_start"]) + relative_end,
                        "text": sentence,
                        "query_coverage": coverage,
                        "score": coverage + len(overlap) / max(1, len(terms)),
                    }
                )
        candidates.sort(key=lambda item: -float(item["score"]))
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            signature = (str(candidate["document_id"]), str(candidate["text"]))
            if signature in seen:
                continue
            seen.add(signature)
            selected.append(candidate)
            if len(selected) == 3:
                break
        top_relevance = float(ranked_chunks[0]["relevance"]) if ranked_chunks else 0.0
        top_query_coverage = max(
            (float(item["query_coverage"]) for item in selected),
            default=0.0,
        )
        return selected, round(top_relevance, 4), round(top_query_coverage, 4)

    @staticmethod
    def _question_anchor_supported(query: str, sentence: str) -> bool:
        """Require the answer sentence to contain the question's core predicate.

        Character overlap alone is too permissive for Chinese: a sentence about
        a product's colour otherwise looks relevant to a question about that
        product's warranty.  The trailing content phrase is a conservative,
        deterministic anchor for extractive answers.  English and very short
        questions continue to use the normal retrieval coverage gate.
        """

        chinese = "".join(re.findall(r"[\u3400-\u9fff]+", str(query or "")))
        if not chinese:
            return True
        chinese = re.sub(r"^(?:請問|想請問|我想知道|誰是|何人是|何時|哪裡|何處)", "", chinese)
        chinese = re.sub(
            r"(?:是)?(?:多少|多久|幾天|幾年|幾月|幾日|幾個|幾筆|幾次|"
            r"幾元|幾小時|幾分鐘|哪一天|什麼|為何|怎麼|如何|誰|何人|何時|"
            r"哪裡|何處)$",
            "",
            chinese,
        )
        chinese = chinese.rstrip("的是為")
        if len(chinese) < 2:
            return True
        anchor = chinese[-2:]
        return anchor in str(sentence or "")

    @staticmethod
    def _entities(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        patterns = {
            "date": r"(?<!\d)(?:19|20)\d{2}[-/.年](?:0?[1-9]|1[0-2])(?:[-/.月](?:0?[1-9]|[12]\d|3[01])日?)?(?!\d)",
            "percentage": r"(?<!\w)[+-]?\d+(?:\.\d+)?%",
            "money": r"(?:NT\$|US\$|\$|新台幣|美元)\s?\d[\d,.]*",
            "url": r"https?://[^\s)\]>，。！？；]+",
            "email": r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])",
        }
        entities: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for document in documents:
            for entity_type, pattern in patterns.items():
                for match in re.finditer(pattern, str(document["text"]), flags=re.IGNORECASE):
                    signature = (entity_type, match.group(0).casefold())
                    if signature in seen:
                        continue
                    seen.add(signature)
                    entities.append(
                        {
                            "type": entity_type,
                            "value": match.group(0),
                            "document_id": document["document_id"],
                            "character_start": match.start(),
                        }
                    )
                    if len(entities) >= 200:
                        return entities
        return entities

    @classmethod
    def _action(cls, payload: dict[str, Any], prompt: str, document_count: int) -> str:
        specification = payload.get("reading_spec")
        configured = specification if isinstance(specification, dict) else {}
        requested = str(configured.get("action") or payload.get("reading_action") or "").casefold()
        aliases = {"summary": "summarize", "qa": "question_answer", "question": "question_answer"}
        requested = aliases.get(requested, requested)
        if requested in cls.SUPPORTED_ACTIONS:
            return requested
        if str(payload.get("question") or "").strip() or re.search(
            r"[?？]|什麼|為何|如何|多少|何時|哪裡|誰|what|why|how|when|where|who",
            prompt,
            flags=re.IGNORECASE,
        ):
            return "question_answer"
        if document_count > 1 and re.search(r"比較|差異|共同|compare|difference", prompt, re.IGNORECASE):
            return "compare"
        if re.search(r"大綱|章節|結構|outline", prompt, re.IGNORECASE):
            return "outline"
        return "summarize"

    @staticmethod
    def _bounded_integer(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def process(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("instruction") or payload.get("prompt") or "").strip()
        documents, truncated = self._documents(payload)
        if not documents:
            return {
                "ok": False,
                "error_code": "READING_CONTENT_REQUIRED",
                "message": "請提供 document_text、text、content 或 documents。",
                "network_used": False,
            }

        chunks = self._chunks(documents)
        action = self._action(payload, prompt, len(documents))
        question = str(payload.get("question") or prompt).strip()
        maximum_points = self._bounded_integer(
            payload.get("max_key_points"), 6, 1, 12
        )
        summaries = self._summary_sentences(documents, maximum=maximum_points)
        ranked = self._rank_chunks(chunks, question)
        citations: list[dict[str, Any]] = []
        answer = ""
        evidence_sufficient = True
        answer_query_coverage = 0.0

        if action == "question_answer":
            answers, relevance, answer_query_coverage = self._answer_sentences(
                ranked,
                question,
            )
            evidence_sufficient = bool(
                answers
                and relevance >= self.MIN_QUESTION_CHUNK_RELEVANCE
                and answer_query_coverage >= self.MIN_QUESTION_QUERY_COVERAGE
            )
            if evidence_sufficient:
                for index, item in enumerate(answers, start=1):
                    citation = self._citation(item, chunks, f"R{index}")
                    citations.append(citation)
                answer = " ".join(
                    f"{item['text']} [{citations[index]['citation_id']}]"
                    for index, item in enumerate(answers)
                )
            else:
                answer = "原文沒有足夠資訊回答這個問題。"
        else:
            selected = summaries[: self.MAX_CITATIONS]
            for index, item in enumerate(selected, start=1):
                citations.append(self._citation(item, chunks, f"R{index}"))
            answer = " ".join(
                f"{item['text']} [{citations[index]['citation_id']}]"
                for index, item in enumerate(selected)
            )

        outlines = [
            {
                "document_id": document["document_id"],
                "title": document["title"],
                "headings": self._outline(document),
            }
            for document in documents
        ]
        comparison = []
        if action == "compare":
            for document in documents:
                findings = [
                    item["text"]
                    for item in summaries
                    if item["document_id"] == document["document_id"]
                ][:3]
                comparison.append(
                    {
                        "document_id": document["document_id"],
                        "title": document["title"],
                        "findings": findings,
                    }
                )

        public_documents = [
            {key: value for key, value in document.items() if key != "text"}
            for document in documents
        ]
        return {
            "ok": True,
            "action": action,
            "response": answer,
            "answer": answer if action == "question_answer" else "",
            "summary": answer if action != "question_answer" else "",
            "evidence_sufficient": evidence_sufficient,
            "documents": public_documents,
            "outline": outlines,
            "comparison": comparison,
            "key_points": [item["text"] for item in summaries],
            "entities": self._entities(documents),
            "citations": citations,
            "metrics": {
                "document_count": len(documents),
                "total_characters": sum(document["character_count"] for document in documents),
                "chunk_count": len(chunks),
                "citation_count": len(citations),
                "answer_query_coverage": answer_query_coverage,
                "input_truncated": truncated,
                "maximum_documents": self.MAX_DOCUMENTS,
                "maximum_characters": self.MAX_TOTAL_CHARACTERS,
                "retrieval_method": "bm25-character-bigram",
            },
            "quality": {
                "extractive_grounding": True,
                "citations_verified_against_supplied_text": True,
                "unsupported_answer_behavior": "explicit-insufficient-evidence",
                "source_offsets_preserved": True,
            },
            "network_used": False,
            "external_model_used": False,
        }


__all__ = ["StarReadingExpert"]
