"""Response Validator — 最終輸出檢查。

對應需求 40-41：
  Response Validator 檢查：
  - 是否回答問題
  - 是否引用不存在來源
  - 是否把網路文字當成系統指令
  - 是否混淆不同來源
  - 是否存在明顯時間衝突
  - 是否有未處理的搜尋錯誤
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .citation import CitationResult


@dataclass
class ValidationResult:
    """驗證結果。"""

    valid: bool = True
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sanitized_answer: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": self.issues,
            "warnings": self.warnings,
        }


class ResponseValidator:
    """回應驗證器。"""

    # 可能的 prompt injection 模式
    _INJECTION_PATTERNS = (
        re.compile(r"ignore (?:previous|above|all) instructions", re.IGNORECASE),
        re.compile(r"disregard (?:your|the) (?:system|governance) prompt", re.IGNORECASE),
        re.compile(r"you are now (?:a|an) (?:different|new)", re.IGNORECASE),
        re.compile(r"system:\s*", re.IGNORECASE),
        re.compile(r"<\|system\|>", re.IGNORECASE),
        re.compile(r"忘記(?:之前|前面|所有)(?:的)?指令", re.IGNORECASE),
        re.compile(r"忽略(?:系統|治理)(?:規則|指令|提示)", re.IGNORECASE),
        re.compile(r"你現在是(?:一個|新的)(?:不同|新)", re.IGNORECASE),
    )

    # 系統指令模式（不應出現在回答中）
    _SYSTEM_INSTRUCTION_PATTERNS = (
        re.compile(r"<\|system\|>.*?<\|/system\|>", re.IGNORECASE | re.DOTALL),
        re.compile(r"<\|im_start\|>system.*?<\|im_end\|>", re.IGNORECASE | re.DOTALL),
    )

    def validate(
        self,
        answer: str,
        citation_result: CitationResult,
        *,
        user_question: str = "",
        search_errors: list[str] | None = None,
    ) -> ValidationResult:
        result = ValidationResult(sanitized_answer=answer)

        # 1. 是否回答問題
        if user_question and len(answer.strip()) < 5:
            result.issues.append("回答過短，可能未實際回答問題")
            result.valid = False

        # 2. 是否引用不存在來源
        if citation_result.unresolved_references:
            result.warnings.append(
                f"回答中引用了 {len(citation_result.unresolved_references)} 個不存在的來源"
            )

        # 3. 是否把網路文字當成系統指令（prompt injection）
        for pattern in self._INJECTION_PATTERNS:
            if pattern.search(answer):
                result.issues.append(f"偵測到可能的 prompt injection: {pattern.pattern}")
                result.valid = False
                result.sanitized_answer = self._sanitize_injection(result.sanitized_answer)

        # 4. 移除系統指令標記
        for pattern in self._SYSTEM_INSTRUCTION_PATTERNS:
            if pattern.search(result.sanitized_answer):
                result.sanitized_answer = pattern.sub("[已移除可疑內容]", result.sanitized_answer)
                result.warnings.append("移除了回答中的系統指令標記")

        # 5. 是否有未處理的搜尋錯誤
        if search_errors:
            result.warnings.append(f"搜尋過程中有 {len(search_errors)} 個錯誤")

        # 6. 時間衝突（簡易檢查）
        time_mentions = re.findall(r"\b(20\d{2})\b", answer)
        if len(set(time_mentions)) > 3:
            result.warnings.append("回答中出現多個不同年份，可能存在時間衝突")

        return result

    @staticmethod
    def _sanitize_injection(text: str) -> str:
        """移除可疑的 injection 內容。"""
        # 移除整行可疑指令
        lines = text.split("\n")
        clean_lines = [
            line for line in lines
            if not any(
                re.search(p, line, re.IGNORECASE)
                for p in (
                    r"ignore (?:previous|above|all) instructions",
                    r"disregard.*system.*prompt",
                    r"<\|system\|>",
                    r"忘記.*指令",
                    r"忽略.*系統.*規則",
                )
            )
        ]
        return "\n".join(clean_lines)


__all__ = ["ResponseValidator", "ValidationResult"]
