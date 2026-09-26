import sys
sys.path.insert(0, ".")
sys.path.insert(0, "shared-layer/src")
from shared_layer.security import credential_store
for purpose in ("runtime", "reader", "admin"):
    target = credential_store.DSN_TARGET_TEMPLATE.format(purpose=purpose)
    try:
        v = credential_store.read_secret(target)
        print(purpose, "->", (v or "")[:80])
    except Exception as e:
        print(purpose, "-> ERR", e)
