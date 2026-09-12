from __future__ import annotations

import ast
import math
import operator
import re
import statistics
from collections import Counter
from datetime import date, datetime
from typing import Any


class StarMathematicalExpert:
    """Offline deterministic calculation, statistics and data organization."""

    _BINARY_OPERATORS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    _UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

    @classmethod
    def _evaluate_node(cls, node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return cls._evaluate_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and type(node.op) in cls._UNARY_OPERATORS:
            return float(cls._UNARY_OPERATORS[type(node.op)](cls._evaluate_node(node.operand)))
        if isinstance(node, ast.BinOp) and type(node.op) in cls._BINARY_OPERATORS:
            left = cls._evaluate_node(node.left)
            right = cls._evaluate_node(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError("exponent is outside the governed limit")
            value = float(cls._BINARY_OPERATORS[type(node.op)](left, right))
            if not math.isfinite(value) or abs(value) > 1e100:
                raise ValueError("result is outside the governed limit")
            return value
        raise ValueError("unsupported expression")

    @staticmethod
    def _expression(payload: dict[str, Any]) -> str:
        explicit = str(payload.get("expression") or "").strip()
        if explicit:
            return explicit
        prompt = str(payload.get("prompt") or "").replace("×", "*").replace("÷", "/")
        candidates = re.findall(r"[0-9][0-9.()\s+*/%^-]*[0-9)]", prompt)
        return max(candidates, key=len).strip().replace("^", "**") if candidates else ""

    @staticmethod
    def _numbers(payload: dict[str, Any]) -> list[float]:
        values = payload.get("numbers")
        if not isinstance(values, list):
            return []
        numbers = [float(value) for value in values if isinstance(value, (int, float))]
        return [value for value in numbers if math.isfinite(value)][:100_000]

    @staticmethod
    def _organize_records(payload: dict[str, Any]) -> dict[str, Any] | None:
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            return None
        records = [item for item in raw_records[:10_000] if isinstance(item, dict)]
        columns = sorted({str(key) for item in records for key in item})
        missing = {
            column: sum(item.get(column) in (None, "") for item in records)
            for column in columns
        }
        group_by = str(payload.get("group_by") or "").strip()
        groups = None
        if group_by:
            groups = dict(
                Counter(str(item.get(group_by) or "(empty)") for item in records).most_common()
            )
        return {
            "row_count": len(records),
            "columns": columns,
            "missing_by_column": missing,
            "group_by": group_by or None,
            "group_counts": groups,
        }

    @staticmethod
    def _finite_series(value: Any, *, limit: int = 100_000) -> list[float]:
        if not isinstance(value, list):
            return []
        output: list[float] = []
        for item in value[:limit]:
            try:
                number = float(item)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                output.append(number)
        return output

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        candidate = str(value or "").strip()
        if not candidate:
            return None
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(candidate[:10])
            except ValueError:
                return None

    @classmethod
    def _xirr(cls, payload: dict[str, Any]) -> dict[str, Any] | None:
        raw = payload.get("cash_flows")
        if not isinstance(raw, list):
            return None
        flows: list[tuple[date, float]] = []
        for item in raw[:10_000]:
            if not isinstance(item, dict):
                continue
            flow_date = cls._parse_date(item.get("date"))
            try:
                amount = float(item.get("amount"))
            except (TypeError, ValueError):
                continue
            if flow_date is not None and math.isfinite(amount):
                flows.append((flow_date, amount))
        if len(flows) < 2 or not any(amount < 0 for _day, amount in flows) or not any(
            amount > 0 for _day, amount in flows
        ):
            return {"error": "XIRR requires dated positive and negative cash flows"}
        flows.sort(key=lambda item: item[0])
        start = flows[0][0]

        def npv(rate: float) -> float:
            return sum(
                amount / ((1 + rate) ** ((day - start).days / 365.0))
                for day, amount in flows
            )

        low, high = -0.9999, 10.0
        low_value, high_value = npv(low), npv(high)
        while low_value * high_value > 0 and high < 1_000:
            high *= 2
            high_value = npv(high)
        if low_value * high_value > 0:
            return {"error": "XIRR did not converge inside the governed range"}
        for _ in range(160):
            middle = (low + high) / 2
            middle_value = npv(middle)
            if abs(middle_value) < 1e-9:
                low = high = middle
                break
            if low_value * middle_value <= 0:
                high = middle
            else:
                low, low_value = middle, middle_value
        rate = (low + high) / 2
        return {
            "annual_rate": rate,
            "annual_rate_percent": rate * 100,
            "cash_flow_count": len(flows),
            "start_date": flows[0][0].isoformat(),
            "end_date": flows[-1][0].isoformat(),
        }

    @classmethod
    def _portfolio_metrics(cls, payload: dict[str, Any]) -> dict[str, Any] | None:
        returns = cls._finite_series(payload.get("returns") or payload.get("return_series"))
        prices = cls._finite_series(payload.get("prices"))
        if not returns and len(prices) >= 2:
            returns = [
                prices[index] / prices[index - 1] - 1
                for index in range(1, len(prices))
                if prices[index - 1] != 0
            ]
        if payload.get("returns_in_percent") is True:
            returns = [value / 100 for value in returns]
        if not returns and not prices:
            return None
        metrics: dict[str, Any] = {"observation_count": len(returns)}
        if returns:
            twr = math.prod(1 + value for value in returns) - 1
            daily_mean = statistics.fmean(returns)
            daily_volatility = statistics.pstdev(returns) if len(returns) > 1 else 0.0
            annualized_return = (1 + twr) ** (252 / len(returns)) - 1 if twr > -1 else -1.0
            annualized_volatility = daily_volatility * math.sqrt(252)
            risk_free = float(payload.get("risk_free_rate_percent") or 0) / 100
            downside = [min(0.0, value) for value in returns]
            downside_deviation = statistics.pstdev(downside) * math.sqrt(252) if len(downside) > 1 else 0.0
            metrics.update(
                {
                    "time_weighted_return_percent": twr * 100,
                    "annualized_return_percent": annualized_return * 100,
                    "annualized_volatility_percent": annualized_volatility * 100,
                    "sharpe_ratio": (
                        (daily_mean * 252 - risk_free) / annualized_volatility
                        if annualized_volatility > 0
                        else None
                    ),
                    "sortino_ratio": (
                        (daily_mean * 252 - risk_free) / downside_deviation
                        if downside_deviation > 0
                        else None
                    ),
                }
            )
        if prices:
            peak = prices[0]
            maximum_drawdown = 0.0
            for price in prices:
                peak = max(peak, price)
                drawdown = price / peak - 1 if peak else 0.0
                maximum_drawdown = min(maximum_drawdown, drawdown)
            metrics["maximum_drawdown_percent"] = maximum_drawdown * 100
        return metrics

    @classmethod
    def _correlations(cls, payload: dict[str, Any]) -> dict[str, Any] | None:
        raw = payload.get("return_series_by_asset")
        if not isinstance(raw, dict):
            return None
        series = {
            str(key): cls._finite_series(value, limit=10_000)
            for key, value in raw.items()
        }
        series = {key: value for key, value in series.items() if len(value) >= 2}
        matrix: dict[str, dict[str, float | None]] = {}
        for left_name, left in series.items():
            matrix[left_name] = {}
            for right_name, right in series.items():
                count = min(len(left), len(right))
                left_values, right_values = left[-count:], right[-count:]
                left_deviation = statistics.pstdev(left_values)
                right_deviation = statistics.pstdev(right_values)
                if left_deviation == 0 or right_deviation == 0:
                    correlation = None
                else:
                    covariance = sum(
                        (left_values[index] - statistics.fmean(left_values))
                        * (right_values[index] - statistics.fmean(right_values))
                        for index in range(count)
                    ) / count
                    correlation = covariance / (left_deviation * right_deviation)
                matrix[left_name][right_name] = correlation
        return {"assets": sorted(series), "correlation_matrix": matrix}

    @staticmethod
    def _stress_and_rebalance(payload: dict[str, Any]) -> dict[str, Any] | None:
        current = payload.get("current_weights")
        target = payload.get("target_weights")
        shocks = payload.get("scenario_shocks")
        portfolio_value = float(payload.get("portfolio_value") or 0)
        if not any(isinstance(value, dict) for value in (current, target, shocks)):
            return None
        current = current if isinstance(current, dict) else {}
        target = target if isinstance(target, dict) else {}
        shocks = shocks if isinstance(shocks, dict) else {}
        scenario_change = sum(
            float(current.get(key) or 0) / 100 * float(shocks.get(key) or 0)
            for key in set(current) | set(shocks)
        )
        trades = {
            str(key): round(
                portfolio_value
                * (float(target.get(key) or 0) - float(current.get(key) or 0))
                / 100,
                4,
            )
            for key in set(current) | set(target)
        }
        return {
            "scenario_change_percent": scenario_change,
            "scenario_value_change": portfolio_value * scenario_change / 100,
            "rebalance_trade_values": dict(sorted(trades.items())),
        }

    def process(self, payload: dict[str, Any], intent: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "intent": intent,
            "network_used": False,
            "execution": "offline-deterministic",
        }
        expression = self._expression(payload)
        if expression:
            try:
                parsed = ast.parse(expression, mode="eval")
                result["calculation"] = {
                    "expression": expression,
                    "value": self._evaluate_node(parsed),
                }
            except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
                result["calculation"] = {
                    "expression": expression,
                    "error": str(exc),
                }
        numbers = self._numbers(payload)
        if numbers:
            result["statistics"] = {
                "count": len(numbers),
                "sum": math.fsum(numbers),
                "mean": statistics.fmean(numbers),
                "median": statistics.median(numbers),
                "minimum": min(numbers),
                "maximum": max(numbers),
                "population_standard_deviation": statistics.pstdev(numbers),
            }
        organized = self._organize_records(payload)
        if organized is not None:
            result["data_organization"] = organized
        xirr = self._xirr(payload)
        if xirr is not None:
            result["xirr"] = xirr
        portfolio_metrics = self._portfolio_metrics(payload)
        if portfolio_metrics is not None:
            result["portfolio_metrics"] = portfolio_metrics
        correlations = self._correlations(payload)
        if correlations is not None:
            result["correlations"] = correlations
        scenario = self._stress_and_rebalance(payload)
        if scenario is not None:
            result["scenario_and_rebalancing"] = scenario
        result["audit_trace"] = {
            "input_keys": sorted(str(key) for key in payload),
            "formula_engine": "offline-deterministic",
            "reproducible": True,
        }
        result["facts_locked"] = True
        return result
