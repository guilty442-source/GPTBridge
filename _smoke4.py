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
    s.close()
    return f"ver={ver['value'] if ver else None} signals={n}"
run("trading-store", trading)

sys.path.insert(0, r"E:\GPTBridge\Standalone tools\investment-mobile\src\backend\services")
from investment_mobile.trading.market.history import CandleStore
def candles():
    c = CandleStore()
    c.open()
    st = c.stats()
    c.close()
    return st
run("candle-store", candles)

sys.path.insert(0, r"E:\GPTBridge\Standalone tools\file-sorter\src\backend\services")
from file_sorter.infrastructure.video_fingerprint_cache import VideoFingerprintCache
def fp():
    v = VideoFingerprintCache()
    p = Path(r"E:\GPTBridge\x.bin")
    v.put(p, size=1, mtime_ns=2, payload={"k": "v"})
    got = v.get(p, size=1, mtime_ns=2)
    return f"roundtrip={got}"
run("video-fp-cache", fp)

sys.path.insert(0, r"E:\GPTBridge\Standalone tools\vaultly\src\backend\services")
import importlib
m = importlib.import_module("vaultly.infrastructure")
def vaultly():
    repo_cls = getattr(m, "VaultlyRepository", None) or getattr(m, "Repository", None)
    names = [n for n in dir(m) if "Repos" in n or "Store" in n]
    return f"exports={names[:5]}"
run("vaultly-mod", vaultly)
