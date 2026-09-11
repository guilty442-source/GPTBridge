"""Split from portfolio_file.py."""
from __future__ import annotations

from .portfolio_models import *
from .portfolio_headers import *
from .xlsx_consolidated import *
from .xlsx_horizontal import *
from .xlsx_lowlevel import *

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time as local_time, timedelta, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Iterator
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


PROGRESS_JSON_PREFIX = "INVESTMENT_MANAGER_PROGRESS_JSON="
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_REQUEST_TIMEOUT_SECONDS = 12
DEFAULT_PROVIDER_ORDER = (
    "twse",
    "yahoo-chart",
    "yahoo-quote",
    "coingecko",
    "alphavantage",
)
DEFAULT_IMPORT_SNAPSHOT_KEEP = 20
SNAPSHOT_COPY_ATTEMPTS = 3
SNAPSHOT_COPY_CHUNK_BYTES = 1024 * 1024
XLSX_INFERRED_HEADER_SCAN_ROWS = 25
XLSX_HEADER_SCAN_MAX_ROWS = 80
XLSX_HEADER_SCAN_MAX_COLUMNS = 96
XLSX_HORIZONTAL_GROUP_WIDTH = 4
CSV_EXTENSIONS = {".csv", ".tsv", ".txt"}
JSON_EXTENSIONS = {".json"}
XLSX_EXTENSIONS = {".xlsx"}
LEGACY_EXCEL_EXTENSIONS = {".xls"}
EXCEL_EXTENSIONS = XLSX_EXTENSIONS | LEGACY_EXCEL_EXTENSIONS
COMMON_US_ETF_SYMBOLS = {
    "ARKK",
    "DIA",
    "EEM",
    "GLD",
    "IWM",
    "QQQ",
    "SCHD",
    "SLV",
    "SPY",
    "TLT",
    "VIG",
    "VOO",
    "VT",
    "VTI",
    "VXUS",
    "XLE",
    "XLF",
    "XLK",
}
CRYPTO_ID_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "USDT": "tether",
    "USDC": "usd-coin",
}
HEADER_ALIASES = {
    "symbol": {
        "symbol",
        "ticker",
        "code",
        "stockcode",
        "security",
        "securities",
        "isin",
        "stock",
        "stockid",
        "securityid",
        "productid",
        "instrument",
        "\u4ee3\u865f",
        "\u4ee3\u78bc",
        "\u80a1\u7968",
        "\u80a1\u7968\u4ee3\u865f",
        "\u80a1\u7968\u4ee3\u78bc",
        "\u80a1\u865f",
        "\u8b49\u5238\u4ee3\u865f",
        "\u8b49\u5238\u4ee3\u78bc",
        "\u5546\u54c1\u4ee3\u865f",
        "\u5546\u54c1\u4ee3\u78bc",
        "\u6a19\u7684",
        "\u6a19\u7684\u4ee3\u865f",
        "\u6a19\u7684\u4ee3\u78bc",
    },
    "name": {
        "name",
        "securityname",
        "stockname",
        "productname",
        "instrumentname",
        "\u540d\u7a31",
        "\u80a1\u540d",
        "\u8b49\u5238\u540d\u7a31",
        "\u80a1\u7968\u540d\u7a31",
        "\u5546\u54c1\u540d\u7a31",
        "\u6a19\u7684\u540d\u7a31",
    },
    "market": {
        "market",
        "exchange",
        "region",
        "\u5e02\u5834",
        "\u5e02\u5834\u5225",
        "\u4ea4\u6613\u6240",
        "\u4ea4\u6613\u5e02\u5834",
        "\u5340\u57df",
    },
    "asset_type": {
        "type",
        "assettype",
        "category",
        "assetclass",
        "\u985e\u578b",
        "\u5546\u54c1\u985e\u578b",
        "\u8cc7\u7522\u985e\u5225",
        "\u8cc7\u7522\u985e\u578b",
    },
    "quantity": {
        "quantity",
        "qty",
        "shares",
        "units",
        "position",
        "holding",
        "balance",
        "availablequantity",
        "\u80a1\u6578",
        "\u5f35\u6578",
        "\u55ae\u4f4d",
        "\u5eab\u5b58",
        "\u5eab\u5b58\u6578\u91cf",
        "\u5eab\u5b58\u80a1\u6578",
        "\u6301\u80a1",
        "\u6301\u6709\u80a1\u6578",
        "\u6301\u6709\u6578\u91cf",
        "\u6578\u91cf",
    },
    "average_cost": {
        "averagecost",
        "avgcost",
        "averageprice",
        "avgprice",
        "cost",
        "costbasisprice",
        "price",
        "unitcost",
        "\u6210\u672c",
        "\u6210\u672c\u50f9",
        "\u55ae\u4f4d\u6210\u672c",
        "\u8cb7\u9032\u6210\u672c",
        "\u5e73\u5747\u6210\u672c",
        "\u5e73\u5747\u6210\u672c\u50f9",
        "\u5e73\u5747\u50f9",
        "\u5e73\u5747\u8cb7\u9032\u6210\u672c",
        "\u6210\u4ea4\u5747\u50f9",
        "\u8cb7\u9032\u5747\u50f9",
        "\u5747\u50f9",
    },
    "currency": {"currency", "ccy", "\u5e63\u5225", "\u4ea4\u6613\u5e63\u5225", "\u8ca8\u5e63"},
    "principal_amount": {
        "principal",
        "principalamount",
        "costamount",
        "\u672c\u91d1",
        "\u6295\u5165\u672c\u91d1",
        "\u6295\u5165\u91d1\u984d",
        "\u6210\u672c\u91d1\u984d",
    },
    "principal_currency": {
        "principalcurrency",
        "principalccy",
        "\u672c\u91d1\u5e63\u5225",
        "\u6210\u672c\u5e63\u5225",
    },
    "principal_twd": {
        "principaltwd",
        "\u672c\u91d1twd",
        "\u53f0\u5e63\u672c\u91d1",
        "\u65b0\u53f0\u5e63\u672c\u91d1",
    },
}
XLSX_MAPPING_FIELDS = tuple(HEADER_ALIASES)
XLSX_REQUIRED_MAPPING_FIELDS = frozenset({"symbol", "quantity"})
MARKET_ALIASES = {
    "US": {"us", "usa", "nyse", "nasdaq", "amex", "\u7f8e\u80a1", "\u7f8e\u570b"},
    "TW": {
        "tw",
        "tpe",
        "twse",
        "tpex",
        "taiwan",
        "\u53f0\u80a1",
        "\u81fa\u80a1",
        "\u53f0\u7063",
    },
    "HK": {"hk", "hkg", "hkex", "hongkong", "\u6e2f\u80a1", "\u9999\u6e2f"},
    "CRYPTO": {
        "crypto",
        "coin",
        "\u52a0\u5bc6",
        "\u865b\u64ec\u8ca8\u5e63",
        "\u52a0\u5bc6\u8ca8\u5e63",
    },
    "FUND": {"fund", "mutualfund", "\u57fa\u91d1"},
    "INDEX": {"index", "indice", "indices", "\u6307\u6578"},
}
MARKET_SESSIONS = {
    "US": {
        "timezone": "America/New_York",
        "sessions": [(local_time(9, 30), local_time(16, 0))],
    },
    "TW": {
        "timezone": "Asia/Taipei",
        "sessions": [(local_time(9, 0), local_time(13, 30))],
    },
    "HK": {
        "timezone": "Asia/Hong_Kong",
        "sessions": [
            (local_time(9, 30), local_time(12, 0)),
            (local_time(13, 0), local_time(16, 0)),
        ],
    },
}



def local_device_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now() -> datetime:
    return local_device_now()


def utc_now_text() -> str:
    return utc_now().isoformat()


def timezone_for(name: str, now: datetime | None = None) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        pass
    if name in {"Asia/Taipei", "Asia/Hong_Kong"}:
        return timezone(timedelta(hours=8), name)
    if name == "America/New_York":
        reference = now or utc_now()
        offset = -4 if 3 <= reference.month <= 11 else -5
        return timezone(timedelta(hours=offset), name)
    return timezone.utc


@lru_cache(maxsize=1024)

@contextmanager
def _open_portfolio_source_shared(source: Path) -> Iterator[BinaryIO]:
    if os.name != "nt":
        with source.open("rb") as source_file:
            yield source_file
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_sequential_scan = 0x08000000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(source),
        generic_read,
        file_share_read | file_share_write | file_share_delete,
        None,
        open_existing,
        file_attribute_normal | file_flag_sequential_scan,
        None,
    )
    invalid_handle_value = wintypes.HANDLE(-1).value
    if handle in (None, invalid_handle_value):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, ctypes.FormatError(error_code), str(source))

    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | os.O_BINARY,
        )
    except OSError:
        close_handle(handle)
        raise

    with os.fdopen(descriptor, "rb") as source_file:
        yield source_file


def _source_revision_unchanged(before: os.stat_result, after: os.stat_result) -> bool:
    return before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns


def _read_portfolio_source_bytes(source: Path) -> bytes:
    for attempt in range(SNAPSHOT_COPY_ATTEMPTS):
        with _open_portfolio_source_shared(source) as source_file:
            revision_before = os.fstat(source_file.fileno())
            content = source_file.read()
            revision_after = os.fstat(source_file.fileno())
        if _source_revision_unchanged(revision_before, revision_after):
            return content
        if attempt + 1 < SNAPSHOT_COPY_ATTEMPTS:
            time.sleep(0.05)
    raise InvestmentManagerError(
        "Portfolio file kept changing while a non-locking read was in progress."
    )


def _read_portfolio_source_text(source: Path, *, encoding: str) -> str:
    return _read_portfolio_source_bytes(source).decode(encoding)


@contextmanager
def _open_xlsx_workbook_unlocked(path: Path) -> Iterator[zipfile.ZipFile]:
    workbook_bytes = _read_portfolio_source_bytes(path)
    with io.BytesIO(workbook_bytes) as workbook_stream:
        with zipfile.ZipFile(workbook_stream) as workbook:
            yield workbook


def _copy_portfolio_snapshot_once(source: Path, partial_target: Path) -> bool:
    with _open_portfolio_source_shared(source) as source_file:
        revision_before = os.fstat(source_file.fileno())
        with partial_target.open("xb") as target_file:
            while chunk := source_file.read(SNAPSHOT_COPY_CHUNK_BYTES):
                target_file.write(chunk)
        revision_after = os.fstat(source_file.fileno())
    return _source_revision_unchanged(revision_before, revision_after)


def _is_owned_snapshot_partial(partial_target: Path, target: Path) -> bool:
    """Prove a partial belongs to this snapshot transaction before cleanup."""

    if partial_target.parent != target.parent or partial_target.is_symlink():
        return False
    prefix = f".{target.name}."
    suffix = ".partial"
    name = partial_target.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return False
    token = name[len(prefix) : -len(suffix)]
    return bool(re.fullmatch(r"[a-f0-9]{32}", token))


def create_portfolio_file_snapshot(
    source: Path,
    imports_root: Path,
    *,
    keep: int = DEFAULT_IMPORT_SNAPSHOT_KEEP,
) -> Path:
    imports_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._-")
    safe_stem = (safe_stem or "portfolio")[:80]
    target = imports_root / f"{stamp}-{uuid.uuid4().hex}-{safe_stem}{source.suffix}"
    last_error: OSError | InvestmentManagerError | None = None
    for attempt in range(SNAPSHOT_COPY_ATTEMPTS):
        partial_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
        try:
            if not _copy_portfolio_snapshot_once(source, partial_target):
                last_error = InvestmentManagerError(
                    "Portfolio file changed while the import snapshot was being created."
                )
            else:
                if target.exists() or target.is_symlink():
                    raise InvestmentManagerError(
                        "Snapshot publication target already exists; refusing to overwrite it."
                    )
                partial_target.replace(target)
                prune_portfolio_file_snapshots(imports_root, keep=keep)
                return target
        except OSError as exc:
            last_error = exc
        finally:
            if _is_owned_snapshot_partial(partial_target, target):
                try:
                    partial_target.unlink(missing_ok=True)
                except OSError:
                    pass
        if attempt + 1 < SNAPSHOT_COPY_ATTEMPTS:
            time.sleep(0.05)

    raise InvestmentManagerError(
        f"Unable to create a non-locking portfolio import snapshot: {last_error}"
    ) from last_error


def prune_portfolio_file_snapshots(
    imports_root: Path,
    *,
    keep: int = DEFAULT_IMPORT_SNAPSHOT_KEEP,
) -> dict[str, Any]:
    """Report logical archive pressure without deleting source snapshots."""

    try:
        snapshots = sorted(
            (
                item
                for item in imports_root.iterdir()
                if item.is_file() and not item.name.endswith(".partial")
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return {
            "retained_count": 0,
            "logical_archive_count": 0,
            "storage_pressure": False,
            "automatic_delete": False,
        }
    active_limit = max(0, int(keep))
    logical_archive_count = max(0, len(snapshots) - active_limit)
    return {
        "retained_count": len(snapshots),
        "active_window_count": min(len(snapshots), active_limit),
        "logical_archive_count": logical_archive_count,
        "storage_pressure": logical_archive_count > max(100, active_limit * 5),
        "automatic_delete": False,
    }


def load_portfolio(path: Path) -> list[Holding]:
    if not path.exists():
        raise InvestmentManagerError(f"Portfolio file not found: {path}")
    if not path.is_file():
        raise InvestmentManagerError(f"Portfolio path is not a file: {path}")
    suffix = path.suffix.casefold()
    if suffix in XLSX_EXTENSIONS:
        return load_xlsx_portfolio(path)
    if suffix in LEGACY_EXCEL_EXTENSIONS:
        raise InvestmentManagerError(
            "Legacy .xls import is not supported. Save the workbook as .xlsx first, or use conversion to CSV or JSON."
        )
    if suffix in JSON_EXTENSIONS:
        return load_json_portfolio(path)
    if suffix in CSV_EXTENSIONS or suffix == "":
        return load_csv_portfolio(path)
    raise InvestmentManagerError(f"Unsupported portfolio file type: {suffix}")


def load_json_portfolio(path: Path) -> list[Holding]:
    try:
        payload = json.loads(_read_portfolio_source_text(path, encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise InvestmentManagerError(f"Invalid JSON portfolio: {exc}") from exc
    if isinstance(payload, dict):
        rows = payload.get("holdings") or payload.get("positions") or payload.get("data")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise InvestmentManagerError("JSON portfolio must be a list or contain holdings.")
    return rows_to_holdings(rows)


def load_csv_portfolio(path: Path) -> list[Holding]:
    text = _read_portfolio_source_text(path, encoding="utf-8-sig")
    if not text.strip():
        raise InvestmentManagerError("Portfolio file is empty.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.casefold() == ".tsv" else csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    rows = list(reader)
    if not reader.fieldnames:
        raise InvestmentManagerError("Portfolio CSV has no header row.")
    return rows_to_holdings(rows)


def load_xlsx_portfolio(path: Path) -> list[Holding]:
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(
            f"Invalid Excel workbook: {exc}. Use a valid .xlsx file, or use conversion to CSV or JSON."
        ) from exc
    sheets = workbook_scan.get("sheets", [])
    if not any(sheet.get("row_count", 0) for sheet in sheets):
        raise InvestmentManagerError("Excel workbook has no rows.")

    usable_sheets = [
        sheet
        for sheet in sheets
        if sheet.get("usable") and isinstance(sheet.get("rows"), list)
    ]
    if not usable_sheets:
        scanned = ", ".join(
            f"{sheet.get('sheet_name', 'Sheet')}({sheet.get('row_count', 0)} rows)"
            for sheet in sheets
        )
        raise InvestmentManagerError(
            "Excel workbook has no recognizable header row. Required column: symbol / 代號."
            + (f" Scanned sheets: {scanned}." if scanned else "")
        )

    errors: list[str] = []
    for sheet in sorted(
        usable_sheets,
        key=lambda item: int(item.get("score", 0)),
        reverse=True,
    ):
        records = xlsx_records_from_rows(
            sheet["rows"],
            int(sheet["header_row_index"]),
            [str(header) for header in sheet.get("headers", [])],
        )
        try:
            return rows_to_holdings(records)
        except InvestmentManagerError as exc:
            errors.append(f"{sheet.get('sheet_name', 'Sheet')}: {exc}")

    detail = "; ".join(errors) if errors else "No usable data rows."
    raise InvestmentManagerError(f"Excel workbook has no usable holdings after scanning. {detail}")



__all__ = ['PROGRESS_JSON_PREFIX', 'DEFAULT_INTERVAL_SECONDS', 'DEFAULT_REQUEST_TIMEOUT_SECONDS', 'DEFAULT_PROVIDER_ORDER', 'DEFAULT_IMPORT_SNAPSHOT_KEEP', 'SNAPSHOT_COPY_ATTEMPTS', 'SNAPSHOT_COPY_CHUNK_BYTES', 'XLSX_INFERRED_HEADER_SCAN_ROWS', 'XLSX_HEADER_SCAN_MAX_ROWS', 'XLSX_HEADER_SCAN_MAX_COLUMNS', 'XLSX_HORIZONTAL_GROUP_WIDTH', 'CSV_EXTENSIONS', 'JSON_EXTENSIONS', 'XLSX_EXTENSIONS', 'LEGACY_EXCEL_EXTENSIONS', 'EXCEL_EXTENSIONS', 'COMMON_US_ETF_SYMBOLS', 'CRYPTO_ID_MAP', 'HEADER_ALIASES', 'XLSX_MAPPING_FIELDS', 'XLSX_REQUIRED_MAPPING_FIELDS', 'MARKET_ALIASES', 'MARKET_SESSIONS', 'local_device_now', 'utc_now', 'utc_now_text', 'timezone_for', '_open_portfolio_source_shared', '_source_revision_unchanged', '_read_portfolio_source_bytes', '_read_portfolio_source_text', '_open_xlsx_workbook_unlocked', '_copy_portfolio_snapshot_once', '_is_owned_snapshot_partial', 'create_portfolio_file_snapshot', 'prune_portfolio_file_snapshots', 'load_portfolio', 'load_json_portfolio', 'load_csv_portfolio', 'load_xlsx_portfolio']
