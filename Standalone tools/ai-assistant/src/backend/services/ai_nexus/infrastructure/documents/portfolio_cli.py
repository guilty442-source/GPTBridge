"""Portfolio manager CLI split from the documents module."""
from __future__ import annotations

import argparse
import json
import sys

from .portfolio_constants import *
from .portfolio_io import *
from .portfolio_providers import *
from .portfolio_report import *

def emit_progress(enabled: bool, phase: str, message: str, **payload: Any) -> None:
    if not enabled:
        return
    event = {
        "phase": phase,
        "message": message,
        "timestamp": utc_now_text(),
        **payload,
    }
    print(f"{PROGRESS_JSON_PREFIX}{json.dumps(event, ensure_ascii=False)}", flush=True)


def run_manager(
    *,
    portfolio_file: Path,
    provider_order: list[str],
    watch: bool,
    interval_seconds: int,
    max_cycles: int,
    progress_jsonl: bool,
) -> dict[str, Any]:
    started_at = utc_now_text()
    holdings = load_portfolio(portfolio_file)
    providers = provider_registry()
    snapshots: list[dict[str, Any]] = []
    stop_reason = "completed"
    cycle = 0
    emit_progress(
        progress_jsonl,
        "portfolio_loaded",
        "Portfolio loaded",
        portfolio_file=str(portfolio_file),
        holding_count=len(holdings),
    )

    while True:
        cycle += 1
        emit_progress(
            progress_jsonl,
            "quote_cycle_started",
            f"Quote cycle {cycle} started",
            cycle=cycle,
            holding_count=len(holdings),
        )
        snapshot = create_snapshot(holdings, providers, provider_order)
        snapshots.append(snapshot)
        emit_progress(
            progress_jsonl,
            "quote_snapshot",
            f"Quote cycle {cycle} completed",
            cycle=cycle,
            snapshot=snapshot,
        )

        if not watch:
            break
        if max_cycles > 0 and cycle >= max_cycles:
            stop_reason = "max_cycles_reached"
            break
        market_summary = snapshot.get("market_summary", {})
        if market_summary.get("all_watchable_markets_closed"):
            stop_reason = "market_closed"
            emit_progress(
                progress_jsonl,
                "market_closed",
                "All watchable markets are closed",
                cycle=cycle,
                market_summary=market_summary,
            )
            break
        time.sleep(max(1, interval_seconds))

    latest_snapshot = snapshots[-1] if snapshots else None
    return {
        "ok": True,
        "tool": "ai-assistant/investment-watch",
        "action": "watch" if watch else "snapshot",
        "portfolio_file": str(portfolio_file),
        "started_at": started_at,
        "finished_at": utc_now_text(),
        "provider_order": provider_order,
        "watch": watch,
        "interval_seconds": interval_seconds,
        "cycle_count": len(snapshots),
        "stop_reason": stop_reason,
        "holding_count": len(holdings),
        "latest_snapshot": latest_snapshot,
        "snapshots": snapshots,
        "disclaimer": (
            "Quotes are for monitoring only and may be delayed. "
            "This tool does not provide investment advice."
        ),
    }


def sample_template() -> str:
    return "\n".join(
        [
            "symbol,name,market,quantity,average_cost,currency",
            "AAPL,Apple,US,10,190,USD",
            "2330,TSMC,TW,1000,600,TWD",
            "0700,Tencent,HK,100,300,HKD",
            "BTC,Bitcoin,CRYPTO,0.1,50000,USD",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investment manager")
    parser.add_argument("--portfolio", help="Portfolio CSV/TSV/JSON file")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument(
        "--progress-jsonl",
        action="store_true",
        help="Emit progress JSON lines for the UI",
    )
    parser.add_argument("--watch", action="store_true", help="Poll until markets close")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Maximum watch cycles; 0 means stop only on market close or cancellation",
    )
    parser.add_argument(
        "--providers",
        default=",".join(DEFAULT_PROVIDER_ORDER),
        help="Comma-separated quote provider order",
    )
    parser.add_argument(
        "--template",
        action="store_true",
        help="Print a CSV template",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.template:
        print(sample_template())
        return 0
    if not args.portfolio:
        parser.error("--portfolio is required unless --template is used")

    provider_order = [
        item.strip()
        for item in str(args.providers).split(",")
        if item.strip()
    ] or list(DEFAULT_PROVIDER_ORDER)
    try:
        report = run_manager(
            portfolio_file=Path(args.portfolio).expanduser().resolve(),
            provider_order=provider_order,
            watch=bool(args.watch),
            interval_seconds=max(1, int(args.interval)),
            max_cycles=max(0, int(args.max_cycles)),
            progress_jsonl=bool(args.progress_jsonl),
        )
    except InvestmentManagerError as exc:
        error = {"ok": False, "tool": "ai-assistant/investment-watch", "message": str(exc)}
        if args.json:
            print(json.dumps(error, ensure_ascii=False, indent=2))
        else:
            print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        latest = report.get("latest_snapshot") or {}
        print(
            "Investment manager completed: "
            f"{latest.get('quoted_count', 0)}/{latest.get('holding_count', 0)} quoted"
        )
    return 0
