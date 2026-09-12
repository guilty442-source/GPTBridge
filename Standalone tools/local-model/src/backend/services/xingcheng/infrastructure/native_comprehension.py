from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


class StarNativeComprehensionMixin:
    """Comprehension feature extraction, task classification, and intensity scoring."""

    @classmethod
    def _comprehension_features(cls, text: str) -> dict[str, Any]:
        sentences = [
            match.group(0).strip()
            for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text)
            if match.group(0).strip()
        ]
        question_markers = (
            ("reason", ("為何", "為什麼", "why")),
            ("method", ("如何", "怎麼", "how")),
            ("time", ("何時", "什麼時候", "when")),
            ("location", ("哪裡", "何處", "where")),
            ("person", ("誰", "何人", "who")),
            ("quantity", ("多少", "幾個", "how many", "how much")),
            ("definition", ("什麼", "何謂", "what", "which")),
        )
        questions: list[dict[str, Any]] = []
        for sentence in sentences:
            lowered = sentence.casefold()
            matched = [
                marker
                for _, markers in question_markers
                for marker in markers
                if marker in lowered
            ]
            if "?" not in sentence and "？" not in sentence and not matched:
                continue
            question_type = next(
                (
                    kind
                    for kind, markers in question_markers
                    if any(marker in lowered for marker in markers)
                ),
                "yes_no",
            )
            questions.append(
                {
                    "type": question_type,
                    "text": sentence[:500],
                    "markers": matched,
                }
            )

        constraint_patterns = (
            ("required", r"必須|務必|一定要|需要|must\b|shall\b|required\b"),
            ("prohibited", r"不得|不可|禁止|不要|不能|do not\b|must not\b|forbid"),
            ("minimum", r"至少|不低於|at least\b|minimum\b"),
            ("maximum", r"至多|最多|不超過|at most\b|maximum\b"),
            ("exclusive", r"只能|僅限|only\b"),
        )
        constraints = [
            {
                "type": kind,
                "marker": match.group(0),
                "character_start": match.start(),
            }
            for kind, pattern in constraint_patterns
            for match in re.finditer(pattern, text, flags=re.IGNORECASE)
        ]
        negations = [
            {"marker": match.group(0), "character_start": match.start()}
            for match in re.finditer(
                r"不得|不可|不會|不是|沒有|禁止|拒絕|不能|不要|\bnot\b|\bno\b|\bnever\b|without",
                text,
                flags=re.IGNORECASE,
            )
        ]
        output_markers = {
            "summary": ("摘要", "總結", "summar"),
            "outline": ("大綱", "章節", "outline"),
            "table": ("表格", "table"),
            "code": ("程式碼", "code"),
            "citations": ("引用", "來源", "citation", "source"),
            "steps": ("步驟", "流程", "steps"),
            "comparison": ("比較", "差異", "compare"),
        }
        lowered_text = text.casefold()
        requested_outputs = [
            output
            for output, markers in output_markers.items()
            if any(marker in lowered_text for marker in markers)
        ]
        command_markers = re.compile(
            r"請|幫我|需要|務必|建立|新增|實作|產生|執行|修正|修改|檢討|檢查|分析|整理|計算|閱讀|summarize|create|implement|execute|fix|modify|analyze|calculate",
            flags=re.IGNORECASE,
        )
        objectives = [
            sentence[:500] for sentence in sentences if command_markers.search(sentence)
        ][:10]
        action_markers = {
            "create": ("建立", "新增", "產生", "create", "add"),
            "implement": ("實作", "完成", "implement"),
            "execute": ("執行", "運行", "execute", "run"),
            "repair": ("修正", "修復", "修理", "fix", "repair"),
            "modify": ("修改", "變更", "更新", "modify", "update"),
            "inspect": ("檢討", "檢查", "查看", "inspect", "review", "check"),
        }
        requested_actions = [
            action
            for action, markers in action_markers.items()
            if any(marker in lowered_text for marker in markers)
        ]
        return {
            "sentence_count": len(sentences),
            "questions": questions[:20],
            "negations": negations[:30],
            "constraints": constraints[:30],
            "requested_outputs": requested_outputs,
            "objectives": objectives,
            "command": {
                "recognized": bool(objectives or requested_actions),
                "execution_requested": any(
                    action in {"create", "implement", "execute", "repair", "modify"}
                    for action in requested_actions
                ),
                "requested_actions": requested_actions,
            },
            "keywords": cls._semantic_keywords(text),
            "complexity": {
                "multi_sentence": len(sentences) > 1,
                "question_count": len(questions),
                "constraint_count": len(constraints),
            },
        }

    @classmethod
    def _task_classification(
        cls,
        *,
        intents: Sequence[str],
        actions: Sequence[Mapping[str, Any]],
        objects: Sequence[Mapping[str, Any]],
        text: str,
    ) -> dict[str, Any]:
        intent_set = set(intents)
        action_set = {str(item.get("action") or "") for item in actions}
        object_set = {str(item.get("object_type") or "") for item in objects}
        categories: list[str] = []
        if "coding" in intent_set or "code" in object_set:
            categories.append("coding")
        if "visual" in intent_set or object_set & {"image", "video"}:
            categories.append("visual")
        if (
            "file_management" in intent_set
            or object_set & {"file", "folder"}
            or ("classify" in action_set and object_set & {"image", "video"})
            or action_set & {
            "move",
            "copy",
            "rename",
            }
        ):
            categories.append("file_management")
        if intent_set & {"reasoning", "calculation", "statistics", "analysis", "risk"}:
            categories.append("reasoning")
        if intent_set & {"search", "reading"} or "query" in action_set:
            categories.append("rag_search")
        if "monitor" in action_set or re.search(
            r"自動化|排程|代理|agent|automation|schedule", text, flags=re.IGNORECASE
        ):
            categories.append("automation_agent")
        if object_set & {"service", "process", "config", "database"} or action_set & {
            "execute",
            "stop",
        }:
            categories.append("system_operation")
        if re.search(r"多模型|協作|交叉驗證|multi[- ]?model", text, flags=re.IGNORECASE):
            categories.append("multi_model_collaboration")
        if not categories:
            categories.append("general_chinese_task")
        categories = list(dict.fromkeys(categories))
        capability_needs = {
            "visual": "visual" in categories,
            "rag_or_search": "rag_search" in categories,
            "coding": "coding" in categories,
            "agent": "automation_agent" in categories,
            "multi_model": "multi_model_collaboration" in categories,
        }
        return {
            "primary": categories[0],
            "categories": categories,
            "capability_needs": capability_needs,
        }

    @staticmethod
    def _task_intensity(
        *,
        actions: Sequence[Mapping[str, Any]],
        objects: Sequence[Mapping[str, Any]],
        constraints: Sequence[Mapping[str, Any]],
        task_categories: Sequence[str],
        ambiguity_count: int,
        destructive_operation: bool,
        text: str,
    ) -> dict[str, Any]:
        score = 0
        reasons: list[str] = []
        if len(actions) >= 2:
            score += 1
            reasons.append("multiple-actions")
        if len(objects) >= 2:
            score += 1
            reasons.append("multiple-operation-objects")
        if constraints:
            score += 1
            reasons.append("explicit-constraints")
        if len(task_categories) >= 2:
            score += 1
            reasons.append("cross-domain-task")
        if ambiguity_count:
            score += 1
            reasons.append("context-or-clarification-required")
        if destructive_operation:
            score += 2
            reasons.append("destructive-operation-risk")
        if len(text) > 600 or text.count("\n") >= 5:
            score += 1
            reasons.append("long-or-multistep-input")
        if any(
            category in {"automation_agent", "system_operation", "multi_model_collaboration"}
            for category in task_categories
        ):
            score += 1
            reasons.append("operational-or-coordinated-execution")
        if score <= 1:
            level = "simple"
        elif score <= 3:
            level = "normal"
        elif score <= 5:
            level = "intermediate"
        else:
            level = "difficult"
        return {"level": level, "score": score, "reasons": reasons}
