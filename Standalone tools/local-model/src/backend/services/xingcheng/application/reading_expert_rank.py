from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any, Iterable




class ReadingExpertRankMixin:

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
        ranked: list[dict[str, Any]] = []
        for chunk in chunks:
            bm25, overlap, coverage = cls._bm25_chunk_score(
                chunk,
                query_terms,
                query_frequencies,
                document_frequency,
                chunk_count,
                average_length,
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

    @staticmethod
    def _bm25_chunk_score(
        chunk: dict[str, Any],
        query_terms: set[str],
        query_frequencies: Counter,
        document_frequency: Counter,
        chunk_count: int,
        average_length: float,
    ) -> tuple[float, set[str], float]:
        k1 = 1.5
        b = 0.75
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
        return bm25, overlap, coverage

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
