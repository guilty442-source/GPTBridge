import os, sys
sys.path.insert(0, r"E:\GPTBridge\main-system\src-core")
os.environ["VECTOR_BACKEND"] = "rust"
os.environ["VECTOR_URL"] = "http://127.0.0.1:8092"

from core_system.rag.rust_vector_runtime import RustVectorRuntime

rt = RustVectorRuntime(dimension=8)
rt.ensure_collection("smoke_col", dimension=8)
pts = [
    {"id": "a", "vector": [1.0] + [0.0]*7, "payload": {"module_id": "main-system", "resource_id": "r1"}},
    {"id": "b", "vector": [0.9, 0.1] + [0.0]*6, "payload": {"module_id": "other", "resource_id": "r2"}},
]
rt.upsert_points("smoke_col", pts)
hits = rt.search("smoke_col", [1.0]+[0.0]*7, top_k=5, module_id="main-system")
print("scoped hits:", [(h["id"], round(h["score"], 3)) for h in hits])
hits_all = rt.search("smoke_col", [1.0]+[0.0]*7, top_k=5)
print("all hits:", [(h["id"], round(h["score"], 3)) for h in hits_all])
rt.set_alias("smoke_alias", "smoke_col")
print("alias:", rt.get_alias("smoke_alias"))
print("count:", rt.count_points("smoke_col"))
rt.delete_resource("smoke_col", "r1")
print("after delete:", [(h["id"]) for h in rt.search("smoke_col", [1.0]+[0.0]*7, top_k=5)])
try:
    rt.upsert_points("smoke_col", [{"id": "bad", "vector": [1.0, 2.0], "payload": {"module_id": "m"}}])
    print("FAIL: dimension mismatch accepted")
except Exception as e:
    print("dim mismatch rejected:", str(e)[:80])
rt.snapshot()
print("snapshot ok")
print("collections:", rt.list_collections())
rt.delete_collection("smoke_col")
print("after col delete:", rt.list_collections())
print("SMOKE_OK")
