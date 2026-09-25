import sys, io, json, glob, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FILES = [
    r"main-system\runtime\state\codex-amendment-request-js-native-20260925.json.superseded",
    r"main-system\runtime\state\codex-amendment-request-nodejs-replace-20260925.json.superseded",
    r"main-system\runtime\state\codex-amendment-request-test-tools-20260925.json.superseded",
    r"main-system\runtime\state\codex-amendment-request-rust-tests-20260925.json.superseded",
    r"main-system\runtime\state\codex-amendment-request-jax-parity-20260925.json.superseded",
    r"main-system\runtime\state\codex-amendment-request-final-architecture-20260925.json.superseded",
    r"main-system\runtime\state\codex-amendment-request-data-architecture-rust-vector-qdrant-sqlite-retire-20260925.json",
    r"main-system\runtime\state\codex-amendment-request-language-division-gap-closure-20260925-r2.json",
]
for f in FILES:
    if not os.path.exists(f):
        print(f"### MISSING {f}"); continue
    req = json.load(open(f, encoding="utf-8"))
    print(f"\n##### {os.path.basename(f)}  id={req.get('request_id')} class={req.get('change_class')}")
    for ch in req.get("changes", []):
        print("  CHG keys:", list(ch.keys()))
        for op in ch.get("operations", ch.get("ops", [])):
            tbl = op.get("table")
            act = op.get("action")
            row = op.get("row") or op.get("values") or {}
            print(f"   op {act} {tbl} row_keys={list(row.keys()) if isinstance(row,dict) else row}")
            if tbl == "articles" and isinstance(row, dict):
                print("    ARTICLE:", json.dumps(row, ensure_ascii=False)[:1600])
