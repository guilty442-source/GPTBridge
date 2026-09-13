"""xingcheng_native_model_domains — 業務能力面 mixin.

從 XingchengNativeModel 提取的業務能力面屬性：
投資市場搜尋、投資分析、程式升級、專案程式、AI 連線、文件閱讀。
"""

from __future__ import annotations

from typing import Any


class XingchengNativeModelDomainsMixin:
    """業務能力面屬性 mixin。"""

    # 這些方法依賴 _business_domain() 和 _xingcheng_capabilities()，
    # 由主類別提供。

    def business_domain(self, domain_id: str) -> dict[str, Any]:
        """取得指定業務能力面的設定。"""
        return self._business_domain(domain_id)  # type: ignore[attr-defined]

    @property
    def business_domains(self) -> dict[str, dict[str, Any]]:
        """所有業務能力面的完整快照。"""
        manifest = self._manifest()  # type: ignore[attr-defined]
        capabilities = manifest.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            return {}
        return {
            domain_id: dict(config) if isinstance(config, dict) else {}
            for domain_id, config in capabilities.items()
        }

    @property
    def business_domain_ids(self) -> list[str]:
        """所有業務能力面 ID 清單。"""
        return sorted(self.business_domains.keys())

    # --- 投資市場搜尋 ---

    @property
    def investment_market_search(self) -> dict[str, Any]:
        """投資市場搜尋能力面。"""
        return self._business_domain("investment-market-search")  # type: ignore[attr-defined]

    # --- 投資分析 ---

    @property
    def investment_analysis(self) -> dict[str, Any]:
        """投資分析能力面。"""
        return self._business_domain("investment-analysis")  # type: ignore[attr-defined]

    # --- 程式升級優化 ---

    @property
    def upgrade_optimization(self) -> dict[str, Any]:
        """程式升級優化能力面。"""
        return self._business_domain("upgrade-optimization")  # type: ignore[attr-defined]

    # --- 專案程式 ---

    @property
    def star_project_programming(self) -> dict[str, Any]:
        """專案程式能力面。"""
        return self._business_domain("star-project-programming")  # type: ignore[attr-defined]

    # --- AI 連線 ---

    @property
    def ai_connections(self) -> dict[str, Any]:
        """AI 連線能力面。"""
        return self._business_domain("ai-connections")  # type: ignore[attr-defined]

    # --- 文件閱讀 ---

    @property
    def document_reading(self) -> dict[str, Any]:
        """文件閱讀能力。"""
        xingcheng_caps = self._xingcheng_capabilities()  # type: ignore[attr-defined]
        doc_reading = xingcheng_caps.get("document_reading") or {}
        return dict(doc_reading) if isinstance(doc_reading, dict) else {}
