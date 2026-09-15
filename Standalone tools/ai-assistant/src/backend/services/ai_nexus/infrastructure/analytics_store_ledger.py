from __future__ import annotations

import hashlib
import uuid
from typing import Any

from .analytics_common import (
    RECONCILIATION_PREFIX,
    number,
    parse_datetime,
    rounded,
    utc_now,
    utc_text,
)


class AnalyticsStoreLedgerMixin:
    """Position valuation, ledger summary, and reconciliation methods."""

    def record_analysis_snapshot(self, analysis: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        observed = parse_datetime(analysis.get("generated_at")) or utc_now()
        bars = self._snapshot_price_bars(analysis, observed)
        self.add_price_bars(bars)
        latest = self.latest_prices()
        positions, quoted_symbols = self._snapshot_positions(state, latest)
        if not positions:
            return {"price_count": len(bars), "snapshot_saved": False}
        snapshot_id = uuid.uuid4().hex
        total_value = sum(item["market_value"] for item in positions)
        total_cost = sum(item["cost_value"] for item in positions)
        base_currency = str(self.get_setting("base_currency", "TWD") or "TWD")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO portfolio_snapshots VALUES(?, ?, ?, ?, ?, ?)",
                (snapshot_id, utc_text(observed), total_value, total_cost, base_currency, 0.0),
            )
            connection.executemany(
                """
                INSERT INTO snapshot_positions(
                    snapshot_id, symbol, market, asset_type, quantity, price,
                    market_value, cost_value, currency
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item["symbol"],
                        item["market"],
                        item["asset_type"],
                        item["quantity"],
                        item["price"],
                        item["market_value"],
                        item["cost_value"],
                        item["currency"],
                    )
                    for item in positions
                ],
            )
        return {
            "price_count": len(bars),
            "snapshot_saved": True,
            "snapshot_id": snapshot_id,
            "quoted_position_count": len(quoted_symbols),
            "position_count": len(positions),
            "estimated_position_count": max(0, len(positions) - len(quoted_symbols)),
        }

    def _snapshot_price_bars(
        self,
        analysis: dict[str, Any],
        observed: Any,
    ) -> list[dict[str, Any]]:
        reports = analysis.get("holdings") if isinstance(analysis.get("holdings"), list) else []
        bars: list[dict[str, Any]] = []
        for report in reports:
            if not isinstance(report, dict):
                continue
            quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
            price = number(quote.get("price"), -1)
            symbol = str(report.get("symbol") or "").upper()
            if not symbol or price <= 0:
                continue
            bars.append(
                {
                    "symbol": symbol,
                    "observed_at": quote.get("as_of") or utc_text(observed),
                    "close": price,
                    "currency": quote.get("currency") or report.get("currency"),
                    "provider": quote.get("provider") or "投資管家",
                    "verified": bool(report.get("trusted_quote")),
                }
            )
        return bars

    def _snapshot_positions(
        self,
        state: dict[str, Any],
        latest: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        positions_by_symbol: dict[str, dict[str, Any]] = {}
        quoted_symbols: set[str] = set()
        for holding in state.get("holdings", []):
            if not isinstance(holding, dict):
                continue
            symbol = str(holding.get("symbol") or "").upper()
            quote = latest.get(symbol, {})
            quoted_price = number(quote.get("close"), -1)
            if quoted_price > 0:
                quoted_symbols.add(symbol)
            price = quoted_price if quoted_price > 0 else number(holding.get("average_cost"))
            quantity = number(holding.get("quantity"))
            average_cost = number(holding.get("average_cost"))
            if not symbol or price <= 0:
                continue
            position = positions_by_symbol.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "market": str(holding.get("market") or ""),
                    "asset_type": str(holding.get("asset_type") or ""),
                    "quantity": 0.0,
                    "price": price,
                    "market_value": 0.0,
                    "cost_value": 0.0,
                    "currency": str(
                        quote.get("currency") or holding.get("currency") or ""
                    ),
                },
            )
            position["quantity"] += quantity
            position["market_value"] += quantity * price
            position["cost_value"] += quantity * average_cost
        return list(positions_by_symbol.values()), quoted_symbols

    def current_positions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        latest = self.latest_prices()
        positions: list[dict[str, Any]] = []
        holdings = [
            item for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        use_twd_valuation = any(
            number(item.get(field)) > 0
            for item in holdings
            for field in ("principal_twd", "current_value_twd", "web_current_value_twd")
        )
        for holding in holdings:
            positions += self._valued_position(holding, latest, use_twd_valuation)
        total = sum(number(item.get("market_value")) for item in positions)
        for item in positions:
            item["weight_percent"] = rounded(number(item.get("market_value")) / total * 100, 2) if total > 0 else None
        return sorted(positions, key=lambda item: number(item.get("market_value")), reverse=True)

    def _valued_position(
        self,
        holding: dict[str, Any],
        latest: dict[str, dict[str, Any]],
        use_twd_valuation: bool,
    ) -> list[dict[str, Any]]:
        symbol = str(holding.get("symbol") or "").upper()
        quote = latest.get(symbol, {})
        price = number(quote.get("close"), number(holding.get("average_cost")))
        quantity = number(holding.get("quantity"))
        if quantity <= 0:
            return []
        native_cost = number(holding.get("average_cost")) * quantity
        native_value = price * quantity
        if use_twd_valuation:
            cost = number(holding.get("principal_twd"))
            if cost <= 0 and str(holding.get("currency") or "TWD").upper() == "TWD":
                cost = number(holding.get("principal_amount"), native_cost)
            value = number(
                holding.get("web_current_value_twd"),
                number(holding.get("current_value_twd")),
            )
        else:
            cost = native_cost
            value = native_value
        return [
            {
                **holding,
                "symbol": symbol,
                "price": rounded(price),
                "market_value": rounded(value, 2),
                "cost_value": rounded(cost, 2),
                "unrealized_pnl": rounded(value - cost, 2),
                "unrealized_pnl_percent": rounded((value / cost - 1) * 100, 2) if cost > 0 else None,
                "price_as_of": quote.get("observed_at"),
            }
        ]

    def ledger_summary(self) -> dict[str, Any]:
        transactions = list(reversed(self.list_transactions(limit=5000)))
        estimated_count = sum(1 for item in transactions if item.get("is_estimated"))
        lots, realized, dividends, fees, cashflows = self._ledger_fold(transactions)
        opening_ledger = self.get_setting("opening_ledger", {})
        confirmed_count = len(transactions) - estimated_count
        ledger_quality = (
            "empty"
            if not transactions
            else "estimated_opening"
            if estimated_count
            else "confirmed"
        )
        return {
            "transaction_count": len(transactions),
            "confirmed_transaction_count": confirmed_count,
            "estimated_transaction_count": estimated_count,
            "ledger_quality": ledger_quality,
            "opening_ledger": opening_ledger if isinstance(opening_ledger, dict) else {},
            "realized_pnl": rounded(realized, 2),
            "dividend_income": rounded(dividends, 2),
            "fees_and_taxes": rounded(fees, 2),
            "open_lot_count": sum(len(items) for items in lots.values()),
            "cashflows": cashflows[-500:],
        }

    def _ledger_fold(
        self,
        transactions: list[dict[str, Any]],
    ) -> tuple[
        dict[str, list[list[float]]],
        float,
        float,
        float,
        list[dict[str, Any]],
    ]:
        lots: dict[str, list[list[float]]] = {}
        realized = 0.0
        dividends = 0.0
        fees = 0.0
        cashflows: list[dict[str, Any]] = []
        for transaction in transactions:
            symbol = str(transaction.get("symbol") or "")
            side = str(transaction.get("side") or "")
            quantity = number(transaction.get("quantity"))
            price = number(transaction.get("price"))
            fee = number(transaction.get("fee"))
            tax = number(transaction.get("tax"))
            fees += fee + tax
            date = str(transaction.get("occurred_at") or "")
            if side == "BUY":
                lots.setdefault(symbol, []).append([quantity, price])
                cashflows.append({"date": date, "amount": -(quantity * price + fee + tax)})
            elif side == "SELL":
                realized += self._fold_sell(
                    lots.setdefault(symbol, []),
                    quantity,
                    price,
                    fee,
                    tax,
                )
                cashflows.append({"date": date, "amount": quantity * price - fee - tax})
            elif side == "DIVIDEND":
                amount = price if quantity <= 0 else quantity * price
                dividends += amount
                cashflows.append({"date": date, "amount": amount})
            elif side == "CASH_IN":
                cashflows.append({"date": date, "amount": -price})
            elif side in {"CASH_OUT", "FEE"}:
                cashflows.append({"date": date, "amount": price})
        return lots, realized, dividends, fees, cashflows

    def _fold_sell(
        self,
        symbol_lots: list[list[float]],
        quantity: float,
        price: float,
        fee: float,
        tax: float,
    ) -> float:
        remaining = quantity
        cost = 0.0
        for lot in symbol_lots:
            used = min(lot[0], remaining)
            cost += used * lot[1]
            lot[0] -= used
            remaining -= used
            if remaining <= 1e-10:
                break
        symbol_lots[:] = [lot for lot in symbol_lots if lot[0] > 1e-10]
        proceeds = quantity * price - fee - tax
        return proceeds - cost

    def reconcile_ledger_holdings(self, state: dict[str, Any]) -> dict[str, Any]:
        holding_positions = self._reconcile_holding_positions(state)
        ledger_positions = self._ledger_position_quantities()
        all_symbols = sorted(set(holding_positions) | set(ledger_positions))
        differences, matched_count = self._reconcile_differences(
            all_symbols,
            holding_positions,
            ledger_positions,
        )
        difference_rows = [item for item in differences if item["status"] == "difference"]
        return {
            "status": "ready" if all_symbols and not difference_rows else "differences" if all_symbols else "empty",
            "symbol_count": len(all_symbols),
            "matched_count": matched_count,
            "difference_count": len(difference_rows),
            "coverage_percent": rounded(matched_count / len(all_symbols) * 100, 2) if all_symbols else 0.0,
            "differences": difference_rows[:100],
            "generated_at": utc_text(),
        }

    def _reconcile_holding_positions(
        self,
        state: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        holding_positions: dict[str, dict[str, Any]] = {}
        for holding in state.get("holdings", []):
            if not isinstance(holding, dict):
                continue
            symbol = str(holding.get("symbol") or "").strip().upper()
            quantity = number(holding.get("quantity"))
            if not symbol or quantity <= 0:
                continue
            row = holding_positions.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "quantity": 0.0,
                    "market": str(holding.get("market") or "").upper(),
                    "asset_type": str(holding.get("asset_type") or "").upper(),
                    "currency": str(holding.get("currency") or "TWD").upper(),
                    "principal_twd": 0.0,
                    "average_cost_total": 0.0,
                },
            )
            row["quantity"] += quantity
            row["principal_twd"] += number(holding.get("principal_twd"))
            row["average_cost_total"] += number(holding.get("average_cost")) * quantity
        return holding_positions

    def _ledger_position_quantities(self) -> dict[str, float]:
        ledger_positions: dict[str, float] = {}
        for transaction in reversed(self.list_transactions(5000)):
            symbol = str(transaction.get("symbol") or "").strip().upper()
            side = str(transaction.get("side") or "").upper()
            quantity = number(transaction.get("quantity"))
            if not symbol or side not in {"BUY", "SELL"}:
                continue
            ledger_positions[symbol] = ledger_positions.get(symbol, 0.0) + (
                quantity if side == "BUY" else -quantity
            )
        return ledger_positions

    def _reconcile_differences(
        self,
        all_symbols: list[str],
        holding_positions: dict[str, dict[str, Any]],
        ledger_positions: dict[str, float],
    ) -> tuple[list[dict[str, Any]], int]:
        differences: list[dict[str, Any]] = []
        matched_count = 0
        for symbol in all_symbols:
            holding = holding_positions.get(symbol, {})
            holding_quantity = number(holding.get("quantity"))
            ledger_quantity = number(ledger_positions.get(symbol))
            delta = holding_quantity - ledger_quantity
            tolerance = max(0.000001, abs(holding_quantity) * 0.00001)
            matched = abs(delta) <= tolerance
            matched_count += int(matched)
            suggestion = (
                None
                if matched
                else self._reconcile_suggestion(symbol, delta, holding, holding_quantity)
            )
            differences.append(
                {
                    "symbol": symbol,
                    "holding_quantity": rounded(holding_quantity, 8),
                    "ledger_quantity": rounded(ledger_quantity, 8),
                    "difference_quantity": rounded(delta, 8),
                    "status": "matched" if matched else "difference",
                    "suggestion": suggestion,
                }
            )
        return differences, matched_count

    def _reconcile_suggestion(
        self,
        symbol: str,
        delta: float,
        holding: dict[str, Any],
        holding_quantity: float,
    ) -> dict[str, Any]:
        quantity = abs(delta)
        principal_twd = number(holding.get("principal_twd"))
        average_total = number(holding.get("average_cost_total"))
        price = (
            principal_twd / holding_quantity
            if holding_quantity > 0 and principal_twd > 0
            else average_total / holding_quantity
            if holding_quantity > 0 and average_total > 0
            else 0.0
        )
        return {
            "symbol": symbol,
            "side": "BUY" if delta > 0 else "SELL",
            "quantity": rounded(quantity, 8),
            "price": rounded(price, 8),
            "currency": "TWD" if principal_twd > 0 else str(holding.get("currency") or "TWD"),
            "market": str(holding.get("market") or ""),
            "asset_type": str(holding.get("asset_type") or ""),
        }

    def apply_ledger_reconciliation(
        self,
        state: dict[str, Any],
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("ledger reconciliation requires confirmation")
        reconciliation = self.reconcile_ledger_holdings(state)
        applied = 0
        stamp = utc_text()
        for item in reconciliation.get("differences", []):
            suggestion = item.get("suggestion") if isinstance(item, dict) else None
            if not isinstance(suggestion, dict):
                continue
            if number(suggestion.get("quantity")) <= 0 or number(suggestion.get("price")) <= 0:
                continue
            identity = f"{stamp}|{suggestion.get('symbol')}|{suggestion.get('side')}|{suggestion.get('quantity')}"
            self.add_transaction(
                {
                    **suggestion,
                    "transaction_id": RECONCILIATION_PREFIX
                    + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                    "occurred_at": stamp,
                    "note": "依目前持股與交易帳本差額建立的估算對帳調整；並非券商成交紀錄。",
                }
            )
            applied += 1
        result = self.reconcile_ledger_holdings(state)
        result["applied_count"] = applied
        self.audit("ledger_reconciliation_applied", result, severity="warning")
        return result


__all__ = ['AnalyticsStoreLedgerMixin']
