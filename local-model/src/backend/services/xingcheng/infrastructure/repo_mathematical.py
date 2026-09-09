from __future__ import annotations

import sqlite3
from typing import Any


class MathematicalMixin:
    """Mathematical capability definitions and catalog for LocalAiRepository."""

    def _seed_mathematical_capability_definitions(
        self, connection: sqlite3.Connection
    ) -> None:
        now = self._utc_now()
        definitions = (
            ("arithmetic", "calculation", "一般運算", "四則、次方、餘數與括號運算。"),
            ("formula", "calculation", "公式計算", "依明確輸入與公式執行可重現計算。"),
            ("logic", "reasoning", "邏輯推理", "區分前提、推導與結論並檢查矛盾。"),
            ("descriptive-statistics", "statistics", "描述統計", "計算樣本數、平均、中位數與離散程度。"),
            ("comparative-statistics", "statistics", "比較統計", "比較群組與期間差異，保留樣本限制。"),
            ("data-cleaning", "data", "資料清理", "辨識缺漏、型別與重複資料，不猜測補值。"),
            ("data-aggregation", "data", "資料彙整", "依欄位分類、計數與彙整。"),
            ("result-validation", "validation", "結果驗證", "檢查有限值、範圍、除零與輸入完整性。"),
            ("xirr", "finance", "不規則現金流報酬", "依日期與現金流計算可重現 XIRR。"),
            ("time-weighted-return", "finance", "時間加權報酬", "依期間報酬鏈結計算時間加權報酬。"),
            ("maximum-drawdown", "risk", "最大回撤", "依價格序列計算峰值至谷底的最大回撤。"),
            ("risk-adjusted-return", "risk", "風險調整報酬", "計算年化波動、Sharpe 與 Sortino。"),
            ("covariance-correlation", "statistics", "共變異與相關性", "比較多資產報酬序列的相關性。"),
            ("scenario-testing", "risk", "情境壓力測試", "套用明確曝險與衝擊參數估算情境變化。"),
            ("portfolio-rebalancing", "portfolio", "再平衡計算", "依目前與目標權重計算調整金額。"),
            ("calculation-audit-trace", "validation", "計算稽核軌跡", "保留輸入欄位、公式引擎與可重現狀態。"),
        )
        connection.executemany(
            """
            INSERT INTO mathematical_capability_definition(
                capability_key, category, display_name, description, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(capability_key) DO UPDATE SET
                category=excluded.category,
                display_name=excluded.display_name,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            [(*item, now) for item in definitions],
        )

    def mathematical_capability_catalog(self) -> list[dict[str, Any]]:
        if self.database_scope != "mathematical":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT capability_key, category, display_name, description, enabled
                FROM mathematical_capability_definition
                WHERE enabled = 1 ORDER BY category, capability_key
                """
            ).fetchall()
        return [
            {
                "capability_key": str(row[0]),
                "category": str(row[1]),
                "display_name": str(row[2]),
                "description": str(row[3]),
                "enabled": bool(row[4]),
            }
            for row in rows
        ]
