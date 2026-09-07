"""Sovereign sub-laws — Chinese reference (中文主宰子法備用參考).

This module is backup-only and has no decision authority.  See
``governance_rule/codex/sovereigns.py`` for the authoritative code-form tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Tuple


@dataclass(frozen=True)
class ChineseCodexSovereign:
    """主宰子法的中文備用參考。"""

    id: str
    name: str
    rank: str
    duties: str
    powers: str
    prohibitions: str
    basis: str


SOVEREIGNS_CHINESE: Final[Tuple[ChineseCodexSovereign, ...]] = (
    ChineseCodexSovereign(
        id="governance-authority",
        name="治理權威",
        rank="最高規則層維護執行主體",
        duties="守護法典不可變性、守護法典完整性、維護最高規則層",
        powers="無",
        prohibitions="禁止從屬權威代行治理權、禁止凌駕治理法典",
        basis="法典",
    ),
    ChineseCodexSovereign(
        id="system-sovereign",
        name="系統主宰",
        rank="頂層總裁主宰",
        duties="平台全生命周期編排、依賴狀態整合、子主宰委派、運行子主宰委派、資源子主宰委派、資料子主宰委派、整合子主宰委派",
        powers="無",
        prohibitions="禁止越權執行、持有執行權、代決子主宰細節、直接執行子主宰工作、代行權限事務",
        basis="法典",
    ),
    ChineseCodexSovereign(
        id="maintenance-sovereign",
        name="系統維護主宰",
        rank="頂層總裁主宰",
        duties="更新管理、系統健康監控、資料完整性呈現、自動修復協調、故障判定、備份協調、自我健康測試管理",
        powers="無",
        prohibitions="禁止越權執行、逾越法典、忽略或掩蓋資料完整性異常、代行資料完整性查核、代行權限事務",
        basis="法典",
    ),
    ChineseCodexSovereign(
        id="permission-sovereign",
        name="系統權限主宰",
        rank="頂層總裁主宰",
        duties="權限管理、權限發放、權限終止、權限監管、權限 ID 管理、身分組監管",
        powers="無",
        prohibitions="禁止越權執行、逾越法典、自我準予、委派、繼承、特權擴張、代行權限事務、各模組自行發放權限 ID",
        basis="法典",
    ),
    ChineseCodexSovereign(
        id="xingcheng",
        name="星澄",
        rank="頂層總裁主宰",
        duties="觀察、分析、推理、建議、協調、解釋、管理思考、模型模式收斂",
        powers="觀察、分析、推理、建議、協調、解釋",
        prohibitions="禁止直接執行、授權他人執行、覆寫決策、覆寫狀態、憑自身推論凌駕法典、持有系統執行權、代行整合結構性介面、模型模式逾越宣告之三模式、任一模式持有執行權",
        basis="法典",
    ),
)


__all__ = ["ChineseCodexSovereign", "SOVEREIGNS_CHINESE"]
