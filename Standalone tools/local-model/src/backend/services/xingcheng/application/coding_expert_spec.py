from __future__ import annotations

import re
from typing import Any


class CodingExpertSpecMixin:
    """Spec inference and normalization for StarCodingExpert."""

    @classmethod
    def _infer_spec(cls, prompt: str) -> dict[str, Any]:
        normalized = str(prompt or "").casefold()
        rules = (
            (("平均", "average", "mean"), "calculate_average", "sum(values) / len(values) if values else 0"),
            (("加總", "總和", "sum"), "calculate_total", "sum(values)"),
            (("最大", "maximum", " max"), "find_maximum", "max(values) if values else None"),
            (("最小", "minimum", " min"), "find_minimum", "min(values) if values else None"),
            (("數量", "個數", "count"), "count_items", "len(values)"),
            (("排序", "sort"), "sort_values", "sorted(values)"),
        )
        inferred: dict[str, Any] = {
            "language": "python",
            "kind": "function",
            "name": "generated_task",
            "parameters": ["payload"],
            "description": prompt or "星澄產生的函式",
            "return_expression": "None",
            "operation": "custom",
        }
        if any(
            token in normalized
            for token in ("fastapi", "rest api", "restful", "api 服務", "api服務")
        ):
            inferred.update(
                kind="api",
                name="star_api",
                framework="fastapi",
                resource="items",
            )
        for tokens, name, expression in rules:
            if any(token in normalized for token in tokens):
                inferred.update(
                    name=name,
                    parameters=["values"],
                    return_expression=expression,
                    operation={
                        "calculate_average": "average",
                        "calculate_total": "sum",
                        "find_maximum": "maximum",
                        "find_minimum": "minimum",
                        "count_items": "count",
                        "sort_values": "sort",
                    }[name],
                )
                break
        if "typescript" in normalized or "typescript" in str(prompt).casefold():
            inferred["language"] = "typescript"
        elif "javascript" in normalized or "javascript" in str(prompt).casefold():
            inferred["language"] = "javascript"
        elif re.search(r"\bsql\b", normalized):
            inferred["language"] = "sql"
        elif "json" in normalized:
            inferred["language"] = "json"
        if inferred["kind"] == "api":
            pass
        elif "dataclass" in normalized or "資料類別" in normalized:
            inferred.update(kind="dataclass", name="GeneratedRecord")
        elif "類別" in normalized or "class" in normalized:
            inferred.update(kind="class", name="GeneratedService")
        elif "測試" in normalized or "unittest" in normalized:
            inferred.update(kind="test", name="GeneratedCodeTest")
        return inferred

    @classmethod
    def _normalize_spec(cls, payload: dict[str, Any], prompt: str, intent: str) -> dict[str, Any]:
        inferred = cls._infer_spec(prompt)
        provided = payload.get("code_spec")
        if isinstance(provided, dict):
            inferred.update(provided)
        action = str(
            inferred.get("action")
            or ("self_upgrade" if intent == "self_upgrade" else "generate")
        ).strip().casefold()
        inferred["action"] = action if action in cls.ALLOWED_ACTIONS else "generate"
        inferred["language"] = str(inferred.get("language") or "python").strip().casefold()
        inferred["kind"] = str(inferred.get("kind") or "function").strip().casefold()
        if inferred["language"] == "sql":
            inferred["kind"] = "query"
        elif inferred["language"] == "json":
            inferred["kind"] = "document"
        return inferred
