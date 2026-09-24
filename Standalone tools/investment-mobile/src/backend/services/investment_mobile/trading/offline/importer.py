"""InvestmentImportService — staged file import for offline accounts.

Pipeline (all steps explicit, nothing implicit):

    register file → detect format → preview rows → field mapping →
    validate → dedup against existing journal → user confirm → commit

Guarantees:

- The source file is only ever read, never modified.
- No filesystem access outside the user-selected path — the service
  never scans directories on its own.
- A pre-commit snapshot of the account journal is taken; ``rollback``
  restores it and marks the batch REVERTED.
- Duplicate detection keys on the row's content hash inside the target
  account so re-importing the same file is idempotent.

Formats: CSV and JSON natively; XLSX when ``openpyxl`` is installed
(same convention as fund/providers.py).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from .accounts import OfflineAccountService

# Canonical import targets → required logical fields.
TARGET_SCHEMAS: dict[str, dict[str, Any]] = {
    "holdings": {"required": ["instrument_id", "quantity"],
                 "optional": ["avg_cost", "kind"]},
    "cash": {"required": ["currency", "amount"], "optional": []},
    "transactions": {"required": ["instrument_id", "side", "quantity",
                                  "price"],
                     "optional": ["fee", "tax", "traded_at"]},
    "dividends": {"required": ["instrument_id", "amount"],
                  "optional": ["currency", "paid_at"]},
}

# Field aliases → canonical names (common broker export headers).
FIELD_ALIASES = {
    "instrument_id": ["instrument_id", "代碼", "股票代碼", "symbol",
                      "ticker", "code", "商品代碼"],
    "quantity": ["quantity", "股數", "數量", "qty", "shares", "持有數量"],
    "avg_cost": ["avg_cost", "平均成本", "成本", "cost", "avg price"],
    "currency": ["currency", "幣別", "幣種", "ccy"],
    "amount": ["amount", "金額", "現金", "cash"],
    "side": ["side", "買賣", "方向", "direction"],
    "price": ["price", "成交價", "價格", "單價"],
    "fee": ["fee", "手續費", "commission"],
    "tax": ["tax", "交易稅", "證交稅"],
    "traded_at": ["traded_at", "成交日期", "date", "交易日期"],
    "paid_at": ["paid_at", "配息日期", "除息日"],
    "kind": ["kind", "類別", "asset_type"],
}


# Dedup keys per target — only the row's identity fields participate,
# never auto-attached metadata like account currency or timestamps.
_TARGET_CORE_FIELDS: dict[str, tuple[str, ...]] = {
    "holdings": ("instrument_id", "quantity", "avg_cost"),
    "cash": ("currency", "amount"),
    "transactions": ("instrument_id", "side", "quantity", "price"),
    "dividends": ("instrument_id", "amount"),
}


def _hash_row(row: dict[str, Any]) -> str:
    canonical = json.dumps(row, sort_keys=True, ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class InvestmentImportService:
    def __init__(self, state_dir: Path,
                 accounts: OfflineAccountService) -> None:
        self._dir = state_dir / "imports"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._accounts = accounts
        self._batches: dict[str, dict[str, Any]] = {}
        self._batch_path = self._dir / "batches.json"
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._batch_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._batches = data
        except Exception:
            self._batches = {}

    def _persist(self) -> None:
        self._batch_path.write_text(
            json.dumps(self._batches, indent=2, ensure_ascii=False),
            encoding="utf-8")

    # ------------------------------------------------------------------
    # Step 1+2: register + detect + parse
    # ------------------------------------------------------------------
    def start(self, file_path: str, *, target: str,
              account_id: str) -> dict[str, Any]:
        if target not in TARGET_SCHEMAS:
            return {"ok": False, "error_code": "TARGET_UNKNOWN",
                    "targets": sorted(TARGET_SCHEMAS)}
        acct = self._accounts.get(account_id)
        if acct is None:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        path = Path(file_path)
        if not path.is_file():
            return {"ok": False, "error_code": "FILE_NOT_FOUND"}
        try:
            rows = self._parse(path)
        except ValueError as exc:
            return {"ok": False, "error_code": "PARSE_FAILED",
                    "detail": str(exc)}
        if not rows:
            return {"ok": False, "error_code": "EMPTY_FILE"}
        batch_id = f"imp-{uuid.uuid4().hex[:10]}"
        batch = {
            "batch_id": batch_id, "file": str(path),
            "target": target, "account_id": account_id,
            "rows": rows, "status": "PREVIEW",
            "created_at": time.time(), "simulated": False,
            "source": "FILE_IMPORT",
        }
        self._batches[batch_id] = batch
        self._persist()
        return {"ok": True, "batch_id": batch_id,
                "format": path.suffix.lower().lstrip("."),
                "row_count": len(rows),
                "columns": sorted(rows[0].keys()),
                "preview": rows[:10],
                "suggested_mapping": self._suggest_mapping(rows[0])}

    def _parse(self, path: Path) -> list[dict[str, Any]]:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            return [dict(r) for r in csv.DictReader(io.StringIO(text))]
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in ("rows", "data", "records", "items"):
                    if isinstance(data.get(key), list):
                        data = data[key]
                        break
            if not isinstance(data, list):
                raise ValueError("JSON must be a list of objects")
            return [r for r in data if isinstance(r, dict)]
        if suffix == ".xlsx":
            try:
                import openpyxl  # type: ignore
            except ImportError:
                raise ValueError("XLSX import requires openpyxl")
            wb = openpyxl.load_workbook(path, read_only=True,
                                        data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            headers = [str(h) if h is not None else ""
                       for h in next(rows_iter, [])]
            return [
                {headers[i]: c for i, c in enumerate(r) if i < len(headers)}
                for r in rows_iter
            ]
        raise ValueError(f"unsupported format: {suffix}")

    def _suggest_mapping(self, sample: dict[str, Any]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        cols = {str(c).strip().lower(): c for c in sample}
        for canonical, aliases in FIELD_ALIASES.items():
            for alias in aliases:
                if alias.lower() in cols:
                    mapping[cols[alias.lower()]] = canonical
                    break
        return mapping

    # ------------------------------------------------------------------
    # Step 3b: diff preview — classify each row against existing books
    # (new | quantity_changed | cost_changed | cash_changed | duplicate
    #  | conflict). Later data never unconditionally overwrites.
    # ------------------------------------------------------------------
    def diff(self, batch_id: str,
             *, mapping: dict[str, str]) -> dict[str, Any]:
        batch = self._batches.get(batch_id)
        if batch is None:
            return {"ok": False, "error_code": "BATCH_NOT_FOUND"}
        aid, target = batch["account_id"], batch["target"]
        core = _TARGET_CORE_FIELDS[target]
        existing = {self._identity_of(row, target): row
                    for row in self._fetch(aid, target)}
        seen_hashes = self._existing_hashes(aid, target)
        items = []
        for i, raw in enumerate(batch["rows"]):
            row = {mapping.get(k, k): v for k, v in raw.items()}
            h = _hash_row({k: str(row[k]) for k in core if k in row})
            ident = row.get("instrument_id") or row.get("currency")
            cur = existing.get(str(ident))
            if h in seen_hashes:
                kind = "duplicate"
            elif cur is None:
                kind = "new"
            elif target == "holdings":
                if str(cur.get("quantity")) != str(row.get("quantity")):
                    kind = "quantity_changed"
                elif str(cur.get("avg_cost")) != str(
                        row.get("avg_cost", "")):
                    kind = "cost_changed"
                else:
                    kind = "conflict"
            elif target == "cash":
                kind = ("cash_changed"
                        if str(cur.get("amount")) != str(row.get("amount"))
                        else "duplicate")
            else:
                kind = "conflict"   # txn/dividend same identity, diff body
            items.append({"row": i, "kind": kind, "identity": ident,
                          "existing": cur, "incoming": row})
        return {"ok": True, "batch_id": batch_id, "items": items,
                "summary": {
                    k: sum(1 for it in items if it["kind"] == k)
                    for k in ("new", "quantity_changed", "cost_changed",
                              "cash_changed", "duplicate", "conflict")}}

    def _fetch(self, account_id: str, target: str) -> list[dict[str, Any]]:
        return {
            "holdings": self._accounts.holdings,
            "cash": self._accounts.cash,
            "transactions": self._accounts.transactions,
            "dividends": self._accounts.dividends,
        }.get(target, lambda a: [])(account_id)

    def _identity_of(self, row: dict[str, Any], target: str) -> str:
        return str(row.get("instrument_id") or row.get("currency") or "")

    # ------------------------------------------------------------------
    # Step 3+4: apply mapping → validate → dedup → confirm → commit
    # ------------------------------------------------------------------
    def commit(self, batch_id: str, *, mapping: dict[str, str],
               confirm: bool = False) -> dict[str, Any]:
        batch = self._batches.get(batch_id)
        if batch is None:
            return {"ok": False, "error_code": "BATCH_NOT_FOUND"}
        if batch["status"] != "PREVIEW":
            return {"ok": False, "error_code": "BATCH_NOT_COMMITTABLE",
                    "status": batch["status"]}
        schema = TARGET_SCHEMAS[batch["target"]]
        mapped, errors = [], []
        seen: set[str] = set()
        existing = self._existing_hashes(batch["account_id"],
                                         batch["target"])
        for i, raw in enumerate(batch["rows"]):
            row = {mapping.get(k, k): v for k, v in raw.items()}
            missing = [f for f in schema["required"]
                       if f not in row or str(row[f]).strip() == ""]
            if missing:
                errors.append({"row": i, "error": "MISSING_FIELDS",
                               "fields": missing})
                continue
            core = _TARGET_CORE_FIELDS[batch["target"]]
            h = _hash_row({k: str(row[k]) for k in core if k in row})
            if h in seen or h in existing:
                errors.append({"row": i, "error": "DUPLICATE",
                               "hash": h})
                continue
            seen.add(h)
            row["_hash"] = h
            mapped.append(row)
        if errors and not mapped:
            batch["status"] = "FAILED"
            batch["errors"] = errors
            self._persist()
            return {"ok": False, "error_code": "VALIDATION_FAILED",
                    "errors": errors}
        if not confirm:
            return {
                "ok": True, "batch_id": batch_id, "status": "PREVIEW",
                "ready": len(mapped), "errors": errors,
                "note": "dry-run — re-call with confirm=true to write",
            }
        # snapshot before write → rollback support
        snapshot = self._dir / f"snapshot-{batch_id}.jsonl"
        journal = self._accounts._journal_path
        if journal.exists():
            shutil.copy2(journal, snapshot)
        written = 0
        for row in mapped:
            r = self._write_row(batch["account_id"], batch["target"], row)
            if r.get("ok"):
                written += 1
            else:
                errors.append({"row": row, "error": r.get("error_code")})
        batch["status"] = "COMMITTED"
        batch["written"] = written
        batch["errors"] = errors
        batch["committed_at"] = time.time()
        self._persist()
        return {"ok": True, "batch_id": batch_id, "written": written,
                "errors": errors, "status": "COMMITTED"}

    def _existing_hashes(self, account_id: str, target: str) -> set[str]:
        # Content-hash every existing entry of that type in the account.
        out: set[str] = set()
        fetch = {
            "holdings": self._accounts.holdings,
            "cash": self._accounts.cash,
            "transactions": self._accounts.transactions,
            "dividends": self._accounts.dividends,
        }.get(target, lambda a: [])
        core = _TARGET_CORE_FIELDS.get(target, ())
        for row in fetch(account_id):
            out.add(_hash_row({k: str(row[k]) for k in core
                               if k in row}))
        return out

    def _write_row(self, account_id: str, target: str,
                   row: dict[str, Any]) -> dict[str, Any]:
        if target == "holdings":
            return self._accounts.set_holding(
                account_id, row["instrument_id"], row["quantity"],
                row.get("avg_cost", "0"), source="FILE_IMPORT")
        if target == "cash":
            return self._accounts.set_cash(
                account_id, row["currency"], row["amount"],
                source="FILE_IMPORT")
        if target == "transactions":
            return self._accounts.add_transaction(
                account_id, row, source="FILE_IMPORT")
        if target == "dividends":
            return self._accounts.add_dividend(
                account_id, row, source="FILE_IMPORT")
        return {"ok": False, "error_code": "TARGET_UNKNOWN"}

    # ------------------------------------------------------------------
    def rollback(self, batch_id: str) -> dict[str, Any]:
        batch = self._batches.get(batch_id)
        if batch is None:
            return {"ok": False, "error_code": "BATCH_NOT_FOUND"}
        if batch["status"] != "COMMITTED":
            return {"ok": False, "error_code": "BATCH_NOT_COMMITTED"}
        snapshot = self._dir / f"snapshot-{batch_id}.jsonl"
        if not snapshot.exists():
            return {"ok": False, "error_code": "SNAPSHOT_MISSING"}
        # restore the journal snapshot and rebuild in-memory state
        self._accounts._fh.close()
        shutil.copy2(snapshot, self._accounts._journal_path)
        self._accounts._accounts.clear()
        self._accounts._holdings.clear()
        self._accounts._cash.clear()
        self._accounts._transactions.clear()
        self._accounts._dividends.clear()
        self._accounts._replay()
        self._accounts._fh = open(
            self._accounts._journal_path, "a", encoding="utf-8")
        batch["status"] = "REVERTED"
        batch["reverted_at"] = time.time()
        self._persist()
        return {"ok": True, "batch_id": batch_id, "status": "REVERTED"}

    def batches(self, account_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for b in self._batches.values():
            if account_id and b["account_id"] != account_id:
                continue
            out.append({k: v for k, v in b.items() if k != "rows"} |
                       {"row_count": len(b["rows"])})
        return out
