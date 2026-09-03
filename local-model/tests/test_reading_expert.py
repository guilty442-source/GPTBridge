from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

from xingcheng.application.reading_expert import StarReadingExpert
from xingcheng.application.service import LocalAiService
from xingcheng.infrastructure.native_model import StarNativeLanguageModel


DOCUMENT = """# 星澄年度計畫

星澄閱讀能力專案於2026年8月26日啟動，核定預算為NT$500,000。

## 執行目標

第一階段會加入長文分段、摘要與原文問答。所有答案必須附上來源段落，原文沒有答案時不得猜測。

## 驗收標準

專案必須通過自動化測試，並保存文件雜湊與引用位置。
"""


def test_reading_expert_summarizes_with_verified_citations() -> None:
    result = StarReadingExpert().process(
        {"prompt": "請摘要這篇文章的重點", "document_text": DOCUMENT}
    )

    assert result["ok"] is True
    assert result["action"] == "summarize"
    assert result["summary"]
    assert result["citations"]
    assert result["quality"]["extractive_grounding"] is True
    for citation in result["citations"]:
        assert citation["quote"] in DOCUMENT
        assert DOCUMENT[
            citation["character_start"] : citation["character_end"]
        ].strip() == citation["quote"]


def test_reading_expert_answers_question_from_source() -> None:
    result = StarReadingExpert().process(
        {
            "prompt": "預算是多少？",
            "question": "星澄閱讀能力專案的預算是多少？",
            "document_text": DOCUMENT,
        }
    )

    assert result["ok"] is True
    assert result["action"] == "question_answer"
    assert result["evidence_sufficient"] is True
    assert "NT$500,000" in result["answer"]
    assert "[R1]" in result["answer"]


def test_reading_expert_abstains_when_source_has_no_answer() -> None:
    result = StarReadingExpert().process(
        {
            "prompt": "負責人的電話號碼是多少？",
            "question": "負責人的電話號碼是多少？",
            "document_text": DOCUMENT,
        }
    )

    assert result["ok"] is True
    assert result["evidence_sufficient"] is False
    assert result["answer"] == "原文沒有足夠資訊回答這個問題。"
    assert result["citations"] == []


def test_reading_expert_rejects_partial_topic_overlap_as_answer_evidence() -> None:
    cases = [
        ("產品保固多久？", "產品顏色是藍色。"),
        ("公司去年營收是多少？", "公司去年推出新產品。"),
        ("誰是專案負責人？", "專案預計週五完成。"),
        ("退款期限是幾天？", "本店提供退款服務。"),
    ]

    for question, document_text in cases:
        result = StarReadingExpert().process(
            {"question": question, "document_text": document_text}
        )
        assert result["evidence_sufficient"] is False
        assert result["citations"] == []
        assert result["metrics"]["answer_query_coverage"] < 0.5


def test_reading_expert_accepts_answer_with_specific_query_coverage() -> None:
    result = StarReadingExpert().process(
        {
            "question": "產品保固多久？",
            "document_text": "產品保固期限為兩年。",
        }
    )

    assert result["evidence_sufficient"] is True
    assert "兩年" in result["answer"]
    assert result["citations"]
    assert result["metrics"]["answer_query_coverage"] >= 0.5


def test_reading_expert_extracts_outline_and_entities() -> None:
    result = StarReadingExpert().process(
        {
            "prompt": "整理文件大綱",
            "reading_action": "outline",
            "document_text": DOCUMENT,
        }
    )

    headings = result["outline"][0]["headings"]
    entities = {(item["type"], item["value"]) for item in result["entities"]}
    assert [item["title"] for item in headings] == [
        "星澄年度計畫",
        "執行目標",
        "驗收標準",
    ]
    assert ("date", "2026年8月26日") in entities
    assert ("money", "NT$500,000") in entities


def test_reading_expert_compares_multiple_documents() -> None:
    result = StarReadingExpert().process(
        {
            "prompt": "比較兩份文件的差異",
            "documents": [
                {"id": "plan-a", "title": "方案 A", "text": "方案A採用每日備份。成本為NT$10,000。"},
                {"id": "plan-b", "title": "方案 B", "text": "方案B採用每週備份。成本為NT$6,000。"},
            ],
        }
    )

    assert result["ok"] is True
    assert result["action"] == "compare"
    assert result["metrics"]["document_count"] == 2
    assert {item["document_id"] for item in result["comparison"]} == {
        "plan-a",
        "plan-b",
    }
    assert all(item["findings"] for item in result["comparison"])


def test_reading_expert_enforces_document_and_character_bounds(monkeypatch) -> None:
    monkeypatch.setattr(StarReadingExpert, "MAX_DOCUMENTS", 2)
    monkeypatch.setattr(StarReadingExpert, "MAX_TOTAL_CHARACTERS", 40)
    result = StarReadingExpert().process(
        {
            "prompt": "摘要",
            "documents": [
                {"id": "a", "text": "甲" * 30},
                {"id": "b", "text": "乙" * 30},
                {"id": "c", "text": "丙" * 30},
            ],
        }
    )

    assert result["ok"] is True
    assert result["metrics"]["document_count"] == 2
    assert result["metrics"]["total_characters"] == 40
    assert result["metrics"]["input_truncated"] is True


def test_reading_expert_finds_answer_across_overlapping_chunks(monkeypatch) -> None:
    monkeypatch.setattr(StarReadingExpert, "CHUNK_CHARACTERS", 120)
    monkeypatch.setattr(StarReadingExpert, "CHUNK_OVERLAP", 30)
    long_document = (
        "背景資料。" * 25
        + "關鍵決議是將正式發布日期定為2026年12月15日。"
        + "後續說明。" * 25
    )
    result = StarReadingExpert().process(
        {
            "prompt": "正式發布日期是哪一天？",
            "question": "正式發布日期是哪一天？",
            "document_text": long_document,
        }
    )

    assert result["metrics"]["chunk_count"] > 1
    assert result["evidence_sufficient"] is True
    assert "2026年12月15日" in result["answer"]
    assert result["citations"][0]["quote"] in long_document
    assert result["metrics"]["retrieval_method"] == "bm25-character-bigram"


def test_reading_expert_assigns_unique_ids_to_duplicate_document_ids() -> None:
    result = StarReadingExpert().process(
        {
            "prompt": "比較文件",
            "reading_action": "compare",
            "documents": [
                {"id": "same", "text": "第一份文件使用藍色標籤。"},
                {"id": "same", "text": "第二份文件使用綠色標籤。"},
            ],
        }
    )

    document_ids = [item["document_id"] for item in result["documents"]]
    assert document_ids == ["same", "same-2"]
    assert len({item["chunk_id"] for item in result["citations"]}) == len(
        result["citations"]
    )


def test_reading_expert_accepts_string_documents_and_invalid_point_limit() -> None:
    result = StarReadingExpert().process(
        {
            "prompt": "摘要",
            "max_key_points": "not-a-number",
            "documents": ["第一項原則是保留證據。", "第二項原則是拒絕猜測。"],
        }
    )

    assert result["ok"] is True
    assert result["metrics"]["document_count"] == 2
    assert 1 <= len(result["key_points"]) <= 6


def test_reading_expert_requires_supplied_content() -> None:
    result = StarReadingExpert().process({"prompt": "請閱讀並摘要"})

    assert result["ok"] is False
    assert result["error_code"] == "READING_CONTENT_REQUIRED"
    assert result["network_used"] is False


def test_language_model_classifies_reading_and_keeps_version_one() -> None:
    assert StarNativeLanguageModel.classify_intent("請閱讀文件並整理摘要") == "reading"
    assert StarNativeLanguageModel.VERSION == "1.0"
    assert "source-attributed-long-context-reading" in StarNativeLanguageModel.ARCHITECTURE


def test_language_model_routes_semantic_paraphrases_without_explicit_keywords() -> None:
    assert (
        StarNativeLanguageModel.classify_intent("找出兩份內容的共同觀點")
        == "reading"
    )
    assert StarNativeLanguageModel.classify_intent("建立資料接收端點") == "coding"
    assert StarNativeLanguageModel.classify_intent("確認收益何時發放") == "distribution"


def test_language_model_understands_questions_constraints_and_requested_outputs() -> None:
    plan = StarNativeLanguageModel.semantic_plan(
        "請閱讀報告並回答：為何成本增加？不得猜測，至少提供3個步驟與來源。"
        "預算為NT$25,000，截止日是2026年9月30日，聯絡信箱為team@example.com。"
    )

    comprehension = plan["comprehension"]
    assert plan["primary_intent"] == "reading"
    assert plan["language"] == "mixed-zh-latin"
    assert comprehension["questions"][0]["type"] == "reason"
    assert {item["type"] for item in comprehension["constraints"]} >= {
        "prohibited",
        "minimum",
    }
    assert comprehension["negations"]
    assert {"steps", "citations"} <= set(comprehension["requested_outputs"])
    assert comprehension["objectives"]
    assert comprehension["keywords"]
    assert "2026年9月30日" in plan["entities"]["dates"]
    assert "NT$25,000" in plan["entities"]["money"]
    assert "team@example.com" in plan["entities"]["emails"]


def test_service_routes_supplied_document_to_reading_without_keyword(tmp_path: Path) -> None:
    service = LocalAiService(tmp_path)
    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "預算是多少？",
                "document_text": DOCUMENT,
            },
        )
    )

    assert result["intent"] == "reading"
    assert result["reading_result"]["ok"] is True
    assert result["reading_result"]["evidence_sufficient"] is True
    assert "NT$500,000" in result["response"]
    assert result["generation"]["reading_extractive_grounding"] is True
    assert result["evidence"] == result["reading_result"]["citations"]
    reading_module = next(
        item
        for item in result["module_execution"]["modules"]
        if item["module_id"] == "document-reading"
    )
    assert reading_module["status"] == "completed"
    assert result["instruction_execution"]["status"] == "completed"
    assert result["semantic_understanding"]["document_understanding"][
        "document_count"
    ] == 1
    assert result["context_retrieval"]["document_chunk_count"] >= 1
    assert result["evidence_policy"]["verbatim_citation_offsets_required"] is True


def test_service_marks_missing_reading_content_as_input_required(tmp_path: Path) -> None:
    service = LocalAiService(tmp_path)
    _, result = asyncio.run(
        service.handle("xingcheng_infer", {"prompt": "請閱讀文件並回答問題"})
    )

    assert result["intent"] == "reading"
    assert result["reading_result"]["error_code"] == "READING_CONTENT_REQUIRED"
    assert result["response"] == "請提供 document_text、text、content 或 documents。"
    assert result["generation"]["reading_extractive_grounding"] is False
    assert result["self_training"]["accepted"] is False
    assert result["instruction_execution"]["status"] == "input-required"
    assert result["instruction_execution"]["missing_inputs"] == [
        "document-text-or-documents"
    ]


def test_service_reports_reading_capabilities(tmp_path: Path) -> None:
    service = LocalAiService(tmp_path)
    _, status = asyncio.run(service.handle("xingcheng_status", {}))

    assert status["model_version"] == "1.0"
    assert status["reading"]["actions"] == [
        "compare",
        "outline",
        "question_answer",
        "summarize",
    ]
    assert status["reading"]["maximum_characters"] == 500_000
    assert status["reading"]["citation_required"] is True
    assert status["reading"]["network_access"] is False


def test_star_version_remains_consistently_one(tmp_path: Path) -> None:
    manifest = json.loads((ROOT / "local-model" / "manifest.json").read_text(encoding="utf-8"))
    service = LocalAiService(tmp_path)

    assert LocalAiService.VERSION == "1.0.0"
    assert service.runtime_health()["star_version"] == "1.0"
    assert manifest["version"] == "1.0.0"
    assert manifest["display_version"] == "1.0"
    assert manifest["capabilities"]["xingcheng"]["language_model_version"] == "1.0"
    assert manifest["capabilities"]["upgrade-optimization"]["version_locked"] == "1.0"
