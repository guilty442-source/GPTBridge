from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Sequence


def _yahoo_chart_url(requested: str, params: dict[str, str]) -> str:
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(requested, safe="")
        + "?"
        + urllib.parse.urlencode(params)
    )


def _quote_field(quote: dict[str, Any], field: str, index: int) -> Any:
    values = quote.get(field) or []
    return values[index] if index < len(values) else None


def _fetch_all(
    candidates: Sequence[dict[str, Any]],
    worker: Callable[[dict[str, Any]], dict[str, Any]],
    max_workers: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    updates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    # §10.30/A590: worker count converges through the five-core budget entry.
    from shared_layer.performance.thread_budget import bounded_workers

    workers = bounded_workers(int(max_workers or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(worker, holding): holding for holding in candidates
        }
        for future in as_completed(futures):
            holding = futures[future]
            try:
                updates.append(future.result())
            except Exception as exc:
                errors.append(
                    {
                        "symbol": str(holding.get("symbol") or ""),
                        "market": str(holding.get("market") or ""),
                        "message": str(exc),
                    }
                )
    return updates, errors
