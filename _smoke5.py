import io, sys, traceback
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")
from pathlib import Path

def run(label, fn):
    try:
        print(f"{label}: OK {fn()}")
    except Exception:
        print(f"{label}: FAIL")
        traceback.print_exc(limit=4)

sys.path.insert(0, r"E:\GPTBridge\Standalone tools\ai-assistant\src\backend\services")
from ai_nexus.infrastructure.trading_store import TradingStore
def trading():
    s = TradingStore(Path(r"E:\GPTBridge\Standalone tools\ai-assistant"))
    s.open()
    ver = s._db().execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    n = s._db().execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
    # composite-PK OR REPLACE roundtrip
    s._db().execute("INSERT OR REPLACE INTO market_candles(instrument_id, timeframe,"
        " candle_start, adjustment_type, market, open, high, low, close, volume, source_id, payload)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("T1","1d","2026-01-01","raw","tw","1","1","1","1","0","x","{}"))
    s._db().commit()
    c2 = s._db().execute("SELECT COUNT(*) c FROM market_candles").fetchone()["c"]
    s.close()
    return f"ver={ver['value'] if ver else None} signals={n} candles={c2}"
run("trading-store", trading)

sys.path.insert(0, r"E:\GPTBridge\Standalone tools\vaultly\src\backend\services")
from vaultly.infrastructure.repository import VaultlyRepository
def vaultly():
    r = VaultlyRepository(Path(r"E:\GPTBridge\Standalone tools\vaultly"))
    settings = r.list_accounts() if hasattr(r, "list_accounts") else None
    return f"db={r.db_path} accounts={len(settings) if settings is not None else 'n/a'}"
run("vaultly-repo", vaultly)
