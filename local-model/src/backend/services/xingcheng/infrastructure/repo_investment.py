from __future__ import annotations

import json
import sqlite3
from typing import Any


class InvestmentMixin:
    """Investment parameter values, model definitions, and seeding for LocalAiRepository."""

    def investment_parameter_values(self) -> dict[str, float]:
        if self.database_scope != "investment":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        values = {
            key: definition[0]
            for key, definition in self.ADJUSTABLE_INVESTMENT_PARAMETERS.items()
        }
        with self._connect() as connection:
            for key in values:
                row = connection.execute(
                    """
                    SELECT applied_value FROM investment_parameter_adjustment
                    WHERE parameter_key = ? ORDER BY id DESC LIMIT 1
                    """,
                    (key,),
                ).fetchone()
                if row is not None:
                    values[key] = float(row[0])
        return values

    def apply_chatgpt_parameter_recommendations(
        self, recommendations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if self.database_scope != "investment":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        current = self.investment_parameter_values()
        applied: list[dict[str, Any]] = []
        with self._connect() as connection:
            for recommendation in recommendations[:10]:
                key = str(recommendation.get("parameter_key") or "").strip()
                definition = self.ADJUSTABLE_INVESTMENT_PARAMETERS.get(key)
                if definition is None:
                    continue
                try:
                    value = float(recommendation.get("value"))
                except (TypeError, ValueError):
                    continue
                _default, minimum, maximum = definition
                if not minimum <= value <= maximum:
                    continue
                previous = float(current[key])
                rationale = str(recommendation.get("rationale") or "").strip()[:1000]
                connection.execute(
                    """
                    INSERT INTO investment_parameter_adjustment(
                        parameter_key, previous_value, applied_value,
                        minimum_value, maximum_value, recommendation_source,
                        rationale, applied_by, applied_at
                    ) VALUES (?, ?, ?, ?, ?, 'chatgpt', ?, 'star-main-native-model', ?)
                    """,
                    (key, previous, value, minimum, maximum, rationale, self._utc_now()),
                )
                current[key] = value
                applied.append(
                    {
                        "parameter_key": key,
                        "previous_value": previous,
                        "applied_value": value,
                        "minimum_value": minimum,
                        "maximum_value": maximum,
                        "recommendation_source": "chatgpt",
                        "applied_by": "star-main-native-model",
                    }
                )
        return applied

    def _seed_parameter_definitions(self, connection: sqlite3.Connection) -> None:
        now = self._utc_now()
        definitions = (
            ("price", "valuation", "市價／淨值", "number", "currency", "最近可驗證的市價或基金淨值"),
            ("previous_close", "valuation", "前收／前次淨值", "number", "currency", "前一有效觀測值"),
            ("change_percent", "return", "漲跌幅", "number", "%", "相對前一有效觀測值的變動"),
            ("ytd_return_percent", "return", "年初至今報酬", "number", "%", "公開來源揭露的年初至今變動"),
            ("distribution_amount", "income", "每單位配息", "number", "currency", "已公告的每單位現金分配"),
            ("distribution_frequency", "income", "配息頻率", "text", "", "公開來源揭露或由歷史事件推估"),
            ("annual_distribution_per_unit", "income", "近一年每單位配息", "number", "currency", "近 366 日現金分配合計"),
            ("distribution_yield_percent", "income", "近一年配息率", "number", "%", "近一年每單位配息除以最近價格"),
            ("risk_level", "risk", "風險等級", "text", "", "公開來源揭露的風險等級"),
            ("volatility_percent", "risk", "波動率", "number", "%", "依可用價格序列計算"),
            ("management_fee_percent", "fee", "經理費", "number", "%", "公開資料揭露的管理費率"),
            ("custody_fee_percent", "fee", "保管費", "number", "%", "公開資料揭露的保管費率"),
            ("fund_size", "profile", "基金規模", "number", "currency", "公開資料揭露的基金規模"),
            ("region_exposure", "exposure", "區域曝險", "json", "%", "公開資料揭露的區域配置"),
            ("industry_exposure", "exposure", "產業曝險", "json", "%", "公開資料揭露的產業配置"),
        )
        connection.executemany(
            """
            INSERT INTO investment_parameter_definition(
                parameter_key, category, display_name, value_type, unit, description, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(parameter_key) DO UPDATE SET
                category=excluded.category,
                display_name=excluded.display_name,
                value_type=excluded.value_type,
                unit=excluded.unit,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            [(*item, now) for item in definitions],
        )

    def _seed_investment_model_definitions(self, connection: sqlite3.Connection) -> None:
        now = self._utc_now()
        definitions = (
            (
                "valuation",
                "valuation",
                "估值模型",
                "star-investment-parameter-engine",
                "1.0",
                ("price", "previous_close", "fund_size"),
                "依價格、淨值與規模進行相對估值；資料不足時保留未知值。",
            ),
            (
                "income-distribution",
                "income",
                "收益與配息模型",
                "star-investment-parameter-engine",
                "1.0",
                (
                    "distribution_amount",
                    "distribution_frequency",
                    "annual_distribution_per_unit",
                    "distribution_yield_percent",
                ),
                "分析現金分配、頻率與近一年配息率。",
            ),
            (
                "risk-volatility",
                "risk",
                "風險與波動模型",
                "star-investment-parameter-engine",
                "1.0",
                ("risk_level", "volatility_percent"),
                "評估風險等級與可驗證價格序列的波動。",
            ),
            (
                "portfolio-concentration",
                "portfolio",
                "投資組合集中度模型",
                "star-investment-analysis-engine",
                "1.0",
                ("region_exposure", "industry_exposure"),
                "檢查標的、區域與產業集中風險。",
            ),
            (
                "asset-allocation",
                "portfolio",
                "資產配置模型",
                "star-investment-analysis-engine",
                "1.0",
                ("region_exposure", "industry_exposure", "risk_level"),
                "依資產、區域、產業與風險層級檢查配置。",
            ),
            (
                "scenario-stress",
                "risk",
                "情境壓力模型",
                "star-mathematical-delegation-engine",
                "1.0",
                ("volatility_percent", "change_percent", "risk_level"),
                "由主模型協調數理專家執行情境與壓力計算。",
            ),
            (
                "fee-efficiency",
                "fee",
                "費用效率模型",
                "star-investment-parameter-engine",
                "1.0",
                ("management_fee_percent", "custody_fee_percent"),
                "分析經理費與保管費對持有成本的影響。",
            ),
            (
                "return-trend",
                "return",
                "報酬趨勢模型",
                "star-investment-parameter-engine",
                "1.0",
                ("change_percent", "ytd_return_percent"),
                "比較短期變動與年初至今報酬，不補造缺少的歷史值。",
            ),
        )
        connection.executemany(
            """
            INSERT INTO investment_model_definition(
                model_key, category, display_name, engine, version,
                parameter_keys_json, description, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_key) DO UPDATE SET
                category=excluded.category,
                display_name=excluded.display_name,
                engine=excluded.engine,
                version=excluded.version,
                parameter_keys_json=excluded.parameter_keys_json,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            [
                (*item[:5], json.dumps(item[5], ensure_ascii=False), item[6], now)
                for item in definitions
            ],
        )

    def investment_model_catalog(self) -> list[dict[str, Any]]:
        if self.database_scope != "investment":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT model_key, category, display_name, engine, version,
                       parameter_keys_json, description, enabled
                FROM investment_model_definition
                WHERE enabled = 1 ORDER BY category, model_key
                """
            ).fetchall()
        return [
            {
                "model_key": str(row[0]),
                "category": str(row[1]),
                "display_name": str(row[2]),
                "engine": str(row[3]),
                "version": str(row[4]),
                "parameter_keys": json.loads(str(row[5]) or "[]"),
                "description": str(row[6]),
                "enabled": bool(row[7]),
            }
            for row in rows
        ]
