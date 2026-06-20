"""AI Assistant standalone application entry."""

from __future__ import annotations

from pathlib import Path
import sys


SERVICES_ROOT = Path(__file__).resolve().parent / "backend" / "services"
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

from ai_nexus import investment_manager_core as _investment_manager_core


# Compatibility exports for legacy loaders that still import this standalone
# entry as the former investment watch main module.
Holding = _investment_manager_core.Holding
InvestmentManagerError = _investment_manager_core.InvestmentManagerError
QuoteProviderError = _investment_manager_core.QuoteProviderError
load_portfolio = _investment_manager_core.load_portfolio
load_json_portfolio = _investment_manager_core.load_json_portfolio
load_csv_portfolio = _investment_manager_core.load_csv_portfolio
load_xlsx_portfolio = _investment_manager_core.load_xlsx_portfolio
scan_xlsx_workbook = _investment_manager_core.scan_xlsx_workbook
create_snapshot = _investment_manager_core.create_snapshot
run_manager = _investment_manager_core.run_manager
provider_registry = _investment_manager_core.provider_registry
quote_holding = _investment_manager_core.quote_holding
market_status = _investment_manager_core.market_status
utc_now = _investment_manager_core.utc_now


def main() -> None:
    workspace = Path(__file__).resolve().parent.parent
    print("AI Investment Manager is registered as a standalone GPTBridge application.")
    print(f"Project folder: {workspace}")
    print("Open AI投資管家 from the Applications screen.")


if __name__ == "__main__":
    main()
