from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from ..infrastructure.analytics_repository import (
    number,
    parse_datetime,
    rounded,
    utc_now,
    utc_text,
)


class PortfolioEngineFxMixin:
    def add_fx_rates(self, rates: Sequence[dict[str, Any]]) -> int:
        prepared = []
        for item in rates:
            base = str(item.get("base_currency") or item.get("base") or "").upper()
            quote = str(item.get("quote_currency") or item.get("quote") or "").upper()
            observed = parse_datetime(item.get("observed_at") or item.get("timestamp"))
            rate = number(item.get("rate"), -1)
            if not base or not quote or observed is None or rate <= 0:
                continue
            prepared.append(
                (
                    base,
                    quote,
                    utc_text(observed),
                    rate,
                    str(item.get("provider") or "manual"),
                    int(bool(item.get("verified", True))),
                )
            )
        if not prepared:
            return 0
        with self.store.connect() as connection:
            connection.executemany(
                """
                INSERT INTO fx_rates(base_currency, quote_currency, observed_at, rate, provider, verified)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(base_currency, quote_currency, observed_at, provider) DO UPDATE SET
                    rate=excluded.rate, verified=excluded.verified
                """,
                prepared,
            )
        self.store.audit("fx_rates_imported", {"count": len(prepared)})
        return len(prepared)

    def fx_rate(
        self,
        source_currency: str,
        target_currency: str,
        at: datetime | None = None,
    ) -> dict[str, Any] | None:
        source = str(source_currency or "").upper()
        target = str(target_currency or "").upper()
        if not source or not target:
            return None
        if source == target:
            return {"rate": 1.0, "observed_at": utc_text(at), "provider": "identity", "verified": True}
        cutoff = utc_text(at or utc_now())
        with self.store.connect() as connection:
            direct = connection.execute(
                """
                SELECT base_currency, quote_currency, observed_at, rate, provider, verified FROM fx_rates
                WHERE base_currency = ? AND quote_currency = ? AND observed_at <= ?
                ORDER BY observed_at DESC, verified DESC LIMIT 1
                """,
                (source, target, cutoff),
            ).fetchone()
            if direct:
                return dict(direct)
            inverse = connection.execute(
                """
                SELECT base_currency, quote_currency, observed_at, rate, provider, verified FROM fx_rates
                WHERE base_currency = ? AND quote_currency = ? AND observed_at <= ?
                ORDER BY observed_at DESC, verified DESC LIMIT 1
                """,
                (target, source, cutoff),
            ).fetchone()
        if inverse and number(inverse["rate"]) > 0:
            item = dict(inverse)
            item["rate"] = 1 / number(inverse["rate"])
            item["inverse"] = True
            return item
        return None

    def multi_currency_accounting(self, state: dict[str, Any]) -> dict[str, Any]:
        holdings = [item for item in state.get("holdings", []) if isinstance(item, dict)]
        base_currency = str(self.store.get_setting("base_currency", "TWD") or "TWD").upper()
        latest = self.store.latest_prices()
        transactions = list(reversed(self.store.list_transactions(5000)))
        acquisition_fx = self._acquisition_fx_lots(transactions, base_currency)
        rows = []
        missing: set[str] = set()
        totals = {
            "market_value": 0.0,
            "cost_value": 0.0,
            "asset_pnl": 0.0,
            "currency_pnl": 0.0,
        }
        for holding in holdings:
            row, values = self._mc_position_row(
                holding, base_currency, latest, acquisition_fx, missing
            )
            rows.append(row)
            if values is None:
                continue
            totals["market_value"] += values[0]
            totals["cost_value"] += values[1]
            totals["asset_pnl"] += values[2]
            totals["currency_pnl"] += values[3]
        return {
            "status": "ready" if rows and not missing else "incomplete_fx" if rows else "empty",
            "base_currency": base_currency,
            "market_value_base": rounded(totals["market_value"], 2),
            "cost_value_base": rounded(totals["cost_value"], 2),
            "total_pnl_base": rounded(totals["market_value"] - totals["cost_value"], 2),
            "asset_pnl_base": rounded(totals["asset_pnl"], 2),
            "currency_pnl_base": rounded(totals["currency_pnl"], 2),
            "missing_currencies": sorted(missing),
            "positions": rows,
            "methodology": "historical-acquisition-fx-and-latest-market-fx",
        }

    def _acquisition_fx_lots(
        self,
        transactions: Sequence[dict[str, Any]],
        base_currency: str,
    ) -> dict[str, list[tuple[float, float]]]:
        acquisition_fx: dict[str, list[tuple[float, float]]] = {}
        for transaction in transactions:
            if transaction.get("side") != "BUY":
                continue
            symbol = str(transaction.get("symbol") or "")
            currency = str(transaction.get("currency") or base_currency)
            occurred = parse_datetime(transaction.get("occurred_at"))
            quote = self.fx_rate(currency, base_currency, occurred)
            if quote:
                acquisition_fx.setdefault(symbol, []).append(
                    (number(transaction.get("quantity")), number(quote.get("rate")))
                )
        return acquisition_fx

    def _mc_position_row(
        self,
        holding: dict[str, Any],
        base_currency: str,
        latest: dict[str, Any],
        acquisition_fx: dict[str, list[tuple[float, float]]],
        missing: set[str],
    ) -> tuple[dict[str, Any], tuple[float, float, float, float] | None]:
        symbol = str(holding.get("symbol") or "").upper()
        currency = str(holding.get("currency") or base_currency).upper()
        quantity = number(holding.get("quantity"))
        average_cost = number(holding.get("average_cost"))
        current_price = number(latest.get(symbol, {}).get("close"), average_cost)
        current_fx_quote = self.fx_rate(currency, base_currency)
        if current_fx_quote is None:
            missing.add(currency)
            row = {"symbol": symbol, "currency": currency, "status": "missing_fx_rate"}
            return row, None
        current_fx = number(current_fx_quote.get("rate"), 1)
        fx_lots = acquisition_fx.get(symbol, [])
        fx_quantity = sum(item[0] for item in fx_lots)
        cost_fx = (
            sum(quantity_value * rate for quantity_value, rate in fx_lots) / fx_quantity
            if fx_quantity > 0
            else current_fx
        )
        market_value = quantity * current_price * current_fx
        cost_value = quantity * average_cost * cost_fx
        asset_pnl = quantity * (current_price - average_cost) * cost_fx
        currency_pnl = quantity * current_price * (current_fx - cost_fx)
        row = {
            "symbol": symbol,
            "currency": currency,
            "quantity": rounded(quantity, 6),
            "current_price": rounded(current_price, 6),
            "current_fx_rate": rounded(current_fx, 8),
            "cost_fx_rate": rounded(cost_fx, 8),
            "market_value_base": rounded(market_value, 2),
            "cost_value_base": rounded(cost_value, 2),
            "asset_pnl_base": rounded(asset_pnl, 2),
            "currency_pnl_base": rounded(currency_pnl, 2),
            "total_pnl_base": rounded(market_value - cost_value, 2),
            "status": "ready",
        }
        return row, (market_value, cost_value, asset_pnl, currency_pnl)
