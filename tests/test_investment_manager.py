from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "platform_tools"
    / "ai-assistant"
    / "src"
    / "backend"
    / "services"
    / "ai_nexus"
    / "investment_manager_core.py"
)
SPEC = importlib.util.spec_from_file_location("investment_manager_main", MODULE_PATH)
assert SPEC and SPEC.loader
investment_manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = investment_manager
SPEC.loader.exec_module(investment_manager)

REPOSITORY_PATH = (
    ROOT
    / "platform_tools"
    / "ai-assistant"
    / "src"
    / "backend"
    / "services"
    / "ai_nexus"
    / "investment_repository.py"
)
REPOSITORY_SPEC = importlib.util.spec_from_file_location(
    "investment_watch_repository", REPOSITORY_PATH
)
assert REPOSITORY_SPEC and REPOSITORY_SPEC.loader
investment_watch_repository = importlib.util.module_from_spec(REPOSITORY_SPEC)
sys.modules[REPOSITORY_SPEC.name] = investment_watch_repository
REPOSITORY_SPEC.loader.exec_module(investment_watch_repository)


def _xlsx_column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _xlsx_cell_xml(row_number: int, column_index: int, value: Any) -> str:
    reference = f"{_xlsx_column_name(column_index)}{row_number}"
    if value is None:
        return f'<c r="{reference}"/>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    return (
        f'<c r="{reference}" t="inlineStr">'
        f"<is><t>{escape(str(value))}</t></is>"
        "</c>"
    )


def _write_minimal_xlsx(
    path: Path,
    sheets: list[tuple[str, list[list[Any]]] | tuple[str, list[list[Any]], list[str]]],
) -> None:
    workbook_sheets = []
    relationships = []
    content_overrides = []
    sheet_files: list[tuple[str, str]] = []
    for index, sheet in enumerate(sheets, start=1):
        sheet_name = sheet[0]
        rows = sheet[1]
        merge_refs = sheet[2] if len(sheet) > 2 else []
        workbook_sheets.append(
            f'<sheet name="{escape(sheet_name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
        relationships.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        content_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        row_xml = []
        for row_position, row in enumerate(rows, start=1):
            row_index = row_position
            values = row
            if (
                isinstance(row, tuple)
                and len(row) == 2
                and isinstance(row[0], int)
                and isinstance(row[1], list)
            ):
                row_index = row[0]
                values = row[1]
            cells = "".join(
                _xlsx_cell_xml(row_index, column_index, value)
                for column_index, value in enumerate(values)
            )
            row_xml.append(f'<row r="{row_index}">{cells}</row>')
        merge_xml = ""
        if merge_refs:
            merge_cells = "".join(
                f'<mergeCell ref="{escape(reference)}"/>'
                for reference in merge_refs
            )
            merge_xml = f'<mergeCells count="{len(merge_refs)}">{merge_cells}</mergeCells>'
        sheet_files.append(
            (
                f"xl/worksheets/sheet{index}.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData>{''.join(row_xml)}</sheetData>"
                f"{merge_xml}"
                "</worksheet>",
            )
        )

    with zipfile.ZipFile(path, "w") as workbook:
        workbook.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f"{''.join(content_overrides)}"
            "</Types>",
        )
        workbook.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        workbook.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{''.join(workbook_sheets)}</sheets>"
            "</workbook>",
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{''.join(relationships)}"
            "</Relationships>",
        )
        for sheet_path, sheet_xml in sheet_files:
            workbook.writestr(sheet_path, sheet_xml)


def test_load_portfolio_accepts_chinese_headers(tmp_path: Path) -> None:
    portfolio = tmp_path / "holdings.csv"
    portfolio.write_text(
        "代號,市場,股數,成本,幣別\n"
        "2330,台股,1000,600,TWD\n"
        "AAPL,美股,10,190,USD\n",
        encoding="utf-8",
    )

    holdings = investment_manager.load_portfolio(portfolio)

    assert [holding.symbol for holding in holdings] == ["2330", "AAPL"]
    assert holdings[0].market == "TW"
    assert holdings[0].quantity == 1000
    assert holdings[0].average_cost == 600
    assert holdings[1].market == "US"


def test_xlsx_portfolio_scans_header_below_title_rows(tmp_path: Path) -> None:
    portfolio = tmp_path / "holdings.xlsx"
    _write_minimal_xlsx(
        portfolio,
        [
            (
                "持股",
                [
                    ["投資組合匯出"],
                    [""],
                    ["股票代碼", "市場別", "庫存股數", "平均成本", "幣別"],
                    ["2330", "台股", 1000, 600, "TWD"],
                    ["AAPL", "美股", 10, 190, "USD"],
                ],
            )
        ],
    )

    holdings = investment_manager.load_portfolio(portfolio)
    scan = investment_manager.scan_xlsx_workbook(portfolio)

    assert [holding.symbol for holding in holdings] == ["2330", "AAPL"]
    assert scan["selected_sheet"]["sheet_name"] == "持股"
    assert scan["selected_sheet"]["header_row_number"] == 3


def test_xlsx_portfolio_scans_all_workbook_sheets(tmp_path: Path) -> None:
    portfolio = tmp_path / "holdings.xlsx"
    _write_minimal_xlsx(
        portfolio,
        [
            ("說明", [["這是匯出說明"], ["日期", "2026-07-05"]]),
            (
                "持股明細",
                [
                    ["報表標題"],
                    ["代碼", "市場", "數量", "成本價"],
                    ["NVDA", "US", 2, 120],
                ],
            ),
        ],
    )

    holdings = investment_manager.load_portfolio(portfolio)
    scan = investment_manager.scan_xlsx_workbook(portfolio)

    assert [holding.symbol for holding in holdings] == ["NVDA"]
    assert scan["sheet_count"] == 2
    assert scan["selected_sheet"]["sheet_name"] == "持股明細"
    assert scan["selected_sheet"]["header_row_number"] == 2


def test_xlsx_scan_ignores_header_like_summary_without_holdings(tmp_path: Path) -> None:
    portfolio = tmp_path / "holdings.xlsx"
    _write_minimal_xlsx(
        portfolio,
        [
            (
                "summary",
                [
                    ["symbol", "quantity", "average_cost"],
                    ["TOTAL", 12, 100],
                    ["updated_at", "2026-07-05", ""],
                ],
            ),
            (
                "holdings",
                [
                    ["symbol", "market", "quantity", "average_cost"],
                    ["MSFT", "US", 3, 120],
                ],
            ),
        ],
    )

    holdings = investment_manager.load_portfolio(portfolio)
    scan = investment_manager.scan_xlsx_workbook(portfolio)

    assert [holding.symbol for holding in holdings] == ["MSFT"]
    assert scan["selected_sheet"]["sheet_name"] == "holdings"
    summary_scan = scan["sheets"][0]
    assert summary_scan["usable"] is False
    assert summary_scan["valid_data_row_count"] == 0


def test_xlsx_scan_reports_real_excel_row_numbers_for_sparse_rows(tmp_path: Path) -> None:
    portfolio = tmp_path / "holdings.xlsx"
    _write_minimal_xlsx(
        portfolio,
        [
            (
                "holdings",
                [
                    (5, ["symbol", "market", "quantity", "average_cost"]),
                    (6, ["AAPL", "US", 2, 190]),
                ],
            )
        ],
    )

    scan = investment_manager.scan_xlsx_workbook(portfolio)
    holdings = investment_manager.load_portfolio(portfolio)

    assert scan["selected_sheet"]["header_row_number"] == 5
    assert [holding.symbol for holding in holdings] == ["AAPL"]


def test_xlsx_scan_infers_self_made_table_columns(tmp_path: Path) -> None:
    portfolio = tmp_path / "custom-holdings.xlsx"
    _write_minimal_xlsx(
        portfolio,
        [
            (
                "my table",
                [
                    ["自製投資表"],
                    ["追蹤項目", "所在地", "持倉", "入場"],
                    ["AAPL", "US", 10, 190],
                    ["2330", "TW", 1000, 600],
                ],
            )
        ],
    )

    holdings = investment_manager.load_portfolio(portfolio)
    scan = investment_manager.scan_xlsx_workbook(portfolio)

    assert [holding.symbol for holding in holdings] == ["AAPL", "2330"]
    assert holdings[0].quantity == 10
    assert holdings[0].average_cost == 190
    assert holdings[1].market == "TW"
    assert scan["selected_sheet"]["header_mode"] == "inferred"
    assert scan["selected_sheet"]["headers"][:4] == [
        "symbol",
        "market",
        "quantity",
        "average_cost",
    ]


def test_xlsx_scan_combines_two_row_self_made_headers(tmp_path: Path) -> None:
    portfolio = tmp_path / "custom-two-row-holdings.xlsx"
    _write_minimal_xlsx(
        portfolio,
        [
            (
                "holdings",
                [
                    ["Stock", "Position", "Entry"],
                    ["ID", "Shares", "Price"],
                    ["MSFT", 3, 120],
                ],
            )
        ],
    )

    holdings = investment_manager.load_portfolio(portfolio)
    scan = investment_manager.scan_xlsx_workbook(portfolio)

    assert [holding.symbol for holding in holdings] == ["MSFT"]
    assert holdings[0].quantity == 3
    assert holdings[0].average_cost == 120
    assert scan["selected_sheet"]["header_depth"] == 2
    assert scan["selected_sheet"]["headers"][:3] == [
        "Stock ID",
        "Position Shares",
        "Entry Price",
    ]


def test_xlsx_scan_expands_merged_self_made_header_cells(tmp_path: Path) -> None:
    portfolio = tmp_path / "custom-merged-header-holdings.xlsx"
    _write_minimal_xlsx(
        portfolio,
        [
            (
                "holdings",
                [
                    ["Stock", "Position", None],
                    ["ID", "Shares", "Price"],
                    ["TSLA", 5, 180],
                ],
                ["B1:C1"],
            )
        ],
    )

    holdings = investment_manager.load_portfolio(portfolio)
    scan = investment_manager.scan_xlsx_workbook(portfolio)

    assert [holding.symbol for holding in holdings] == ["TSLA"]
    assert holdings[0].quantity == 5
    assert holdings[0].average_cost == 180
    assert scan["selected_sheet"]["headers"][:3] == [
        "Stock ID",
        "Position Shares",
        "Position Price",
    ]


def test_quote_holding_falls_back_to_next_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request_json(url: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if "mis.twse.com.tw" in url:
            return {"msgArray": []}
        if "finance/chart" in url:
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "symbol": "2330.TW",
                                "regularMarketPrice": 700,
                                "chartPreviousClose": 690,
                                "currency": "TWD",
                                "regularMarketTime": 1781676000,
                                "marketState": "REGULAR",
                                "fullExchangeName": "Taiwan",
                            }
                        }
                    ],
                    "error": None,
                }
            }
        raise AssertionError(url)

    monkeypatch.setattr(investment_manager, "request_json", fake_request_json)
    holding = investment_manager.Holding(
        symbol="2330",
        market="TW",
        quantity=1000,
        average_cost=600,
        currency="TWD",
    )
    providers = investment_manager.provider_registry()

    quote, attempts = investment_manager.quote_holding(
        holding,
        providers,
        ["twse", "yahoo-chart"],
        investment_manager.utc_now(),
    )

    assert quote is not None
    assert quote.provider == "yahoo-chart"
    assert quote.price == 700
    assert [(attempt.provider, attempt.ok) for attempt in attempts] == [
        ("twse", False),
        ("yahoo-chart", True),
    ]


def test_watch_stops_after_market_close_without_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portfolio = tmp_path / "holdings.csv"
    portfolio.write_text("symbol,market,quantity\nAAPL,US,1\n", encoding="utf-8")

    def fake_snapshot(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "timestamp": "2026-06-17T00:00:00+00:00",
            "holding_count": 1,
            "quoted_count": 1,
            "failed_quote_count": 0,
            "holdings": [],
            "totals_by_currency": {},
            "market_summary": {
                "markets": {"US": {"state": "closed", "is_open": False}},
                "open_markets": [],
                "watchable_markets": ["US"],
                "all_watchable_markets_closed": True,
            },
        }

    monkeypatch.setattr(investment_manager, "create_snapshot", fake_snapshot)
    monkeypatch.setattr(
        investment_manager.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("sleep not expected")),
    )

    report = investment_manager.run_manager(
        portfolio_file=portfolio,
        provider_order=["yahoo-chart"],
        watch=True,
        interval_seconds=60,
        max_cycles=0,
        progress_jsonl=False,
    )

    assert report["stop_reason"] == "market_closed"
    assert report["cycle_count"] == 1


def test_excel_import_returns_actionable_error(tmp_path: Path) -> None:
    portfolio = tmp_path / "holdings.xlsx"
    portfolio.write_bytes(b"not-an-xlsx")

    with pytest.raises(investment_manager.InvestmentManagerError) as exc_info:
        investment_manager.load_portfolio(portfolio)

    assert "conversion to CSV or JSON" in str(exc_info.value)


def test_investment_watch_repository_clear_state_removes_old_data(tmp_path: Path) -> None:
    repository = investment_watch_repository.InvestmentWatchRepository(tmp_path)
    state = repository.save_portfolio(
        tmp_path / "holdings.xlsx",
        [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "market": "US",
                "quantity": 1,
                "average_cost": 100,
                "currency": "USD",
            }
        ],
    )
    assert state["holdings"]
    assert repository.state_path.exists()

    cleared = repository.clear_state()

    assert repository.state_path.exists() is False
    assert cleared["portfolio"] is None
    assert cleared["holdings"] == []
    assert cleared["ai_runs"] == []
    assert "尚未匯入持股" in cleared["shared_memory"]


def test_market_status_falls_back_when_zoneinfo_data_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_zoneinfo(_name: str) -> object:
        raise RuntimeError("missing tzdata")

    monkeypatch.setattr(investment_manager, "ZoneInfo", fail_zoneinfo)

    status = investment_manager.market_status(
        "US",
        investment_manager.datetime.fromisoformat("2026-06-17T14:00:00+00:00"),
    )

    assert status["timezone"] == "America/New_York"
    assert status["state"] == "open"
