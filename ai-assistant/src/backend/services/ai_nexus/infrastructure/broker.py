from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .analytics_repository import InvestmentAnalyticsStore, number, parse_datetime, utc_text
from .portfolio_file import scan_xlsx_workbook
from .privacy import protect_text, unprotect_text


HEADER_ALIASES = {
    "occurred_at": {"date", "datetime", "trade date", "transaction date", "日期", "交易日期", "成交日期", "時間"},
    "symbol": {"symbol", "ticker", "stock", "security", "code", "股票代號", "證券代號", "商品代號", "代號"},
    "side": {"side", "action", "type", "transaction type", "買賣別", "交易類型", "類型"},
    "quantity": {"quantity", "qty", "shares", "units", "成交股數", "數量", "股數"},
    "price": {"price", "trade price", "unit price", "成交價", "價格", "單價"},
    "amount": {"amount", "net amount", "gross amount", "cash", "金額", "成交金額", "淨額"},
    "fee": {"fee", "commission", "handling fee", "手續費", "佣金"},
    "tax": {"tax", "transaction tax", "交易稅", "稅"},
    "currency": {"currency", "ccy", "幣別", "貨幣"},
}

SIDE_ALIASES = {
    "BUY": {"buy", "b", "買", "買進", "買入"},
    "SELL": {"sell", "s", "賣", "賣出"},
    "DIVIDEND": {"dividend", "div", "股息", "配息", "現金股利"},
    "FEE": {"fee", "commission", "手續費", "費用"},
    "CASH_IN": {"deposit", "cash in", "入金", "存入"},
    "CASH_OUT": {"withdrawal", "cash out", "出金", "提款"},
}


def _key(value: Any) -> str:
    return re.sub(r"[\s_\-:/()（）]+", " ", str(value or "").strip().casefold()).strip()


def _canonical_headers(headers: Iterable[Any]) -> dict[int, str]:
    aliases = {alias: name for name, choices in HEADER_ALIASES.items() for alias in choices}
    return {index: aliases[_key(value)] for index, value in enumerate(headers) if _key(value) in aliases}


def _as_number(value: Any) -> float:
    text = str(value or "").strip().replace(",", "").replace("$", "")
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    return number(text)


def _side(value: Any) -> str:
    normalized = _key(value)
    for side, aliases in SIDE_ALIASES.items():
        if normalized in aliases:
            return side
    return normalized.upper()


def _broker_datetime(value: Any) -> datetime | None:
    parsed_number = _as_number(value)
    if 20_000 <= parsed_number <= 80_000:
        return datetime(1899, 12, 30, tzinfo=timezone.utc) + timedelta(days=parsed_number)
    return parse_datetime(value)


def _normalize_record(record: dict[str, Any]) -> dict[str, Any] | None:
    occurred = _broker_datetime(record.get("occurred_at"))
    side = _side(record.get("side"))
    symbol = str(record.get("symbol") or "").strip().upper()
    if occurred is None or side not in SIDE_ALIASES:
        return None
    quantity = abs(_as_number(record.get("quantity")))
    price = abs(_as_number(record.get("price")))
    amount = abs(_as_number(record.get("amount")))
    if price <= 0 and quantity > 0 and amount > 0:
        price = amount / quantity
    if amount <= 0 and quantity > 0 and price > 0:
        amount = quantity * price
    if side in {"BUY", "SELL"} and (not symbol or quantity <= 0 or price <= 0):
        return None
    if side == "DIVIDEND" and not symbol:
        return None
    return {
        "occurred_at": utc_text(occurred),
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "amount": amount,
        "fee": abs(_as_number(record.get("fee"))),
        "tax": abs(_as_number(record.get("tax"))),
        "currency": str(record.get("currency") or "").strip().upper(),
        "raw": record,
    }


def _records_from_matrix(rows: list[list[Any]]) -> list[dict[str, Any]]:
    best: tuple[int, dict[int, str]] | None = None
    for index, row in enumerate(rows[:40]):
        mapping = _canonical_headers(row)
        if len(mapping) >= 3 and {"occurred_at", "side"}.issubset(mapping.values()):
            if best is None or len(mapping) > len(best[1]):
                best = (index, mapping)
    if best is None:
        raise ValueError("找不到券商明細欄位，至少需要日期、交易類型與商品/金額欄位")
    header_index, mapping = best
    output: list[dict[str, Any]] = []
    for row in rows[header_index + 1 :]:
        record = {name: row[index] if index < len(row) else "" for index, name in mapping.items()}
        normalized = _normalize_record(record)
        if normalized:
            output.append(normalized)
    return output


def _csv_rows(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "utf-16", "cp950", "big5"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        raise ValueError("無法辨識券商檔案編碼")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    return [list(row) for row in csv.reader(io.StringIO(text), dialect=dialect)]


def _pdf_text(path: Path) -> str:
    data = path.read_bytes()
    chunks: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        chunk = match.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            pass
        chunks.append(chunk)
    content = b"\n".join(chunks) if chunks else data
    strings = re.findall(rb"\((?:\\.|[^\\)])*\)", content)
    decoded = []
    for value in strings:
        item = value[1:-1]
        item = re.sub(rb"\\([()\\])", rb"\1", item)
        for encoding in ("utf-8", "utf-16-be", "cp950", "latin-1"):
            try:
                decoded.append(item.decode(encoding))
                break
            except UnicodeDecodeError:
                continue
    return " ".join(decoded)


def _pdf_records(path: Path) -> list[dict[str, Any]]:
    text = _pdf_text(path)
    pattern = re.compile(
        r"(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+"
        r"(?P<symbol>[A-Za-z0-9.\-]+)\s+"
        r"(?P<side>BUY|SELL|DIVIDEND|買進|買入|賣出|股息)\s+"
        r"(?P<quantity>[\d,.]+)\s+(?P<price>[\d,.]+)"
        r"(?:\s+(?P<amount>[\d,.]+))?",
        re.IGNORECASE,
    )
    records = []
    for match in pattern.finditer(text):
        values = match.groupdict()
        normalized = _normalize_record(
            {
                "occurred_at": values["date"],
                "symbol": values["symbol"],
                "side": values["side"],
                "quantity": values["quantity"],
                "price": values["price"],
                "amount": values.get("amount") or "",
            }
        )
        if normalized:
            records.append(normalized)
    if not records:
        raise ValueError("PDF 沒有可擷取的文字交易明細；請改匯出券商 CSV 或 Excel")
    return records


class BrokerReconciliationService:
    def __init__(self, store: InvestmentAnalyticsStore) -> None:
        self.store = store

    def parse(self, path: Path) -> list[dict[str, Any]]:
        suffix = path.suffix.casefold()
        if suffix in {".csv", ".tsv", ".txt"}:
            return _records_from_matrix(_csv_rows(path))
        if suffix == ".xlsx":
            scan = scan_xlsx_workbook(path, include_rows=True)
            candidates = sorted(scan.get("sheets", []), key=lambda item: len(item.get("rows", [])), reverse=True)
            errors = []
            for sheet in candidates:
                try:
                    records = _records_from_matrix(sheet.get("rows", []))
                    if records:
                        return records
                except ValueError as exc:
                    errors.append(str(exc))
            raise ValueError(errors[0] if errors else "Excel 沒有可用的交易明細")
        if suffix == ".pdf":
            return _pdf_records(path)
        raise ValueError("券商明細僅支援 CSV、TSV、XLSX 或文字型 PDF")

    def _match(self, row: dict[str, Any], transactions: list[dict[str, Any]]) -> str:
        occurred = parse_datetime(row["occurred_at"])
        for transaction in transactions:
            transaction_date = parse_datetime(transaction.get("occurred_at"))
            if occurred is None or transaction_date is None or abs(transaction_date - occurred) > timedelta(days=1):
                continue
            if str(transaction.get("symbol") or "").upper() != row["symbol"] or transaction.get("side") != row["side"]:
                continue
            if row["side"] not in {"BUY", "SELL"}:
                transaction_amount = number(transaction.get("price")) * max(1.0, number(transaction.get("quantity")))
                if abs(transaction_amount - row["amount"]) <= max(0.01, row["amount"] * 0.001):
                    return str(transaction.get("transaction_id") or "")
                continue
            if abs(number(transaction.get("quantity")) - row["quantity"]) > max(0.0001, row["quantity"] * 0.001):
                continue
            if abs(number(transaction.get("price")) - row["price"]) > max(0.01, row["price"] * 0.001):
                continue
            return str(transaction.get("transaction_id") or "")
        return ""

    def import_statement(self, path: Path, *, source_name: str = "", broker: str = "") -> dict[str, Any]:
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.store.connect() as connection:
            existing = connection.execute("SELECT import_id FROM broker_imports WHERE source_hash = ?", (source_hash,)).fetchone()
        if existing:
            return self.get_import(str(existing["import_id"]))
        rows = self.parse(path)
        transactions = self.store.list_transactions(5000)
        import_id = uuid.uuid4().hex
        matched = 0
        prepared = []
        for row in rows:
            matched_id = self._match(row, transactions)
            matched += int(bool(matched_id))
            prepared.append(
                (
                    uuid.uuid4().hex, import_id, row["occurred_at"], row["symbol"], row["side"],
                    row["quantity"], row["price"], row["amount"], row["fee"], row["tax"],
                    row["currency"], "matched" if matched_id else "unmatched", matched_id,
                    protect_text(json.dumps(row["raw"], ensure_ascii=False, default=str)),
                )
            )
        summary = {"rows": len(rows), "matched": matched, "differences": len(rows) - matched}
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO broker_imports VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (import_id, utc_text(), source_name or path.name, source_hash, broker, len(rows), matched, len(rows) - matched, "review", protect_text(json.dumps(summary))),
            )
            connection.executemany("INSERT INTO broker_import_rows VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", prepared)
        self.store.audit("broker_statement_import", {"import_id": import_id, **summary})
        return self.get_import(import_id)

    def get_import(self, import_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            header = connection.execute("SELECT * FROM broker_imports WHERE import_id = ?", (import_id,)).fetchone()
            rows = connection.execute(
                "SELECT * FROM broker_import_rows WHERE import_id = ? ORDER BY occurred_at, row_id", (import_id,)
            ).fetchall()
        if not header:
            raise ValueError("找不到券商匯入批次")
        item = dict(header)
        item["summary"] = json.loads(unprotect_text(item.pop("summary_encrypted")) or "{}")
        item["rows"] = []
        for source in rows:
            row = dict(source)
            row["raw"] = json.loads(unprotect_text(row.pop("raw_encrypted")) or "{}")
            item["rows"].append(row)
        return item

    def list_imports(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute("SELECT * FROM broker_imports ORDER BY imported_at DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
        output = []
        for source in rows:
            item = dict(source)
            item["summary"] = json.loads(unprotect_text(item.pop("summary_encrypted")) or "{}")
            output.append(item)
        return output

    def approve_rows(
        self,
        import_id: str,
        row_ids: Iterable[str],
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        with self.store.batch_updates():
            return self._approve_rows_atomic(
                import_id,
                row_ids,
                confirmed=confirmed,
            )

    def _approve_rows_atomic(
        self,
        import_id: str,
        row_ids: Iterable[str],
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("入帳前必須明確確認")
        selected = {str(value) for value in row_ids if str(value).strip()}
        current = self.get_import(import_id)
        approved = 0
        for row in current["rows"]:
            if row["row_id"] not in selected or row["match_status"] != "unmatched":
                continue
            self.store.add_transaction(
                {
                    "transaction_id": f"broker-{row['row_id']}",
                    "occurred_at": row["occurred_at"],
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "quantity": row["quantity"],
                    "price": row["price"],
                    "amount": row["amount"],
                    "fee": row["fee"],
                    "tax": row["tax"],
                    "currency": row["currency"],
                    "note": f"券商對帳匯入 {import_id}",
                }
            )
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE broker_import_rows SET match_status='approved', matched_transaction_id=? WHERE row_id=?",
                    (f"broker-{row['row_id']}", row["row_id"]),
                )
            approved += 1
        with self.store.connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM broker_import_rows WHERE import_id=? AND match_status='unmatched'", (import_id,)
            ).fetchone()[0]
            connection.execute(
                "UPDATE broker_imports SET status=?, difference_count=?, matched_count=matched_count+? WHERE import_id=?",
                ("approved" if remaining == 0 else "review", remaining, approved, import_id),
            )
        self.store.audit("broker_rows_approved", {"import_id": import_id, "approved": approved}, severity="warning")
        return self.get_import(import_id)
