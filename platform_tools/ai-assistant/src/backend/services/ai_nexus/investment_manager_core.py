"""Investment manager tool entry."""

from __future__ import annotations

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
import urllib.request
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


class InvestmentManagerError(Exception):
    """Tool-specific error."""


class QuoteProviderError(Exception):
    """Raised when one quote provider cannot return a valid quote."""


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str = ""
    market: str = ""
    asset_type: str = ""
    quantity: float = 0.0
    average_cost: float | None = None
    currency: str = ""
    principal_amount: float | None = None
    principal_currency: str = ""
    principal_twd: float | None = None
    source_row: int | None = None
    dividend_amount_twd: float | None = None
    dividend_per_unit: float | None = None
    monthly_dividend_twd: float | None = None
    annual_dividend_yield_percent: float | None = None
    payback_rate_percent: float | None = None
    current_value_twd: float | None = None
    estimated_annual_dividend_twd: float | None = None
    estimated_weekly_dividend_twd: float | None = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    requested_symbol: str
    provider: str
    price: float
    currency: str = ""
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    as_of: str = ""
    market_state: str = ""
    exchange: str = ""
    raw_market: str = ""


@dataclass
class QuoteAttempt:
    provider: str
    ok: bool
    message: str


@dataclass
class QuoteContext:
    holding: Holding
    now_utc: datetime
    attempts: list[QuoteAttempt] = field(default_factory=list)


def local_device_now() -> datetime:
    return datetime.now().astimezone()


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


@lru_cache(maxsize=8192)
def normalize_header(value: str) -> str:
    return re.sub(
        r"[\s_\-()\uff08\uff09./:：,，;；\[\]【】{}（）「」『』]+",
        "",
        str(value).strip().casefold(),
    )


def normalized_header_tokens(value: str) -> list[str]:
    return [
        token
        for token in (
            normalize_header(part)
            for part in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", str(value))
        )
        if token
    ]


@lru_cache(maxsize=1)
def header_alias_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            normalized_alias = normalize_header(alias)
            if normalized_alias:
                lookup[normalized_alias] = canonical
    return lookup


@lru_cache(maxsize=1)
def header_substring_aliases() -> tuple[tuple[str, str], ...]:
    aliases: list[tuple[str, str]] = []
    for canonical, raw_aliases in HEADER_ALIASES.items():
        for alias in raw_aliases:
            normalized_alias = normalize_header(alias)
            if len(normalized_alias) >= 2:
                aliases.append((normalized_alias, canonical))
    return tuple(aliases)


@lru_cache(maxsize=1)
def market_alias_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for market, aliases in MARKET_ALIASES.items():
        lookup[normalize_header(market)] = market
        for alias in aliases:
            normalized_alias = normalize_header(alias)
            if normalized_alias:
                lookup[normalized_alias] = market
    return lookup


@lru_cache(maxsize=1)
def header_token_lookup() -> frozenset[str]:
    return frozenset(
        normalize_header(token)
        for token in (
            "id",
            "code",
            "symbol",
            "ticker",
            "stock",
            "name",
            "market",
            "qty",
            "quantity",
            "shares",
            "units",
            "position",
            "price",
            "cost",
            "avg",
            "entry",
            "\u4ee3\u865f",
            "\u4ee3\u78bc",
            "\u540d\u7a31",
            "\u6578\u91cf",
            "\u6301\u5009",
            "\u6210\u672c",
            "\u50f9\u683c",
        )
    )


@lru_cache(maxsize=8192)
def canonical_column(header: str) -> str | None:
    normalized = normalize_header(header)
    if not normalized:
        return None
    alias_lookup = header_alias_lookup()
    if normalized in alias_lookup:
        return alias_lookup[normalized]

    token_matches = [
        (index, token, alias_lookup[token])
        for index, token in enumerate(normalized_header_tokens(header))
        if token in alias_lookup
    ]
    if token_matches:
        _index, token, canonical = token_matches[-1]
        if canonical == "symbol" and token in {"code", "id"}:
            previous = next(
                (
                    previous_canonical
                    for _previous_index, _previous_token, previous_canonical in reversed(token_matches[:-1])
                    if previous_canonical != "symbol"
                ),
                None,
            )
            if previous:
                return previous
        return canonical

    substring_matches: list[tuple[bool, int, bool, str]] = []
    for normalized_alias, canonical in header_substring_aliases():
        if normalized_alias not in normalized:
            continue
        suffix_match = normalized.endswith(normalized_alias)
        generic_symbol_suffix = canonical == "symbol" and normalized_alias in {"code", "id"}
        substring_matches.append(
            (
                suffix_match and not generic_symbol_suffix,
                len(normalized_alias),
                suffix_match,
                canonical,
            )
        )
    if substring_matches:
        return sorted(substring_matches, reverse=True)[0][3]
    return None


def canonical_columns(headers: Iterable[str]) -> list[str]:
    return [
        canonical
        for canonical in (canonical_column(header) for header in headers)
        if canonical
    ]


def canonical_header_map(headers: Iterable[str]) -> list[str]:
    mapped: list[str] = []
    seen: set[str] = set()
    for header in headers:
        canonical = canonical_column(header)
        if canonical and canonical not in seen:
            mapped.append(canonical)
            seen.add(canonical)
        else:
            mapped.append("")
    return mapped


def portfolio_header_score(headers: Iterable[str]) -> int:
    canonical_headers = set(canonical_columns(headers))
    if "symbol" not in canonical_headers:
        return 0
    score = 100 + len(canonical_headers) * 10
    for preferred in ("quantity", "market", "average_cost", "currency"):
        if preferred in canonical_headers:
            score += 20
    return score


def normalize_market(value: str) -> str:
    normalized = normalize_header(value)
    if not normalized:
        return ""
    return market_alias_lookup().get(normalized, normalized.upper())


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[$,%\s,\uff0c]", "", text)
    text = text.replace("NT", "").replace("TWD", "").replace("USD", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def infer_market(symbol: str, explicit_market: str = "", asset_type: str = "") -> str:
    market = normalize_market(explicit_market)
    if market:
        return market
    normalized_type = normalize_market(asset_type)
    if normalized_type in {"CRYPTO", "FUND", "INDEX"}:
        return normalized_type
    symbol_upper = symbol.strip().upper()
    if symbol_upper.endswith((".TW", ".TWO")):
        return "TW"
    if symbol_upper.endswith(".HK"):
        return "HK"
    if symbol_upper.endswith("-USD") or "/" in symbol_upper:
        return "CRYPTO"
    if symbol_upper.startswith("^"):
        return "INDEX"
    if re.fullmatch(r"\d{4,6}", symbol_upper):
        return "HK" if symbol_upper.startswith("0") else "TW"
    return "US"


def infer_currency(market: str, explicit_currency: str = "") -> str:
    currency = str(explicit_currency or "").strip().upper()
    if currency:
        return currency
    return {
        "US": "USD",
        "TW": "TWD",
        "HK": "HKD",
        "CRYPTO": "USD",
    }.get(market, "")


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


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


def xlsx_mapping_preview(
    path: Path,
    *,
    max_rows: int = XLSX_HEADER_SCAN_MAX_ROWS,
    max_columns: int = XLSX_HEADER_SCAN_MAX_COLUMNS,
) -> dict[str, Any]:
    """Return a bounded workbook preview suitable for an interactive column mapper."""
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(f"Invalid Excel workbook: {exc}") from exc

    preview_sheets: list[dict[str, Any]] = []
    for raw_sheet in workbook_scan.get("sheets", []):
        rows = raw_sheet.get("rows") if isinstance(raw_sheet, dict) else None
        if not isinstance(rows, list):
            continue
        bounded_rows = [
            [str(value or "") for value in row[:max_columns]]
            for row in rows[:max_rows]
            if isinstance(row, list)
        ]
        width = min(
            max((len(row) for row in bounded_rows), default=0),
            max_columns,
        )
        headers = [str(value or "") for value in raw_sheet.get("headers", [])]
        suggested_mapping: dict[str, int] = {}
        for index, canonical in enumerate(canonical_header_map(headers)):
            if canonical and canonical not in suggested_mapping:
                suggested_mapping[canonical] = index
        preview_sheets.append(
            {
                "sheet_index": raw_sheet.get("sheet_index", len(preview_sheets)),
                "sheet_name": str(raw_sheet.get("sheet_name") or f"Sheet{len(preview_sheets) + 1}"),
                "row_count": int(raw_sheet.get("row_count") or 0),
                "preview_row_count": len(bounded_rows),
                "column_count": width,
                "suggested_header_row_number": raw_sheet.get("header_row_number"),
                "suggested_data_start_row_number": raw_sheet.get("data_start_row_number"),
                "suggested_mapping": suggested_mapping,
                "header_candidates": raw_sheet.get("header_candidates", []),
                "rows": bounded_rows,
            }
        )

    horizontal_layout = xlsx_horizontal_matrix_preview_from_scan(workbook_scan)
    consolidated_layout = xlsx_consolidated_report_preview_from_scan(workbook_scan)
    selected_sheet = workbook_scan.get("selected_sheet")
    selected_sheet_name = (
        str(selected_sheet.get("sheet_name") or "")
        if isinstance(selected_sheet, dict)
        else ""
    )
    if consolidated_layout.get("recommended"):
        selected_sheet_name = str(
            consolidated_layout.get("sheet_name") or selected_sheet_name
        )
    elif horizontal_layout.get("recommended"):
        preferred_sheet = next(
            (
                sheet
                for sheet in horizontal_layout.get("sheets", [])
                if isinstance(sheet, dict) and sheet.get("selected_by_default")
            ),
            None,
        )
        if isinstance(preferred_sheet, dict):
            selected_sheet_name = str(preferred_sheet.get("sheet_name") or selected_sheet_name)
    if not selected_sheet_name and preview_sheets:
        selected_sheet_name = str(preview_sheets[0]["sheet_name"])
    return {
        "sheet_count": len(preview_sheets),
        "selected_sheet_name": selected_sheet_name,
        "sheets": preview_sheets,
        "mapping_fields": list(XLSX_MAPPING_FIELDS),
        "required_fields": sorted(XLSX_REQUIRED_MAPPING_FIELDS),
        "preview_limits": {"rows": max_rows, "columns": max_columns},
        "horizontal_layout": horizontal_layout,
        "consolidated_layout": consolidated_layout,
    }


def xlsx_consolidated_report_preview_from_scan(
    workbook_scan: dict[str, Any],
) -> dict[str, Any]:
    candidates: list[tuple[dict[str, Any], int, int]] = []
    for sheet in workbook_scan.get("sheets", []):
        if not isinstance(sheet, dict) or not isinstance(sheet.get("rows"), list):
            continue
        rows = sheet["rows"]
        fund_header_index = None
        security_header_index = None
        for index, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            name_header = xlsx_matrix_label(row[2] if len(row) > 2 else "")
            quantity_header = xlsx_matrix_label(row[13] if len(row) > 13 else "")
            price_header = xlsx_matrix_label(row[18] if len(row) > 18 else "")
            if name_header != "名稱":
                continue
            if quantity_header in {"總單位數", "單位數", "總股數"}:
                fund_header_index = index
            elif price_header in {"即時股價", "股價", "現價"}:
                security_header_index = index
        if fund_header_index is not None and security_header_index is not None:
            candidates.append((sheet, fund_header_index, security_header_index))

    if not candidates:
        return {
            "detected": False,
            "recommended": False,
            "layout": "consolidated_report",
            "holding_count": 0,
        }
    sheet, fund_header_index, security_header_index = sorted(
        candidates,
        key=lambda item: (
            str(item[0].get("sheet_name") or "").strip() == "報酬",
            len(item[0].get("rows") or []),
        ),
        reverse=True,
    )[0]
    rows = sheet["rows"]
    data_end_row_number = len(rows)
    for index in range(security_header_index + 1, len(rows)):
        next_rows = rows[index : index + 3]
        if len(next_rows) < 3:
            continue
        if all(
            not str(row[2] if isinstance(row, list) and len(row) > 2 else "").strip()
            for row in next_rows
        ):
            data_end_row_number = index
            break
    config: dict[str, Any] = {
        "layout": "consolidated_report",
        "sheet_name": str(sheet.get("sheet_name") or ""),
        "fund_header_row_number": fund_header_index + 1,
        "fund_start_row_number": fund_header_index + 2,
        "security_header_row_number": security_header_index + 1,
        "security_start_row_number": security_header_index + 2,
        "data_end_row_number": data_end_row_number,
        "tw_symbol_column_index": 1,
        "fund_name_column_index": 2,
        "tw_name_column_index": 2,
        "us_symbol_column_index": 2,
        "us_name_column_index": 1,
        "dividend_amount_column_index": 3,
        "dividend_per_unit_column_index": 4,
        "monthly_dividend_column_index": 5,
        "annual_dividend_yield_column_index": 6,
        "payback_rate_column_index": 7,
        "quantity_column_index": 13,
        "cost_amount_column_index": 14,
        "price_column_index": 18,
        "current_value_column_index": 21,
        "include_zero_quantity": True,
        "fund_market": "FUND",
        "fund_asset_type": "FUND",
        "fund_currency": "TWD",
        "fund_principal_currency": "TWD",
        "tw_market": "TW",
        "tw_asset_type": "AUTO",
        "tw_currency": "TWD",
        "tw_principal_currency": "TWD",
        "us_market": "US",
        "us_asset_type": "AUTO",
        "us_currency": "USD",
        "us_principal_currency": "TWD",
    }
    holdings, details = xlsx_holdings_from_consolidated_report(sheet, config)
    return {
        **config,
        "detected": bool(holdings),
        "recommended": bool(holdings),
        "holding_count": len(holdings),
        "fund_count": sum(holding.asset_type == "FUND" for holding in holdings),
        "tw_count": sum(holding.market == "TW" for holding in holdings),
        "us_count": sum(holding.market == "US" for holding in holdings),
        "etf_count": sum(holding.asset_type == "ETF" for holding in holdings),
        "stock_count": sum(holding.asset_type == "STOCK" for holding in holdings),
        "skipped_row_count": details["skipped_row_count"],
        "sample_holdings": [
            {
                "symbol": holding.symbol,
                "name": holding.name,
                "market": holding.market,
                "asset_type": holding.asset_type,
                "quantity": holding.quantity,
                "average_cost": holding.average_cost,
                "currency": holding.currency,
                "principal_amount": holding.principal_amount,
                "principal_currency": holding.principal_currency,
                "principal_twd": holding.principal_twd,
                "annual_dividend_yield_percent": holding.annual_dividend_yield_percent,
                "estimated_weekly_dividend_twd": holding.estimated_weekly_dividend_twd,
            }
            for holding in (
                holdings[:3]
                + [item for item in holdings if item.market == "TW"][:3]
                + [item for item in holdings if item.market == "US"][:4]
            )
        ],
    }


def xlsx_report_integer_setting(
    config: dict[str, Any],
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(config.get(field))
    except (TypeError, ValueError) as exc:
        raise InvestmentManagerError(f"Invalid {field} in consolidated Excel mapping.") from exc
    if value < minimum or value > maximum:
        raise InvestmentManagerError(
            f"{field} must be between {minimum} and {maximum}."
        )
    return value


def xlsx_report_asset_type(
    symbol: str,
    name: str,
    market: str,
    configured: str,
) -> str:
    inferred = xlsx_horizontal_asset_type(symbol, market, configured)
    if inferred == "ETF" or configured not in {"", "AUTO"}:
        return inferred
    normalized_name = normalize_header(name)
    if market == "US" and any(
        token in normalized_name
        for token in ("ETF", "期權收益", "收益策略", "收益增強", "YIELDMAX")
    ):
        return "ETF"
    return inferred


def xlsx_report_principal_currency(
    config: dict[str, Any],
    prefix: str,
    asset_currency: str,
) -> str:
    configured = str(
        config.get(f"{prefix}_principal_currency") or "TWD"
    ).strip().upper()
    if configured in {"AUTO", "ASSET"}:
        return asset_currency
    if not configured or len(configured) > 8 or not configured.replace("-", "").isalnum():
        raise InvestmentManagerError(
            f"Invalid {prefix}_principal_currency in consolidated Excel mapping."
        )
    return configured


def xlsx_holdings_from_consolidated_report(
    sheet: dict[str, Any],
    raw_config: dict[str, Any],
) -> tuple[list[Holding], dict[str, int]]:
    rows = sheet.get("rows")
    if not isinstance(rows, list) or not rows:
        raise InvestmentManagerError("Consolidated Excel worksheet has no rows.")
    config = dict(raw_config)
    row_count = len(rows)
    fund_start = xlsx_report_integer_setting(
        config, "fund_start_row_number", minimum=1, maximum=row_count
    )
    security_start = xlsx_report_integer_setting(
        config, "security_start_row_number", minimum=1, maximum=row_count
    )
    data_end = xlsx_report_integer_setting(
        config, "data_end_row_number", minimum=max(fund_start, security_start), maximum=row_count
    )
    column_fields = (
        "tw_symbol_column_index",
        "fund_name_column_index",
        "tw_name_column_index",
        "us_symbol_column_index",
        "us_name_column_index",
        "dividend_amount_column_index",
        "dividend_per_unit_column_index",
        "monthly_dividend_column_index",
        "annual_dividend_yield_column_index",
        "payback_rate_column_index",
        "quantity_column_index",
        "cost_amount_column_index",
        "price_column_index",
        "current_value_column_index",
    )
    columns = {
        field: xlsx_report_integer_setting(
            config,
            field,
            minimum=0,
            maximum=XLSX_HEADER_SCAN_MAX_COLUMNS - 1,
        )
        for field in column_fields
    }
    include_zero = bool(config.get("include_zero_quantity", True))
    holdings: list[Holding] = []
    skipped = 0
    for excel_row_number in range(fund_start, data_end + 1):
        row = rows[excel_row_number - 1]
        if not isinstance(row, list):
            skipped += 1
            continue

        def cell(field: str) -> str:
            index = columns[field]
            return str(row[index] if index < len(row) else "").strip()

        quantity = parse_float(cell("quantity_column_index")) or 0.0
        principal_amount = parse_float(cell("cost_amount_column_index"))
        dividend_amount_twd = parse_float(cell("dividend_amount_column_index"))
        dividend_per_unit = parse_float(cell("dividend_per_unit_column_index"))
        monthly_dividend_twd = parse_float(cell("monthly_dividend_column_index"))
        annual_yield = parse_float(cell("annual_dividend_yield_column_index"))
        payback_rate = parse_float(cell("payback_rate_column_index"))
        annual_yield_percent = (
            annual_yield * 100.0
            if annual_yield is not None and abs(annual_yield) <= 1
            else annual_yield
        )
        payback_rate_percent = (
            payback_rate * 100.0
            if payback_rate is not None and abs(payback_rate) <= 1
            else payback_rate
        )
        current_value_twd = parse_float(cell("current_value_column_index"))
        estimated_annual_dividend_twd = None
        if monthly_dividend_twd is not None and monthly_dividend_twd > 0:
            estimated_annual_dividend_twd = monthly_dividend_twd * 12.0
        elif (
            current_value_twd is not None
            and current_value_twd > 0
            and annual_yield_percent is not None
            and annual_yield_percent > 0
        ):
            estimated_annual_dividend_twd = (
                current_value_twd * annual_yield_percent / 100.0
            )
        estimated_weekly_dividend_twd = (
            estimated_annual_dividend_twd / 52.0
            if estimated_annual_dividend_twd is not None
            else None
        )
        if not include_zero and quantity <= 0:
            skipped += 1
            continue
        if excel_row_number < security_start:
            name = cell("fund_name_column_index")
            if not name or xlsx_matrix_label(name) in {"名稱", "投資組合", "現金"}:
                skipped += 1
                continue
            market = normalize_market(str(config.get("fund_market") or "FUND")) or "FUND"
            asset_type = str(config.get("fund_asset_type") or "FUND").strip().upper()
            currency = xlsx_fund_currency(
                name,
                str(config.get("fund_currency") or "TWD").strip().upper(),
            )
            principal_currency = xlsx_report_principal_currency(
                config, "fund", currency
            )
            symbol = f"FUND-R{excel_row_number:03d}"
        else:
            raw_tw_symbol = cell("tw_symbol_column_index")
            raw_us_symbol = cell("us_symbol_column_index")
            if xlsx_matrix_label(raw_tw_symbol) in {"現金", "名稱", "合計", "總計"}:
                skipped += 1
                continue
            if re.fullmatch(r"\d{4,6}[A-Z]{0,2}", normalize_symbol(raw_tw_symbol)):
                symbol = normalize_symbol(raw_tw_symbol)
                name = cell("tw_name_column_index")
                if not name:
                    skipped += 1
                    continue
                market = normalize_market(str(config.get("tw_market") or "TW")) or "TW"
                configured_type = str(config.get("tw_asset_type") or "AUTO").strip().upper()
                asset_type = xlsx_report_asset_type(symbol, name, market, configured_type)
                currency = str(config.get("tw_currency") or "TWD").strip().upper()
                principal_currency = xlsx_report_principal_currency(
                    config, "tw", currency
                )
            else:
                symbol = normalize_symbol(raw_us_symbol)
                if not looks_like_portfolio_symbol(symbol):
                    skipped += 1
                    continue
                name = cell("us_name_column_index")
                market = normalize_market(str(config.get("us_market") or "US")) or "US"
                configured_type = str(config.get("us_asset_type") or "AUTO").strip().upper()
                asset_type = xlsx_report_asset_type(symbol, name, market, configured_type)
                currency = str(config.get("us_currency") or "USD").strip().upper()
                principal_currency = xlsx_report_principal_currency(
                    config, "us", currency
                )
        average_cost = (
            principal_amount / quantity
            if principal_currency == currency
            and principal_amount is not None
            and principal_amount > 0
            and quantity > 0
            else None
        )
        holdings.append(
            Holding(
                symbol=symbol,
                name=name,
                market=market,
                asset_type=asset_type,
                quantity=quantity,
                average_cost=average_cost,
                currency=currency,
                principal_amount=principal_amount,
                principal_currency=principal_currency,
                principal_twd=(
                    principal_amount if principal_currency == "TWD" else None
                ),
                source_row=excel_row_number,
                dividend_amount_twd=dividend_amount_twd,
                dividend_per_unit=dividend_per_unit,
                monthly_dividend_twd=monthly_dividend_twd,
                annual_dividend_yield_percent=annual_yield_percent,
                payback_rate_percent=payback_rate_percent,
                current_value_twd=current_value_twd,
                estimated_annual_dividend_twd=estimated_annual_dividend_twd,
                estimated_weekly_dividend_twd=estimated_weekly_dividend_twd,
            )
        )
    return holdings, {"skipped_row_count": skipped}


def load_xlsx_portfolio_consolidated_report(
    path: Path,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[list[Holding], dict[str, Any]]:
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(f"Invalid Excel workbook: {exc}") from exc
    detected = xlsx_consolidated_report_preview_from_scan(workbook_scan)
    if not detected.get("detected"):
        raise InvestmentManagerError("No consolidated return worksheet was detected.")
    merged_config = {
        key: value
        for key, value in detected.items()
        if key
        not in {
            "detected",
            "recommended",
            "holding_count",
            "fund_count",
            "tw_count",
            "us_count",
            "etf_count",
            "stock_count",
            "skipped_row_count",
            "sample_holdings",
        }
    }
    if isinstance(config, dict):
        merged_config.update(config)
    sheet_name = str(merged_config.get("sheet_name") or "")
    sheet = next(
        (
            item
            for item in workbook_scan.get("sheets", [])
            if isinstance(item, dict)
            and str(item.get("sheet_name") or "").strip() == sheet_name.strip()
        ),
        None,
    )
    if sheet is None:
        raise InvestmentManagerError(f"Consolidated worksheet not found: {sheet_name}")
    merged_config["sheet_name"] = str(sheet.get("sheet_name") or sheet_name)
    sheet_name = merged_config["sheet_name"]
    holdings, parse_details = xlsx_holdings_from_consolidated_report(sheet, merged_config)
    if not holdings:
        raise InvestmentManagerError(
            "Consolidated Excel settings produced no holdings. Check rows and columns."
        )
    selected_sheet = {
        **public_xlsx_sheet_scan(sheet),
        "usable": True,
        "score": 300,
        "header_mode": "consolidated_report",
        "header_depth": 1,
        "header_row_index": int(merged_config["security_header_row_number"]) - 1,
        "header_row_number": merged_config["security_header_row_number"],
        "data_start_row_index": int(merged_config["fund_start_row_number"]) - 1,
        "data_start_row_number": merged_config["fund_start_row_number"],
        "valid_data_row_count": len(holdings),
        "consolidated_mapping": merged_config,
    }
    public_sheets = [
        selected_sheet
        if str(item.get("sheet_name") or "") == sheet_name
        else public_xlsx_sheet_scan(item)
        for item in workbook_scan.get("sheets", [])
        if isinstance(item, dict)
    ]
    profile = {"layout": "consolidated_report", **merged_config}
    return holdings, {
        "profile": profile,
        "workbook_scan": {
            "sheet_count": len(public_sheets),
            "selected_sheet": selected_sheet,
            "sheets": public_sheets,
        },
        "imported_row_count": len(holdings),
        "skipped_row_count": parse_details["skipped_row_count"],
    }


def xlsx_horizontal_matrix_preview_from_scan(
    workbook_scan: dict[str, Any],
) -> dict[str, Any]:
    detected_sheets: list[dict[str, Any]] = []
    for sheet in workbook_scan.get("sheets", []):
        if not isinstance(sheet, dict):
            continue
        detected = detect_xlsx_horizontal_matrix_sheet(sheet)
        if detected is not None:
            detected_sheets.append(detected)

    preferred_names = {"股票", "美股", "平台基金", "銀行基金"}
    preferred_detected = {
        str(sheet.get("sheet_name") or "").strip()
        for sheet in detected_sheets
        if str(sheet.get("sheet_name") or "").strip() in preferred_names
    }
    if preferred_detected:
        for sheet in detected_sheets:
            sheet["selected_by_default"] = (
                str(sheet.get("sheet_name") or "").strip() in preferred_detected
            )
    else:
        for sheet in detected_sheets:
            sheet["selected_by_default"] = not xlsx_horizontal_sheet_is_summary(
                str(sheet.get("sheet_name") or "")
            )

    selected_sheets = [
        sheet for sheet in detected_sheets if sheet.get("selected_by_default")
    ]
    if not selected_sheets and detected_sheets:
        detected_sheets[0]["selected_by_default"] = True
        selected_sheets = [detected_sheets[0]]
    return {
        "detected": bool(detected_sheets),
        "recommended": bool(selected_sheets),
        "layout": "horizontal_matrix",
        "sheet_count": len(detected_sheets),
        "selected_sheet_count": len(selected_sheets),
        "holding_count": sum(
            int(sheet.get("holding_count") or 0) for sheet in selected_sheets
        ),
        "supported_asset_classes": ["ETF", "台股", "美股", "共同基金"],
        "sheets": detected_sheets,
    }


def xlsx_horizontal_sheet_is_summary(sheet_name: str) -> bool:
    normalized = normalize_header(sheet_name)
    return any(
        token in normalized
        for token in ("總表", "報酬", "績效", "分析", "試算", "說明", "歷史")
    )


def xlsx_horizontal_sheet_defaults(sheet_name: str) -> dict[str, str]:
    normalized = normalize_header(sheet_name)
    if "基金" in normalized or any(token in normalized for token in ("鉅亨", "中租")):
        return {
            "category_label": "共同基金",
            "market": "FUND",
            "asset_type": "FUND",
            "currency": "TWD",
        }
    if "美股" in normalized or "美元" in normalized:
        return {
            "category_label": "美股 / ETF",
            "market": "US",
            "asset_type": "AUTO",
            "currency": "USD",
        }
    return {
        "category_label": "台股 / ETF",
        "market": "TW",
        "asset_type": "AUTO",
        "currency": "TWD",
    }


def xlsx_matrix_label(value: Any) -> str:
    return normalize_header(str(value or ""))


def xlsx_matrix_group_starts(row: list[Any], labels: set[str]) -> list[int]:
    return [
        index
        for index, value in enumerate(row)
        if xlsx_matrix_label(value) in labels
    ]


def xlsx_matrix_numeric_value(
    row: list[Any],
    start_column_index: int,
    group_width: int,
) -> float | None:
    end = min(len(row), start_column_index + max(2, group_width))
    for value in row[start_column_index + 1 : end]:
        parsed = parse_float(value)
        if parsed is not None:
            return parsed
    return None


def xlsx_matrix_header_row(
    rows: list[list[Any]],
    holding_row_index: int,
    group_starts: list[int],
) -> int | None:
    search_start = max(0, holding_row_index - 32)
    for index in range(holding_row_index - 1, search_start - 1, -1):
        row = rows[index]
        first_label = xlsx_matrix_label(row[0] if row else "")
        if first_label in {"月份", "年月", "月分"}:
            return index
    minimum_labels = max(1, min(3, len(group_starts)))
    for index in range(holding_row_index - 1, search_start - 1, -1):
        row = rows[index]
        labels = [
            str(row[column] or "").strip()
            for column in group_starts
            if column < len(row) and str(row[column] or "").strip()
        ]
        if len(labels) >= minimum_labels:
            return index
    return None


def xlsx_matrix_related_row(
    rows: list[list[Any]],
    holding_row_index: int,
    group_starts: list[int],
    labels: set[str],
) -> int | None:
    for index in range(holding_row_index + 1, min(len(rows), holding_row_index + 7)):
        row = rows[index]
        if any(
            column < len(row) and xlsx_matrix_label(row[column]) in labels
            for column in group_starts
        ):
            return index
    return None


def detect_xlsx_horizontal_matrix_sheet(
    sheet: dict[str, Any],
) -> dict[str, Any] | None:
    rows = sheet.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    for holding_row_index in range(len(rows) - 1, -1, -1):
        row = rows[holding_row_index]
        if not isinstance(row, list):
            continue
        group_starts = xlsx_matrix_group_starts(row, {"持有", "持倉"})
        if not group_starts:
            continue
        quantity_row_index = xlsx_matrix_related_row(
            rows,
            holding_row_index,
            group_starts,
            {"股數", "單位數", "持有股數", "持有單位"},
        )
        if quantity_row_index is None:
            continue
        header_row_index = xlsx_matrix_header_row(
            rows,
            holding_row_index,
            group_starts,
        )
        if header_row_index is None:
            continue
        price_row_index = xlsx_matrix_related_row(
            rows,
            holding_row_index,
            group_starts,
            {"股價", "淨值", "價格", "現價"},
        )
        defaults = xlsx_horizontal_sheet_defaults(str(sheet.get("sheet_name") or ""))
        config = {
            "sheet_index": int(sheet.get("sheet_index") or 0),
            "sheet_name": str(sheet.get("sheet_name") or ""),
            "header_row_number": header_row_index + 1,
            "holding_row_number": holding_row_index + 1,
            "price_row_number": price_row_index + 1 if price_row_index is not None else None,
            "quantity_row_number": quantity_row_index + 1,
            "first_asset_column_index": group_starts[0],
            "first_asset_column_letter": xlsx_column_letter(group_starts[0]),
            "group_width": XLSX_HORIZONTAL_GROUP_WIDTH,
            **defaults,
        }
        holdings, skipped = xlsx_holdings_from_horizontal_sheet(sheet, config)
        if not holdings:
            continue
        return {
            **config,
            "detected": True,
            "selected_by_default": False,
            "holding_count": len(holdings),
            "skipped_group_count": skipped,
            "sample_holdings": [
                {
                    "symbol": holding.symbol,
                    "name": holding.name,
                    "market": holding.market,
                    "asset_type": holding.asset_type,
                    "quantity": holding.quantity,
                    "average_cost": holding.average_cost,
                    "currency": holding.currency,
                }
                for holding in holdings[:8]
            ],
        }
    return None


def normalize_xlsx_horizontal_sheet_config(
    raw_config: dict[str, Any],
    detected: dict[str, Any],
) -> dict[str, Any]:
    config = {**detected, **raw_config}
    sheet_name = str(config.get("sheet_name") or "").strip()
    if not sheet_name:
        raise InvestmentManagerError("Horizontal Excel mapping requires a worksheet name.")

    def positive_row(field: str, allow_blank: bool = False) -> int | None:
        raw_value = config.get(field)
        if allow_blank and (raw_value is None or raw_value == ""):
            return None
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise InvestmentManagerError(f"Invalid {field} for {sheet_name}.") from exc
        if value < 1:
            raise InvestmentManagerError(f"{field} must be a positive integer for {sheet_name}.")
        return value

    header_row_number = positive_row("header_row_number")
    holding_row_number = positive_row("holding_row_number")
    price_row_number = positive_row("price_row_number", allow_blank=True)
    quantity_row_number = positive_row("quantity_row_number")
    try:
        first_asset_column_index = int(config.get("first_asset_column_index"))
        group_width = int(config.get("group_width") or XLSX_HORIZONTAL_GROUP_WIDTH)
    except (TypeError, ValueError) as exc:
        raise InvestmentManagerError(f"Invalid horizontal column settings for {sheet_name}.") from exc
    if first_asset_column_index < 0 or first_asset_column_index >= XLSX_HEADER_SCAN_MAX_COLUMNS:
        raise InvestmentManagerError(f"Invalid first asset column for {sheet_name}.")
    if group_width < 2 or group_width > 12:
        raise InvestmentManagerError(f"Group width must be between 2 and 12 for {sheet_name}.")
    market = normalize_market(str(config.get("market") or detected.get("market") or ""))
    asset_type = str(config.get("asset_type") or detected.get("asset_type") or "AUTO").strip().upper()
    currency = str(config.get("currency") or detected.get("currency") or "").strip().upper()
    return {
        "sheet_name": sheet_name,
        "header_row_number": header_row_number,
        "holding_row_number": holding_row_number,
        "price_row_number": price_row_number,
        "quantity_row_number": quantity_row_number,
        "first_asset_column_index": first_asset_column_index,
        "group_width": group_width,
        "market": market,
        "asset_type": asset_type,
        "currency": currency,
        "category_label": str(config.get("category_label") or detected.get("category_label") or ""),
    }


def xlsx_fund_currency(name: str, default_currency: str) -> str:
    normalized = normalize_header(name).upper()
    for tokens, currency in (
        (("美元", "美金", "USD"), "USD"),
        (("台幣", "新台幣", "TWD"), "TWD"),
        (("歐元", "EUR"), "EUR"),
        (("日圓", "日幣", "JPY"), "JPY"),
        (("澳幣", "AUD"), "AUD"),
        (("人民幣", "CNY"), "CNY"),
    ):
        if any(token.upper() in normalized for token in tokens):
            return currency
    return default_currency


def xlsx_horizontal_asset_type(symbol: str, market: str, configured: str) -> str:
    if configured and configured != "AUTO":
        return configured
    if market == "FUND":
        return "FUND"
    if market == "TW" and re.fullmatch(r"00\d{2,4}", symbol):
        return "ETF"
    if market == "US" and symbol in COMMON_US_ETF_SYMBOLS:
        return "ETF"
    return "STOCK"


def xlsx_holdings_from_horizontal_sheet(
    sheet: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[Holding], int]:
    rows = sheet.get("rows")
    if not isinstance(rows, list):
        return [], 0
    header_index = int(config.get("header_row_number") or 0) - 1
    holding_index = int(config.get("holding_row_number") or 0) - 1
    quantity_index = int(config.get("quantity_row_number") or 0) - 1
    price_number = config.get("price_row_number")
    price_index = int(price_number) - 1 if price_number not in (None, "") else None
    required_indexes = [header_index, holding_index, quantity_index]
    if any(index < 0 or index >= len(rows) for index in required_indexes):
        raise InvestmentManagerError(
            f"Horizontal Excel row settings are outside worksheet {config.get('sheet_name')}."
        )
    if price_index is not None and (price_index < 0 or price_index >= len(rows)):
        raise InvestmentManagerError(
            f"Price row is outside worksheet {config.get('sheet_name')}."
        )
    header_row = rows[header_index]
    holding_row = rows[holding_index]
    quantity_row = rows[quantity_index]
    price_row = rows[price_index] if price_index is not None else []
    first_column = int(config.get("first_asset_column_index") or 0)
    group_width = int(config.get("group_width") or XLSX_HORIZONTAL_GROUP_WIDTH)
    market = normalize_market(str(config.get("market") or "")) or "TW"
    configured_type = str(config.get("asset_type") or "AUTO").strip().upper()
    default_currency = str(config.get("currency") or infer_currency(market)).strip().upper()
    sheet_index = int(sheet.get("sheet_index") or 0)
    holdings: list[Holding] = []
    skipped = 0
    for start in range(first_column, len(header_row), group_width):
        raw_label = str(header_row[start] if start < len(header_row) else "").strip()
        if not raw_label or xlsx_matrix_label(raw_label) in {"合計", "總計", "小計"}:
            skipped += 1
            continue
        quantity = xlsx_matrix_numeric_value(quantity_row, start, group_width)
        if quantity is None or quantity <= 0:
            skipped += 1
            continue
        holding_amount = xlsx_matrix_numeric_value(holding_row, start, group_width)
        current_price = xlsx_matrix_numeric_value(price_row, start, group_width)
        if market == "FUND" or configured_type == "FUND":
            symbol = f"FUND-{sheet_index + 1:02d}-{xlsx_column_letter(start)}"
            name = raw_label
        else:
            symbol = normalize_symbol(raw_label)
            name = ""
            if not looks_like_portfolio_symbol(symbol):
                skipped += 1
                continue
        asset_type = xlsx_horizontal_asset_type(symbol, market, configured_type)
        currency = (
            xlsx_fund_currency(raw_label, default_currency)
            if asset_type == "FUND"
            else default_currency or infer_currency(market)
        )
        average_cost: float | None = None
        if holding_amount is not None and holding_amount > 0 and quantity > 0:
            if market == "TW" and currency == "TWD":
                average_cost = holding_amount / quantity
            elif asset_type == "FUND" and currency == "TWD":
                average_cost = holding_amount / quantity
        if average_cost is None and current_price is not None and current_price > 0:
            # Keep the latest workbook value available for local-only funds without
            # pretending it is a broker-confirmed historical cost basis.
            average_cost = current_price if asset_type == "FUND" else None
        holdings.append(
            Holding(
                symbol=symbol,
                name=name,
                market=market,
                asset_type=asset_type,
                quantity=quantity,
                average_cost=average_cost,
                currency=currency,
                principal_amount=holding_amount,
                principal_currency=currency,
                principal_twd=(holding_amount if currency == "TWD" else None),
                source_row=quantity_index + 1,
            )
        )
    return holdings, skipped


def merge_xlsx_horizontal_holdings(holdings: list[Holding]) -> list[Holding]:
    merged: dict[tuple[str, str], Holding] = {}
    for holding in holdings:
        key = (holding.market, holding.symbol)
        previous = merged.get(key)
        if previous is None:
            merged[key] = holding
            continue
        quantity = previous.quantity + holding.quantity
        average_cost = None
        if (
            previous.average_cost is not None
            and holding.average_cost is not None
            and quantity > 0
        ):
            average_cost = (
                previous.average_cost * previous.quantity
                + holding.average_cost * holding.quantity
            ) / quantity
        merged[key] = Holding(
            symbol=holding.symbol,
            name=previous.name or holding.name,
            market=holding.market,
            asset_type=(
                "ETF"
                if "ETF" in {previous.asset_type, holding.asset_type}
                else previous.asset_type or holding.asset_type
            ),
            quantity=quantity,
            average_cost=average_cost,
            currency=previous.currency or holding.currency,
            principal_amount=(
                (previous.principal_amount or 0) + (holding.principal_amount or 0)
                if previous.principal_amount is not None
                or holding.principal_amount is not None
                else None
            ),
            principal_currency=previous.principal_currency or holding.principal_currency,
            principal_twd=(
                (previous.principal_twd or 0) + (holding.principal_twd or 0)
                if previous.principal_twd is not None or holding.principal_twd is not None
                else None
            ),
            source_row=previous.source_row,
        )
    return list(merged.values())


def load_xlsx_portfolio_horizontal_matrix(
    path: Path,
    *,
    sheet_configs: list[dict[str, Any]] | None = None,
) -> tuple[list[Holding], dict[str, Any]]:
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(f"Invalid Excel workbook: {exc}") from exc
    preview = xlsx_horizontal_matrix_preview_from_scan(workbook_scan)
    detected_by_name = {
        str(sheet.get("sheet_name") or ""): sheet
        for sheet in preview.get("sheets", [])
        if isinstance(sheet, dict)
    }
    if not detected_by_name:
        raise InvestmentManagerError("No horizontal holding worksheets were detected.")
    raw_configs = sheet_configs if isinstance(sheet_configs, list) else []
    if raw_configs:
        selected_configs = [
            config
            for config in raw_configs
            if isinstance(config, dict) and config.get("enabled", True)
        ]
    else:
        selected_configs = [
            sheet
            for sheet in preview.get("sheets", [])
            if isinstance(sheet, dict) and sheet.get("selected_by_default")
        ]
    if not selected_configs:
        raise InvestmentManagerError("Select at least one horizontal holding worksheet.")

    workbook_sheets = {
        str(sheet.get("sheet_name") or ""): sheet
        for sheet in workbook_scan.get("sheets", [])
        if isinstance(sheet, dict)
    }
    normalized_configs: list[dict[str, Any]] = []
    all_holdings: list[Holding] = []
    skipped_count = 0
    selected_scan_sheets: list[dict[str, Any]] = []
    for raw_config in selected_configs:
        sheet_name = str(raw_config.get("sheet_name") or "").strip()
        detected = detected_by_name.get(sheet_name)
        sheet = workbook_sheets.get(sheet_name)
        if detected is None or sheet is None:
            raise InvestmentManagerError(f"Horizontal worksheet not found: {sheet_name}")
        config = normalize_xlsx_horizontal_sheet_config(raw_config, detected)
        sheet_holdings, skipped = xlsx_holdings_from_horizontal_sheet(sheet, config)
        if not sheet_holdings:
            raise InvestmentManagerError(
                f"Horizontal settings produced no holdings for worksheet {sheet_name}."
            )
        normalized_configs.append(config)
        all_holdings.extend(sheet_holdings)
        skipped_count += skipped
        selected_scan_sheets.append(
            {
                **public_xlsx_sheet_scan(sheet),
                "usable": True,
                "score": 260,
                "header_mode": "horizontal_matrix",
                "header_depth": 1,
                "header_row_index": int(config["header_row_number"]) - 1,
                "header_row_number": config["header_row_number"],
                "data_start_row_index": int(config["quantity_row_number"]) - 1,
                "data_start_row_number": config["quantity_row_number"],
                "valid_data_row_count": len(sheet_holdings),
                "horizontal_mapping": config,
            }
        )
    holdings = merge_xlsx_horizontal_holdings(all_holdings)
    selected_name = "、".join(config["sheet_name"] for config in normalized_configs)
    selected_summary = {
        "sheet_name": selected_name,
        "usable": True,
        "score": 260,
        "header_mode": "horizontal_matrix",
        "header_depth": 1,
        "header_row_number": normalized_configs[0]["header_row_number"],
        "data_start_row_number": normalized_configs[0]["quantity_row_number"],
        "valid_data_row_count": len(holdings),
        "source_sheet_count": len(normalized_configs),
    }
    public_sheets = []
    selected_by_name = {
        str(sheet.get("sheet_name") or ""): sheet for sheet in selected_scan_sheets
    }
    for sheet in workbook_scan.get("sheets", []):
        if not isinstance(sheet, dict):
            continue
        sheet_name = str(sheet.get("sheet_name") or "")
        public_sheets.append(selected_by_name.get(sheet_name, public_xlsx_sheet_scan(sheet)))
    profile = {
        "layout": "horizontal_matrix",
        "sheets": normalized_configs,
    }
    return holdings, {
        "profile": profile,
        "workbook_scan": {
            "sheet_count": len(public_sheets),
            "selected_sheet": selected_summary,
            "sheets": public_sheets,
        },
        "imported_row_count": len(holdings),
        "source_position_count": len(all_holdings),
        "merged_position_count": max(0, len(all_holdings) - len(holdings)),
        "skipped_row_count": skipped_count,
    }


def normalize_xlsx_column_mapping(column_mapping: Any) -> dict[str, int]:
    if not isinstance(column_mapping, dict):
        raise InvestmentManagerError("Excel column mapping must be an object.")
    normalized: dict[str, int] = {}
    for raw_field, raw_index in column_mapping.items():
        field = str(raw_field or "").strip()
        if field not in XLSX_MAPPING_FIELDS or raw_index is None or raw_index == "":
            continue
        if isinstance(raw_index, bool):
            raise InvestmentManagerError(f"Invalid column index for {field}.")
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise InvestmentManagerError(f"Invalid column index for {field}.") from exc
        if index < 0 or index >= XLSX_HEADER_SCAN_MAX_COLUMNS:
            raise InvestmentManagerError(
                f"Column index for {field} must be between 0 and {XLSX_HEADER_SCAN_MAX_COLUMNS - 1}."
            )
        normalized[field] = index
    missing = sorted(XLSX_REQUIRED_MAPPING_FIELDS - set(normalized))
    if missing:
        raise InvestmentManagerError(
            "Excel column mapping is missing required fields: " + ", ".join(missing)
        )
    return normalized


def xlsx_column_letter(index: int) -> str:
    if index < 0:
        return ""
    letters = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def load_xlsx_portfolio_with_mapping(
    path: Path,
    *,
    sheet_name: str,
    header_row_number: int,
    data_start_row_number: int | None = None,
    column_mapping: dict[str, Any],
) -> tuple[list[Holding], dict[str, Any]]:
    """Load holdings from an explicitly selected sheet, header row, and column map."""
    mapping = normalize_xlsx_column_mapping(column_mapping)
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(f"Invalid Excel workbook: {exc}") from exc

    selected = next(
        (
            sheet
            for sheet in workbook_scan.get("sheets", [])
            if isinstance(sheet, dict) and str(sheet.get("sheet_name") or "") == sheet_name
        ),
        None,
    )
    if selected is None:
        raise InvestmentManagerError(f"Excel worksheet not found: {sheet_name}")
    rows = selected.get("rows")
    if not isinstance(rows, list) or not rows:
        raise InvestmentManagerError(f"Excel worksheet has no rows: {sheet_name}")
    try:
        header_number = int(header_row_number)
    except (TypeError, ValueError) as exc:
        raise InvestmentManagerError("Excel header row must be a positive integer.") from exc
    if header_number < 1 or header_number > len(rows):
        raise InvestmentManagerError(
            f"Excel header row must be between 1 and {len(rows)} for {sheet_name}."
        )
    try:
        data_start_number = int(
            header_number + 1
            if data_start_row_number is None
            else data_start_row_number
        )
    except (TypeError, ValueError) as exc:
        raise InvestmentManagerError("Excel data start row must be a positive integer.") from exc
    if data_start_number < 1 or data_start_number > len(rows):
        raise InvestmentManagerError(
            f"Excel data start row must be between 1 and {len(rows)} for {sheet_name}."
        )

    header_index = header_number - 1
    header_values = [str(value or "").strip() for value in rows[header_index]]
    holdings: list[Holding] = []
    non_empty_data_rows = 0
    for excel_row_number, values in enumerate(
        rows[data_start_number - 1 :],
        start=data_start_number,
    ):
        if not isinstance(values, list) or not any(str(value or "").strip() for value in values):
            continue
        non_empty_data_rows += 1
        mapped = {
            field: values[index] if index < len(values) else ""
            for field, index in mapping.items()
        }
        symbol = normalize_symbol(str(mapped.get("symbol") or ""))
        if not looks_like_portfolio_symbol(symbol):
            continue
        market = infer_market(
            symbol,
            str(mapped.get("market") or ""),
            str(mapped.get("asset_type") or ""),
        )
        holdings.append(
            Holding(
                symbol=symbol,
                name=str(mapped.get("name") or "").strip(),
                market=market,
                asset_type=str(mapped.get("asset_type") or "").strip(),
                quantity=parse_float(mapped.get("quantity")) or 0.0,
                average_cost=parse_float(mapped.get("average_cost")),
                currency=infer_currency(market, str(mapped.get("currency") or "")),
                principal_amount=parse_float(mapped.get("principal_amount")),
                principal_currency=str(
                    mapped.get("principal_currency")
                    or mapped.get("currency")
                    or infer_currency(market)
                ).strip().upper(),
                principal_twd=parse_float(mapped.get("principal_twd")),
                source_row=excel_row_number,
            )
        )
    if not holdings:
        raise InvestmentManagerError(
            "The selected Excel mapping produced no usable holdings. Check the header row, symbol, and quantity columns."
        )

    mapped_columns = {
        field: {
            "column_index": index,
            "column_letter": xlsx_column_letter(index),
            "header": header_values[index] if index < len(header_values) else "",
        }
        for field, index in mapping.items()
    }
    manual_sheet = {
        **public_xlsx_sheet_scan(selected),
        "usable": True,
        "score": 200 + len(mapping) * 10,
        "header_mode": "manual_mapping",
        "header_depth": 1,
        "header_row_index": header_index,
        "header_row_number": header_number,
        "header_start_row_index": header_index,
        "header_start_row_number": header_number,
        "data_start_row_index": data_start_number - 1,
        "data_start_row_number": data_start_number,
        "headers": header_values,
        "canonical_columns": list(mapping),
        "data_row_count": non_empty_data_rows,
        "valid_data_row_count": len(holdings),
        "manual_mapping": mapped_columns,
    }
    public_sheets = []
    for sheet in workbook_scan.get("sheets", []):
        public_sheet = public_xlsx_sheet_scan(sheet)
        if str(public_sheet.get("sheet_name") or "") == sheet_name:
            public_sheet = manual_sheet
        public_sheets.append(public_sheet)
    public_scan = {
        "sheet_count": len(public_sheets),
        "selected_sheet": manual_sheet,
        "sheets": public_sheets,
    }
    profile = {
        "sheet_name": sheet_name,
        "header_row_number": header_number,
        "data_start_row_number": data_start_number,
        "column_mapping": mapping,
        "mapped_columns": mapped_columns,
    }
    return holdings, {
        "profile": profile,
        "workbook_scan": public_scan,
        "imported_row_count": len(holdings),
        "skipped_row_count": max(0, non_empty_data_rows - len(holdings)),
    }


def xlsx_records_from_rows(
    rows: list[list[str]],
    header_index: int,
    headers: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for values in rows[header_index + 1:]:
        if not any(str(value or "").strip() for value in values):
            continue
        if xlsx_row_looks_like_header(values):
            continue
        row: dict[str, Any] = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            row[header] = values[index] if index < len(values) else ""
        records.append(row)
    return records


def xlsx_row_looks_like_header(values: Iterable[Any]) -> bool:
    cells = [str(value or "").strip() for value in values]
    if portfolio_header_score(cells) > 0:
        return True
    canonical_hit_count = sum(1 for value in cells if canonical_column(value))
    if canonical_hit_count >= 2:
        return True
    header_tokens = header_token_lookup()
    token_hits = sum(
        1
        for value in cells
        if normalize_header(value) in header_tokens
    )
    return token_hits >= 2


def looks_like_portfolio_symbol(value: Any) -> bool:
    text = normalize_symbol(str(value or ""))
    if not text:
        return False
    if normalize_header(text) in {
        "total",
        "subtotal",
        "updated",
        "updatedat",
        "date",
        "account",
        "cash",
        "us",
        "tw",
        "hk",
        "usd",
        "twd",
        "hkd",
        "etf",
        "\u5408\u8a08",
        "\u5c0f\u8a08",
        "\u7e3d\u8a08",
        "\u5e33\u6236",
        "\u66f4\u65b0\u6642\u9593",
    }:
        return False
    if re.fullmatch(r"\d{1,3}", text):
        return False
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:E[+-]?\d+)?", text):
        return False
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    return bool(re.fullmatch(r"\^?[A-Z0-9][A-Z0-9./\-]{0,19}", text))


def looks_like_market_value(value: Any) -> bool:
    normalized = normalize_header(str(value or ""))
    return bool(normalized and normalized in market_alias_lookup())


def looks_like_currency_value(value: Any) -> bool:
    normalized = normalize_header(str(value or "")).upper()
    return normalized in {"USD", "TWD", "HKD", "JPY", "EUR", "CNY", "CNH", "GBP"}


def xlsx_row_looks_like_portfolio_data(values: Iterable[Any]) -> bool:
    cells = [str(value or "").strip() for value in values]
    if not any(cells) or xlsx_row_looks_like_header(cells):
        return False
    symbol_hits = sum(1 for value in cells if looks_like_portfolio_symbol(value))
    if symbol_hits <= 0:
        return False
    numeric_hits = sum(1 for value in cells if parse_float(value) is not None)
    market_hits = sum(1 for value in cells if looks_like_market_value(value))
    currency_hits = sum(1 for value in cells if looks_like_currency_value(value))
    return numeric_hits + market_hits + currency_hits > 0


def infer_xlsx_headers_from_data(
    rows: list[list[str]],
    header_index: int,
    source_headers: list[str],
    max_rows: int = 80,
) -> dict[str, Any] | None:
    width = max(
        [len(source_headers)]
        + [len(row) for row in rows[header_index + 1 : header_index + 1 + max_rows]]
    )
    stats = [
        {
            "symbol": 0,
            "numeric": 0,
            "market": 0,
            "currency": 0,
            "text": 0,
            "non_empty": 0,
        }
        for _ in range(width)
    ]

    for row in rows[header_index + 1 : header_index + 1 + max_rows]:
        values = [str(value or "").strip() for value in row]
        if not any(values):
            continue
        if xlsx_row_looks_like_header(values):
            continue
        for column_index in range(width):
            value = values[column_index] if column_index < len(values) else ""
            if not value:
                continue
            stats[column_index]["non_empty"] += 1
            if looks_like_market_value(value):
                stats[column_index]["market"] += 1
            elif looks_like_currency_value(value):
                stats[column_index]["currency"] += 1
            else:
                if parse_float(value) is not None:
                    stats[column_index]["numeric"] += 1
                if looks_like_portfolio_symbol(value):
                    stats[column_index]["symbol"] += 1
                if parse_float(value) is None and not looks_like_portfolio_symbol(value):
                    stats[column_index]["text"] += 1

    symbol_index = max(range(width), key=lambda index: stats[index]["symbol"], default=0)
    if not stats or stats[symbol_index]["symbol"] <= 0:
        return None

    canonical = canonical_header_map(source_headers + [""] * max(0, width - len(source_headers)))
    while len(canonical) < width:
        canonical.append("")
    canonical[symbol_index] = canonical[symbol_index] or "symbol"

    for column_index, column_stats in enumerate(stats):
        if canonical[column_index]:
            continue
        if column_stats["market"] > 0 and column_stats["market"] >= column_stats["symbol"]:
            canonical[column_index] = "market"
        elif column_stats["currency"] > 0:
            canonical[column_index] = "currency"

    numeric_columns = [
        index
        for index, column_stats in enumerate(stats)
        if index != symbol_index
        and not canonical[index]
        and column_stats["numeric"] > 0
    ]
    for column_index in numeric_columns:
        normalized_header = normalize_header(
            source_headers[column_index] if column_index < len(source_headers) else ""
        )
        if any(token in normalized_header for token in ("qty", "share", "unit", "amount", "\u6578", "\u80a1", "\u4efd", "\u5eab\u5b58")):
            canonical[column_index] = "quantity"
        elif any(token in normalized_header for token in ("cost", "price", "avg", "\u6210\u672c", "\u50f9", "\u5747", "\u5165\u5834")):
            canonical[column_index] = "average_cost"

    remaining_numeric = [index for index in numeric_columns if not canonical[index]]
    if "quantity" not in canonical and remaining_numeric:
        canonical[remaining_numeric.pop(0)] = "quantity"
    if "average_cost" not in canonical and remaining_numeric:
        canonical[remaining_numeric.pop(0)] = "average_cost"

    if "name" not in canonical:
        text_candidates = [
            index
            for index, column_stats in enumerate(stats)
            if index != symbol_index
            and not canonical[index]
            and column_stats["text"] > 0
        ]
        if text_candidates:
            canonical[min(text_candidates, key=lambda index: abs(index - symbol_index))] = "name"

    inferred_headers = [
        canonical[index] if canonical[index] else (source_headers[index] if index < len(source_headers) else "")
        for index in range(width)
    ]
    return {
        "headers": inferred_headers,
        "canonical_columns": canonical_columns(inferred_headers),
        "symbol_column_index": symbol_index,
        "inferred_column_count": sum(1 for item in canonical if item),
    }


def xlsx_data_profile(
    rows: list[list[str]],
    header_index: int,
    headers: list[str],
    max_rows: int = 80,
) -> dict[str, int]:
    mapped = canonical_header_map(headers)
    symbol_index = next(
        (index for index, canonical in enumerate(mapped) if canonical == "symbol"),
        None,
    )
    profile = {
        "non_empty_row_count": 0,
        "valid_data_row_count": 0,
        "symbol_row_count": 0,
        "quantity_row_count": 0,
        "average_cost_row_count": 0,
        "market_row_count": 0,
        "supporting_field_row_count": 0,
    }
    if symbol_index is None:
        return profile

    for row in rows[header_index + 1 : header_index + 1 + max_rows]:
        values = [str(value or "").strip() for value in row]
        if not any(values):
            continue
        if xlsx_row_looks_like_header(values):
            continue
        profile["non_empty_row_count"] += 1
        symbol = values[symbol_index] if symbol_index < len(values) else ""
        if not looks_like_portfolio_symbol(symbol):
            continue
        profile["symbol_row_count"] += 1

        has_supporting_field = False
        for column_index, canonical in enumerate(mapped):
            value = values[column_index] if column_index < len(values) else ""
            if not canonical or canonical == "symbol" or not value:
                continue
            has_supporting_field = True
            if canonical == "quantity" and parse_float(value) is not None:
                profile["quantity_row_count"] += 1
            elif canonical == "average_cost" and parse_float(value) is not None:
                profile["average_cost_row_count"] += 1
            elif canonical == "market" and normalize_market(value):
                profile["market_row_count"] += 1
        if has_supporting_field:
            profile["supporting_field_row_count"] += 1
        profile["valid_data_row_count"] += 1
    return profile


def xlsx_sheet_name_score(sheet_name: str) -> int:
    normalized = normalize_header(sheet_name)
    positive = (
        "portfolio",
        "holdings",
        "positions",
        "stock",
        "\u6301\u80a1",
        "\u5eab\u5b58",
        "\u6295\u8cc7",
        "\u90e8\u4f4d",
    )
    negative = (
        "summary",
        "overview",
        "history",
        "transaction",
        "trade",
        "\u6458\u8981",
        "\u7e3d\u89bd",
        "\u640d\u76ca",
        "\u4ea4\u6613",
        "\u6b77\u53f2",
        "\u660e\u7d30",
    )
    score = 0
    if any(token in normalized for token in positive):
        score += 30
    if any(token in normalized for token in negative):
        score -= 30
    return score


def score_xlsx_header_candidate(
    rows: list[list[str]],
    index: int,
    headers: list[str],
    sheet_name: str = "",
    header_start_index: int | None = None,
    header_depth: int = 1,
) -> dict[str, Any] | None:
    source_headers = headers
    header_mode = "headerless_inferred" if header_depth == 0 else "explicit"
    inferred = infer_xlsx_headers_from_data(rows, index, source_headers)
    column_score = portfolio_header_score(source_headers)
    if column_score <= 0:
        if inferred is None:
            return None
        headers = [str(header) for header in inferred["headers"]]
        column_score = 70 + int(inferred.get("inferred_column_count", 0)) * 12
        header_mode = "headerless_inferred" if header_depth == 0 else "inferred"
    elif inferred is not None:
        inferred_headers = [str(header) for header in inferred["headers"]]
        merged_headers: list[str] = []
        for column_index, header in enumerate(source_headers):
            if canonical_column(header):
                merged_headers.append(header)
            elif column_index < len(inferred_headers) and canonical_column(inferred_headers[column_index]):
                merged_headers.append(inferred_headers[column_index])
            else:
                merged_headers.append(header)
        headers = merged_headers
        header_mode = "explicit+inferred"
    data_profile = xlsx_data_profile(rows, index, headers)
    data_score = (
        data_profile["valid_data_row_count"] * 45
        + data_profile["quantity_row_count"] * 12
        + data_profile["average_cost_row_count"] * 10
        + data_profile["market_row_count"] * 8
        + data_profile["supporting_field_row_count"] * 8
    )
    if data_profile["valid_data_row_count"] == 0:
        data_score -= 80
    if xlsx_sheet_name_score(sheet_name) < 0 and data_profile["valid_data_row_count"] < 2:
        data_score -= 120
    score = column_score + data_score + xlsx_sheet_name_score(sheet_name)
    return {
        "header_row_index": index,
        "header_row_number": index + 1 if index >= 0 else None,
        "header_start_row_index": header_start_index if header_start_index is not None else index,
        "header_start_row_number": (
            (header_start_index if header_start_index is not None else index) + 1
            if (header_start_index if header_start_index is not None else index) >= 0
            else None
        ),
        "data_start_row_index": index + 1,
        "data_start_row_number": index + 2,
        "header_depth": header_depth,
        "headers": headers,
        "source_headers": source_headers,
        "header_mode": header_mode,
        "canonical_columns": canonical_columns(headers),
        "column_score": column_score,
        "data_score": data_score,
        "score": score,
        **data_profile,
    }


def combine_xlsx_header_rows(header_rows: list[list[str]]) -> list[str]:
    width = max((len(row) for row in header_rows), default=0)
    combined: list[str] = []
    for column_index in range(width):
        parts: list[str] = []
        for row in header_rows:
            value = str(row[column_index] if column_index < len(row) else "").strip()
            if value and value not in parts:
                parts.append(value)
        combined.append(" ".join(parts))
    return combined


def xlsx_header_candidates(
    rows: list[list[str]],
    sheet_name: str = "",
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    headerless_candidate_added = False
    scan_rows = [
        [str(value or "").strip() for value in row[:XLSX_HEADER_SCAN_MAX_COLUMNS]]
        for row in rows[:XLSX_HEADER_SCAN_MAX_ROWS]
    ]
    for index, headers in enumerate(scan_rows):
        candidate_headers: list[tuple[int, int, list[str]]] = []
        if not headerless_candidate_added and xlsx_row_looks_like_portfolio_data(headers):
            candidate_headers.append((index - 1, 0, [""] * len(headers)))
            headerless_candidate_added = True
        if any(headers):
            candidate_headers.append((index, 1, headers))
        if index > 0 and any(scan_rows[index - 1]):
            candidate_headers.append(
                (
                    index - 1,
                    2,
                    combine_xlsx_header_rows([scan_rows[index - 1], headers]),
                )
            )
        if (
            index > 1
            and any(scan_rows[index - 1])
            and any(scan_rows[index - 2])
        ):
            candidate_headers.append(
                (
                    index - 2,
                    3,
                    combine_xlsx_header_rows(
                        [
                            scan_rows[index - 2],
                            scan_rows[index - 1],
                            headers,
                        ]
                    ),
                )
            )
        for header_start_index, header_depth, candidate_header_values in candidate_headers:
            if header_depth != 0 and not any(candidate_header_values):
                continue
            column_score = portfolio_header_score(candidate_header_values)
            non_empty_header_cells = sum(
                1 for value in candidate_header_values if str(value or "").strip()
            )
            if (
                header_depth != 0
                and column_score <= 0
                and (
                    header_depth != 1
                    or header_start_index >= XLSX_INFERRED_HEADER_SCAN_ROWS
                    or non_empty_header_cells < 2
                )
            ):
                continue
            score_index = header_start_index if header_depth == 0 else index
            candidate = score_xlsx_header_candidate(
                rows,
                score_index,
                candidate_header_values,
                sheet_name,
                header_start_index=header_start_index,
                header_depth=header_depth,
            )
            if candidate is not None:
                candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda item: (
            int(item.get("score", 0)),
            int(item.get("header_depth", 1)),
            -int(item.get("header_row_index", 0)),
        ),
        reverse=True,
    )


def find_portfolio_header_row(rows: list[list[str]]) -> tuple[int, list[str]] | None:
    candidates = xlsx_header_candidates(rows)
    if not candidates:
        return None
    best = candidates[0]
    return int(best["header_row_index"]), [str(header) for header in best["headers"]]


def scan_xlsx_workbook(path: Path, include_rows: bool = False) -> dict[str, Any]:
    namespace = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    with _open_xlsx_workbook_unlocked(path) as workbook:
        shared_strings = read_xlsx_shared_strings(workbook, namespace)
        sheet_entries = xlsx_sheet_entries(workbook)
        sheets: list[dict[str, Any]] = []

        for sheet_index, (sheet_name, sheet_path) in enumerate(sheet_entries):
            sheet_root = ET.fromstring(workbook.read(sheet_path))
            rows = xlsx_rows_from_root(sheet_root, shared_strings, namespace)
            header_candidates = xlsx_header_candidates(rows, sheet_name)
            sheet_scan: dict[str, Any] = {
                "sheet_index": sheet_index,
                "sheet_name": sheet_name,
                "sheet_path": sheet_path,
                "row_count": len(rows),
                "usable": False,
                "score": 0,
                "header_row_index": None,
                "header_row_number": None,
                "headers": [],
                "canonical_columns": [],
                "data_row_count": 0,
                "valid_data_row_count": 0,
                "header_candidates": [
                    public_xlsx_header_candidate(candidate)
                    for candidate in header_candidates[:5]
                ],
            }
            if header_candidates:
                best_header = header_candidates[0]
                header_index = int(best_header["header_row_index"])
                headers = [str(header) for header in best_header.get("headers", [])]
                records = xlsx_records_from_rows(rows, header_index, headers)
                canonical = canonical_columns(headers)
                valid_data_rows = int(best_header.get("valid_data_row_count", 0))
                usable = valid_data_rows > 0 and not (
                    xlsx_sheet_name_score(sheet_name) < 0 and valid_data_rows < 2
                )
                public_valid_data_rows = valid_data_rows if usable else 0
                sheet_scan.update(
                    {
                        "usable": usable,
                        "score": int(best_header.get("score", 0)),
                        "column_score": int(best_header.get("column_score", 0)),
                        "data_score": int(best_header.get("data_score", 0)),
                        "header_mode": str(best_header.get("header_mode", "")),
                        "source_headers": best_header.get("source_headers", []),
                        "header_start_row_index": best_header.get("header_start_row_index"),
                        "header_start_row_number": best_header.get("header_start_row_number"),
                        "header_depth": best_header.get("header_depth", 1),
                        "header_row_index": header_index,
                        "header_row_number": best_header.get("header_row_number"),
                        "data_start_row_index": best_header.get("data_start_row_index"),
                        "data_start_row_number": best_header.get("data_start_row_number"),
                        "headers": headers,
                        "canonical_columns": canonical,
                        "data_row_count": len(records),
                        "valid_data_row_count": public_valid_data_rows,
                    }
                )
            if include_rows:
                sheet_scan["rows"] = rows
            sheets.append(sheet_scan)

    selected_sheet = next(
        (
            public_xlsx_sheet_scan(sheet)
            for sheet in sorted(
                sheets,
                key=lambda item: int(item.get("score", 0)),
                reverse=True,
            )
            if sheet.get("usable")
        ),
        None,
    )
    return {
        "sheet_count": len(sheets),
        "selected_sheet": selected_sheet,
        "sheets": sheets if include_rows else [public_xlsx_sheet_scan(sheet) for sheet in sheets],
    }


def public_xlsx_sheet_scan(sheet: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in sheet.items()
        if key != "rows"
    }


def public_xlsx_header_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    public_keys = {
        "header_row_index",
        "header_row_number",
        "header_start_row_index",
        "header_start_row_number",
        "data_start_row_index",
        "data_start_row_number",
        "header_depth",
        "header_mode",
        "headers",
        "source_headers",
        "canonical_columns",
        "column_score",
        "data_score",
        "score",
        "non_empty_row_count",
        "valid_data_row_count",
        "symbol_row_count",
        "quantity_row_count",
        "average_cost_row_count",
        "market_row_count",
        "supporting_field_row_count",
    }
    return {key: value for key, value in candidate.items() if key in public_keys}


def xlsx_rows(path: Path) -> list[list[str]]:
    namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with _open_xlsx_workbook_unlocked(path) as workbook:
        shared_strings = read_xlsx_shared_strings(workbook, namespace)
        _sheet_title, sheet_name = xlsx_sheet_entries(workbook)[0]
        sheet_root = ET.fromstring(workbook.read(sheet_name))

    return xlsx_rows_from_root(sheet_root, shared_strings, namespace)


def xlsx_rows_from_root(
    sheet_root: ET.Element,
    shared_strings: list[str],
    namespace: dict[str, str],
) -> list[list[str]]:
    output: list[list[str]] = []
    for row in sheet_root.findall(".//m:sheetData/m:row", namespace):
        try:
            row_number = int(str(row.attrib.get("r", "")))
        except ValueError:
            row_number = len(output) + 1
        while len(output) < max(0, row_number - 1):
            output.append([])
        values: list[str] = []
        for cell in row.findall("m:c", namespace):
            index = xlsx_column_index(str(cell.attrib.get("r", "")))
            while len(values) < index:
                values.append("")
            values.append(read_xlsx_cell(cell, shared_strings, namespace))
        if row_number <= 0:
            output.append(values)
        elif len(output) == row_number - 1:
            output.append(values)
        else:
            output[row_number - 1] = values
    apply_xlsx_merged_cells(output, sheet_root, namespace)
    return output


def apply_xlsx_merged_cells(
    rows: list[list[str]],
    sheet_root: ET.Element,
    namespace: dict[str, str],
) -> None:
    for merged_cell in sheet_root.findall(".//m:mergeCells/m:mergeCell", namespace):
        reference = str(merged_cell.attrib.get("ref", ""))
        coordinates = xlsx_range_coordinates(reference)
        if coordinates is None:
            continue
        start_row, start_column, end_row, end_column = coordinates
        while len(rows) <= start_row:
            rows.append([])
        while len(rows[start_row]) <= start_column:
            rows[start_row].append("")
        value = rows[start_row][start_column]
        if not value:
            continue
        for row_index in range(start_row, end_row + 1):
            while len(rows) <= row_index:
                rows.append([])
            for column_index in range(start_column, end_column + 1):
                while len(rows[row_index]) <= column_index:
                    rows[row_index].append("")
                if not rows[row_index][column_index]:
                    rows[row_index][column_index] = value


def xlsx_sheet_entries(workbook: zipfile.ZipFile) -> list[tuple[str, str]]:
    names = set(workbook.namelist())
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        namespace = {
            "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
        }
        workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
        rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        relationships = {
            str(rel.attrib.get("Id", "")): normalize_xlsx_relationship_target(
                "xl",
                str(rel.attrib.get("Target", "")),
            )
            for rel in rels_root.findall("rel:Relationship", namespace)
        }
        entries: list[tuple[str, str]] = []
        for index, sheet in enumerate(workbook_root.findall(".//m:sheets/m:sheet", namespace), start=1):
            sheet_name = str(sheet.attrib.get("name") or f"Sheet{index}")
            relation_id = str(
                sheet.attrib.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                    "",
                )
            )
            sheet_path = relationships.get(relation_id, "")
            if sheet_path in names:
                entries.append((sheet_name, sheet_path))
        if entries:
            return entries

    fallback = sorted(
        (
            name
            for name in names
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        ),
        key=natural_sort_key,
    )
    if not fallback:
        raise KeyError("xl/worksheets/sheet1.xml")
    return [(Path(name).stem, name) for name in fallback]


def normalize_xlsx_relationship_target(base_dir: str, target: str) -> str:
    raw = target.replace("\\", "/").strip()
    if raw.startswith("/"):
        path = PurePosixPath(raw.lstrip("/"))
    else:
        path = PurePosixPath(base_dir) / raw
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def natural_sort_key(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def first_xlsx_sheet_path(workbook: zipfile.ZipFile) -> str:
    return xlsx_sheet_entries(workbook)[0][1]


def read_xlsx_shared_strings(
    workbook: zipfile.ZipFile,
    namespace: dict[str, str],
) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("m:si", namespace):
        parts = [
            text.text or ""
            for text in item.findall(".//m:t", namespace)
        ]
        strings.append("".join(parts))
    return strings


def xlsx_column_index(reference: str) -> int:
    letters = re.sub(r"[^A-Za-z]", "", reference).upper()
    if not letters:
        return 0
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(0, index - 1)


def xlsx_cell_coordinates(reference: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\$?([A-Za-z]+)\$?(\d+)", str(reference).strip())
    if not match:
        return None
    row_number = int(match.group(2))
    if row_number <= 0:
        return None
    return row_number - 1, xlsx_column_index(match.group(1))


def xlsx_range_coordinates(reference: str) -> tuple[int, int, int, int] | None:
    parts = [part.strip() for part in str(reference).split(":") if part.strip()]
    if not parts:
        return None
    start = xlsx_cell_coordinates(parts[0])
    end = xlsx_cell_coordinates(parts[-1])
    if start is None or end is None:
        return None
    start_row, start_column = start
    end_row, end_column = end
    return (
        min(start_row, end_row),
        min(start_column, end_column),
        max(start_row, end_row),
        max(start_column, end_column),
    )


def read_xlsx_cell(
    cell: ET.Element,
    shared_strings: list[str],
    namespace: dict[str, str],
) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(
            text.text or ""
            for text in cell.findall(".//m:t", namespace)
        ).strip()

    value = cell.find("m:v", namespace)
    text = "" if value is None or value.text is None else value.text
    if cell_type == "s":
        try:
            return shared_strings[int(text)].strip()
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if text == "1" else "FALSE"
    return text.strip()


def rows_to_holdings(rows: Iterable[Any]) -> list[Holding]:
    holdings: list[Holding] = []
    for row_index, raw_row in enumerate(rows, start=1):
        if not isinstance(raw_row, dict):
            continue
        mapped: dict[str, Any] = {}
        for key, value in raw_row.items():
            canonical = canonical_column(str(key))
            if canonical:
                mapped[canonical] = value
        symbol = normalize_symbol(str(mapped.get("symbol") or raw_row.get("symbol") or ""))
        if not symbol:
            continue
        market = infer_market(
            symbol,
            str(mapped.get("market") or ""),
            str(mapped.get("asset_type") or ""),
        )
        quantity = parse_float(mapped.get("quantity"))
        average_cost = parse_float(mapped.get("average_cost"))
        currency = infer_currency(market, str(mapped.get("currency") or ""))
        holdings.append(
            Holding(
                symbol=symbol,
                name=str(mapped.get("name") or "").strip(),
                market=market,
                asset_type=str(mapped.get("asset_type") or "").strip(),
                quantity=quantity or 0.0,
                average_cost=average_cost,
                currency=currency,
                principal_amount=parse_float(mapped.get("principal_amount")),
                principal_currency=str(
                    mapped.get("principal_currency") or currency
                ).strip().upper(),
                principal_twd=parse_float(mapped.get("principal_twd")),
                source_row=row_index,
            )
        )
    if not holdings:
        raise InvestmentManagerError("Portfolio file has no usable holdings.")
    return holdings


def request_text(url: str, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 GPTBridgeInvestmentManager/1.0",
            "Accept": "application/json,text/csv,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def request_json(url: str, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> Any:
    return json.loads(request_text(url, timeout=timeout))


def parse_unix_timestamp(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return utc_now_text()
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().isoformat()


def market_status(market: str, now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or utc_now()
    if market == "CRYPTO":
        return {
            "market": market,
            "is_open": True,
            "state": "open",
            "timezone": "UTC",
            "local_time": now.isoformat(),
        }
    session_info = MARKET_SESSIONS.get(market)
    if not session_info:
        return {
            "market": market,
            "is_open": False,
            "state": "snapshot_only",
            "timezone": "UTC",
            "local_time": now.isoformat(),
        }
    zone = timezone_for(str(session_info["timezone"]), now)
    local_now = now.astimezone(zone)
    is_weekday = local_now.weekday() < 5
    is_open = False
    if is_weekday:
        for start, end in session_info["sessions"]:
            if start <= local_now.time() <= end:
                is_open = True
                break
    return {
        "market": market,
        "is_open": is_open,
        "state": "open" if is_open else "closed",
        "timezone": str(session_info["timezone"]),
        "local_time": local_now.isoformat(),
    }


def yahoo_symbol(holding: Holding) -> str:
    symbol = holding.symbol.upper()
    if holding.market == "TW" and not symbol.endswith((".TW", ".TWO")):
        return f"{symbol}.TW"
    if holding.market == "HK" and not symbol.endswith(".HK"):
        if symbol.isdigit():
            return f"{int(symbol):04d}.HK"
        return f"{symbol}.HK"
    if holding.market == "CRYPTO":
        if "/" in symbol:
            base, quote = symbol.split("/", 1)
            return f"{base}-{quote or 'USD'}"
        if "-" not in symbol:
            return f"{symbol}-USD"
    return symbol


class QuoteProvider:
    name = "base"

    def can_handle(self, holding: Holding) -> bool:
        return True

    def quote(self, context: QuoteContext) -> Quote:
        raise NotImplementedError


class YahooChartProvider(QuoteProvider):
    name = "yahoo-chart"

    def quote(self, context: QuoteContext) -> Quote:
        requested = yahoo_symbol(context.holding)
        encoded = urllib.parse.quote(requested, safe="")
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{encoded}?range=1d&interval=1m"
        )
        payload = request_json(url)
        chart = payload.get("chart", {}) if isinstance(payload, dict) else {}
        error = chart.get("error")
        if error:
            raise QuoteProviderError(str(error))
        results = chart.get("result") or []
        if not results:
            raise QuoteProviderError("No chart result.")
        meta = results[0].get("meta", {})
        price = parse_float(meta.get("regularMarketPrice"))
        if price is None:
            indicators = results[0].get("indicators", {}).get("quote", [])
            closes = indicators[0].get("close", []) if indicators else []
            numeric_closes = [parse_float(value) for value in closes]
            numeric_closes = [value for value in numeric_closes if value is not None]
            price = numeric_closes[-1] if numeric_closes else None
        if price is None:
            raise QuoteProviderError("No market price.")
        previous_close = parse_float(meta.get("chartPreviousClose"))
        change = price - previous_close if previous_close is not None else None
        change_percent = (
            change / previous_close * 100
            if change is not None and previous_close
            else None
        )
        as_of = parse_unix_timestamp(meta.get("regularMarketTime"))
        return Quote(
            symbol=str(meta.get("symbol") or requested),
            requested_symbol=requested,
            provider=self.name,
            price=price,
            currency=str(meta.get("currency") or context.holding.currency or ""),
            previous_close=previous_close,
            change=change,
            change_percent=change_percent,
            as_of=as_of,
            market_state=str(meta.get("marketState") or ""),
            exchange=str(meta.get("fullExchangeName") or meta.get("exchangeName") or ""),
            raw_market=str(meta.get("exchangeTimezoneName") or ""),
        )


class YahooQuoteProvider(QuoteProvider):
    name = "yahoo-quote"

    def quote(self, context: QuoteContext) -> Quote:
        requested = yahoo_symbol(context.holding)
        encoded = urllib.parse.quote(requested, safe=",")
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={encoded}"
        payload = request_json(url)
        results = (
            payload.get("quoteResponse", {}).get("result", [])
            if isinstance(payload, dict)
            else []
        )
        if not results:
            raise QuoteProviderError("No quote result.")
        item = results[0]
        price = parse_float(item.get("regularMarketPrice"))
        if price is None:
            raise QuoteProviderError("No regular market price.")
        previous_close = parse_float(item.get("regularMarketPreviousClose"))
        return Quote(
            symbol=str(item.get("symbol") or requested),
            requested_symbol=requested,
            provider=self.name,
            price=price,
            currency=str(item.get("currency") or context.holding.currency or ""),
            previous_close=previous_close,
            change=parse_float(item.get("regularMarketChange")),
            change_percent=parse_float(item.get("regularMarketChangePercent")),
            as_of=parse_unix_timestamp(item.get("regularMarketTime")),
            market_state=str(item.get("marketState") or ""),
            exchange=str(item.get("fullExchangeName") or item.get("exchange") or ""),
        )


class CoinGeckoProvider(QuoteProvider):
    name = "coingecko"

    def can_handle(self, holding: Holding) -> bool:
        return holding.market == "CRYPTO"

    def quote(self, context: QuoteContext) -> Quote:
        symbol = context.holding.symbol.upper().split("-", 1)[0].split("/", 1)[0]
        coin_id = CRYPTO_ID_MAP.get(symbol)
        if not coin_id:
            raise QuoteProviderError(f"Unsupported crypto symbol: {symbol}")
        currency = (context.holding.currency or "USD").lower()
        url = (
            "https://api.coingecko.com/api/v3/simple/price?"
            + urllib.parse.urlencode(
                {
                    "ids": coin_id,
                    "vs_currencies": currency,
                    "include_24hr_change": "true",
                }
            )
        )
        payload = request_json(url)
        item = payload.get(coin_id, {}) if isinstance(payload, dict) else {}
        price = parse_float(item.get(currency))
        if price is None:
            raise QuoteProviderError("No crypto price.")
        return Quote(
            symbol=symbol,
            requested_symbol=coin_id,
            provider=self.name,
            price=price,
            currency=currency.upper(),
            change_percent=parse_float(item.get(f"{currency}_24h_change")),
            as_of=utc_now_text(),
            market_state="REGULAR",
            exchange="CoinGecko",
        )


class AlphaVantageProvider(QuoteProvider):
    name = "alphavantage"

    def can_handle(self, holding: Holding) -> bool:
        return bool(os.environ.get("ALPHAVANTAGE_API_KEY"))

    def quote(self, context: QuoteContext) -> Quote:
        api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
        if not api_key:
            raise QuoteProviderError("ALPHAVANTAGE_API_KEY is not configured.")
        requested = yahoo_symbol(context.holding)
        if requested.endswith((".TW", ".TWO", ".HK")):
            requested = context.holding.symbol
        url = (
            "https://www.alphavantage.co/query?"
            + urllib.parse.urlencode(
                {
                    "function": "GLOBAL_QUOTE",
                    "symbol": requested,
                    "apikey": api_key,
                }
            )
        )
        payload = request_json(url)
        item = payload.get("Global Quote", {}) if isinstance(payload, dict) else {}
        price = parse_float(item.get("05. price"))
        if price is None:
            raise QuoteProviderError("No Alpha Vantage price.")
        previous_close = parse_float(item.get("08. previous close"))
        return Quote(
            symbol=str(item.get("01. symbol") or requested),
            requested_symbol=requested,
            provider=self.name,
            price=price,
            currency=context.holding.currency,
            previous_close=previous_close,
            change=parse_float(item.get("09. change")),
            change_percent=parse_float(item.get("10. change percent")),
            as_of=utc_now_text(),
            market_state="",
            exchange="Alpha Vantage",
        )


class TwseProvider(QuoteProvider):
    name = "twse"

    def can_handle(self, holding: Holding) -> bool:
        return holding.market == "TW" and bool(re.fullmatch(r"\d{4,6}", holding.symbol))

    def quote(self, context: QuoteContext) -> Quote:
        errors: list[str] = []
        for exchange_prefix in ("tse", "otc"):
            ex_ch = f"{exchange_prefix}_{context.holding.symbol}.tw"
            url = (
                "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?"
                + urllib.parse.urlencode({"ex_ch": ex_ch, "json": "1", "delay": "0"})
            )
            try:
                payload = request_json(url)
            except Exception as exc:
                errors.append(str(exc))
                continue
            rows = payload.get("msgArray", []) if isinstance(payload, dict) else []
            if not rows:
                errors.append("No TWSE quote rows.")
                continue
            item = rows[0]
            price = parse_float(item.get("z")) or parse_float(item.get("y"))
            if price is None:
                errors.append("No TWSE price.")
                continue
            previous_close = parse_float(item.get("y"))
            trade_date = str(item.get("d") or "")
            trade_time = str(item.get("t") or "")
            as_of = utc_now_text()
            if re.fullmatch(r"\d{8}", trade_date) and trade_time:
                try:
                    local_dt = datetime.strptime(
                        f"{trade_date} {trade_time}",
                        "%Y%m%d %H:%M:%S",
                    ).replace(tzinfo=timezone_for("Asia/Taipei", context.now_utc))
                    as_of = local_dt.astimezone().isoformat()
                except ValueError:
                    pass
            return Quote(
                symbol=str(item.get("c") or context.holding.symbol),
                requested_symbol=ex_ch,
                provider=self.name,
                price=price,
                currency="TWD",
                previous_close=previous_close,
                change=price - previous_close if previous_close is not None else None,
                change_percent=(
                    (price - previous_close) / previous_close * 100
                    if previous_close
                    else None
                ),
                as_of=as_of,
                market_state=market_status("TW", context.now_utc)["state"].upper(),
                exchange="TWSE" if exchange_prefix == "tse" else "TPEx",
            )
        raise QuoteProviderError("; ".join(errors) or "TWSE quote failed.")


def provider_registry() -> dict[str, QuoteProvider]:
    providers: list[QuoteProvider] = [
        TwseProvider(),
        YahooChartProvider(),
        YahooQuoteProvider(),
        CoinGeckoProvider(),
        AlphaVantageProvider(),
    ]
    return {provider.name: provider for provider in providers}


def provider_order_for_holding(
    holding: Holding,
    requested_order: list[str],
) -> list[str]:
    preferred = list(requested_order)
    if holding.market == "CRYPTO":
        preferred = ["coingecko", *[item for item in preferred if item != "coingecko"]]
    elif holding.market == "TW":
        preferred = ["twse", *[item for item in preferred if item != "twse"]]
    return list(dict.fromkeys(preferred))


def quote_holding(
    holding: Holding,
    providers: dict[str, QuoteProvider],
    provider_order: list[str],
    now: datetime,
) -> tuple[Quote | None, list[QuoteAttempt]]:
    quote, attempts, _candidates = quote_holding_candidates(
        holding,
        providers,
        provider_order,
        now,
        max_successes=1,
    )
    return quote, attempts


def quote_holding_candidates(
    holding: Holding,
    providers: dict[str, QuoteProvider],
    provider_order: list[str],
    now: datetime,
    *,
    max_successes: int = 2,
) -> tuple[Quote | None, list[QuoteAttempt], list[Quote]]:
    context = QuoteContext(holding=holding, now_utc=now)
    candidates: list[Quote] = []
    for provider_name in provider_order_for_holding(holding, provider_order):
        provider = providers.get(provider_name)
        if provider is None:
            context.attempts.append(
                QuoteAttempt(provider_name, False, "Provider is not available.")
            )
            continue
        if not provider.can_handle(holding):
            context.attempts.append(
                QuoteAttempt(provider.name, False, "Provider skipped this holding.")
            )
            continue
        try:
            quote = provider.quote(context)
        except (QuoteProviderError, urllib.error.URLError, TimeoutError, OSError) as exc:
            context.attempts.append(QuoteAttempt(provider.name, False, str(exc)))
            continue
        except Exception as exc:
            context.attempts.append(QuoteAttempt(provider.name, False, str(exc)))
            continue
        context.attempts.append(QuoteAttempt(provider.name, True, "ok"))
        candidates.append(quote)
        if len(candidates) >= max(1, max_successes):
            break
    primary_quote = candidates[0] if candidates else None
    return primary_quote, context.attempts, candidates


def quote_to_dict(quote: Quote) -> dict[str, Any]:
    return {
        "symbol": quote.symbol,
        "requested_symbol": quote.requested_symbol,
        "provider": quote.provider,
        "price": round_number(quote.price),
        "currency": quote.currency,
        "previous_close": round_number(quote.previous_close),
        "change": round_number(quote.change),
        "change_percent": round_number(quote.change_percent),
        "as_of": quote.as_of,
        "market_state": quote.market_state,
        "exchange": quote.exchange,
        "raw_market": quote.raw_market,
    }


def round_number(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def holding_to_report(
    holding: Holding,
    quote: Quote | None,
    attempts: list[QuoteAttempt],
    now: datetime,
) -> dict[str, Any]:
    status = market_status(holding.market, now)
    base = {
        "symbol": holding.symbol,
        "name": holding.name,
        "market": holding.market,
        "asset_type": holding.asset_type,
        "quantity": round_number(holding.quantity),
        "average_cost": round_number(holding.average_cost),
        "currency": holding.currency,
        "market_status": status,
        "attempts": [
            {
                "provider": attempt.provider,
                "ok": attempt.ok,
                "message": attempt.message,
            }
            for attempt in attempts
        ],
    }
    if quote is None:
        return {
            **base,
            "status": "quote_failed",
            "quote": None,
            "market_value": None,
            "cost_basis": (
                round_number(holding.quantity * holding.average_cost)
                if holding.average_cost is not None
                else None
            ),
            "unrealized_pnl": None,
            "unrealized_pnl_percent": None,
        }

    market_value = holding.quantity * quote.price
    cost_basis = (
        holding.quantity * holding.average_cost
        if holding.average_cost is not None
        else None
    )
    pnl = market_value - cost_basis if cost_basis is not None else None
    pnl_percent = pnl / cost_basis * 100 if pnl is not None and cost_basis else None
    return {
        **base,
        "status": "quoted",
        "quote": {
            "symbol": quote.symbol,
            "requested_symbol": quote.requested_symbol,
            "provider": quote.provider,
            "price": round_number(quote.price),
            "currency": quote.currency,
            "previous_close": round_number(quote.previous_close),
            "change": round_number(quote.change),
            "change_percent": round_number(quote.change_percent),
            "as_of": quote.as_of,
            "market_state": quote.market_state,
            "exchange": quote.exchange,
            "raw_market": quote.raw_market,
        },
        "market_value": round_number(market_value),
        "cost_basis": round_number(cost_basis),
        "unrealized_pnl": round_number(pnl),
        "unrealized_pnl_percent": round_number(pnl_percent),
    }


def totals_by_currency(holdings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, float]] = {}
    for holding in holdings:
        quote = holding.get("quote") or {}
        currency = str(quote.get("currency") or holding.get("currency") or "UNKNOWN")
        bucket = totals.setdefault(
            currency,
            {
                "market_value": 0.0,
                "cost_basis": 0.0,
                "unrealized_pnl": 0.0,
                "quoted_count": 0,
            },
        )
        if isinstance(holding.get("market_value"), (int, float)):
            bucket["market_value"] += float(holding["market_value"])
        if isinstance(holding.get("cost_basis"), (int, float)):
            bucket["cost_basis"] += float(holding["cost_basis"])
        if isinstance(holding.get("unrealized_pnl"), (int, float)):
            bucket["unrealized_pnl"] += float(holding["unrealized_pnl"])
        if holding.get("status") == "quoted":
            bucket["quoted_count"] += 1
    return {
        currency: {
            key: int(value) if key == "quoted_count" else round_number(value, 4)
            for key, value in values.items()
        }
        for currency, values in totals.items()
    }


def markets_summary(holdings: list[Holding], now: datetime) -> dict[str, Any]:
    markets = sorted({holding.market for holding in holdings if holding.market})
    statuses = {market: market_status(market, now) for market in markets}
    open_markets = [
        market for market, status in statuses.items() if bool(status.get("is_open"))
    ]
    watchable_markets = [
        market
        for market in markets
        if market in MARKET_SESSIONS or market == "CRYPTO"
    ]
    return {
        "markets": statuses,
        "open_markets": open_markets,
        "watchable_markets": watchable_markets,
        "all_watchable_markets_closed": bool(watchable_markets)
        and not open_markets,
    }


def create_snapshot(
    holdings: list[Holding],
    providers: dict[str, QuoteProvider],
    provider_order: list[str],
) -> dict[str, Any]:
    now = utc_now()
    holding_reports: list[dict[str, Any]] = []
    for holding in holdings:
        quote, attempts = quote_holding(holding, providers, provider_order, now)
        holding_reports.append(holding_to_report(holding, quote, attempts, now))
    quoted_count = sum(1 for holding in holding_reports if holding["status"] == "quoted")
    summary = markets_summary(holdings, now)
    return {
        "timestamp": now.isoformat(),
        "holding_count": len(holding_reports),
        "quoted_count": quoted_count,
        "failed_quote_count": len(holding_reports) - quoted_count,
        "holdings": holding_reports,
        "totals_by_currency": totals_by_currency(holding_reports),
        "market_summary": summary,
    }


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


if __name__ == "__main__":
    raise SystemExit(main())
