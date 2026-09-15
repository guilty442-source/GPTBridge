from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any, Iterable


from .reading_expert_docs import ReadingExpertDocsMixin
from .reading_expert_rank import ReadingExpertRankMixin


class StarReadingExpert(ReadingExpertDocsMixin, ReadingExpertRankMixin):
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
        answer, citations, evidence_sufficient, answer_query_coverage = (
            self._answer_with_citations(action, ranked, summaries, chunks, question)
        )
        return self._process_result(
            action,
            answer,
            documents,
            chunks,
            summaries,
            citations,
            evidence_sufficient,
            answer_query_coverage,
            truncated,
        )

    def _answer_with_citations(
        self,
        action: str,
        ranked: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        question: str,
    ) -> tuple[str, list[dict[str, Any]], bool, float]:
        citations: list[dict[str, Any]] = []
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
        return answer, citations, evidence_sufficient, answer_query_coverage

    def _process_result(
        self,
        action: str,
        answer: str,
        documents: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        evidence_sufficient: bool,
        answer_query_coverage: float,
        truncated: bool,
    ) -> dict[str, Any]:
        outlines = [
            {
                "document_id": document["document_id"],
                "title": document["title"],
                "headings": self._outline(document),
            }
            for document in documents
        ]
        comparison = self._comparison_findings(action, documents, summaries)
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
            "metrics": self._process_metrics(
                documents, chunks, citations, answer_query_coverage, truncated
            ),
            "quality": {
                "extractive_grounding": True,
                "citations_verified_against_supplied_text": True,
                "unsupported_answer_behavior": "explicit-insufficient-evidence",
                "source_offsets_preserved": True,
            },
            "network_used": False,
            "external_model_used": False,
        }

    def _process_metrics(
        self,
        documents: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        answer_query_coverage: float,
        truncated: bool,
    ) -> dict[str, Any]:
        return {
            "document_count": len(documents),
            "total_characters": sum(document["character_count"] for document in documents),
            "chunk_count": len(chunks),
            "citation_count": len(citations),
            "answer_query_coverage": answer_query_coverage,
            "input_truncated": truncated,
            "maximum_documents": self.MAX_DOCUMENTS,
            "maximum_characters": self.MAX_TOTAL_CHARACTERS,
            "retrieval_method": "bm25-character-bigram",
        }

    @staticmethod
    def _comparison_findings(
        action: str,
        documents: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
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
        return comparison


__all__ = ["StarReadingExpert"]
