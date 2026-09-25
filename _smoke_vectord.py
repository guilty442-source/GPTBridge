import asyncio, os, sys
sys.path.insert(0, r"E:\GPTBridge\main-system\src-core")
sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")

from core_system.rag.rag_qdrant import RagPipelineConfig
from core_system.rag.vector_models import PointStruct
from core_system.rag.rust_vector_runtime import RustVectorRuntime


async def main() -> None:
    cfg = RagPipelineConfig(
        qdrant_url="",
        qdrant_api_key=None,
        collection_name="smoke_col",
        postgresql_dsn="",
        embedding_dimension=8,
        vectord_url="http://127.0.0.1:8092",
    )
    rt = RustVectorRuntime(cfg)
    ok = await rt.initialize()
    print("initialize:", ok, "err:", rt.last_error)
    assert ok

    assert await rt.ensure_collection()
    points = [
        PointStruct(id="a", vector=[1.0] + [0.0] * 7,
                    payload={"module_id": "main-system", "resource_id": "r1"}),
        PointStruct(id="b", vector=[0.9, 0.1] + [0.0] * 6,
                    payload={"module_id": "other", "resource_id": "r2"}),
    ]
    assert await rt.upsert_points(points)

    hits = await rt.search([1.0] + [0.0] * 7, module_id="main-system")
    print("scoped hits:", [(h["id"], round(h["score"], 3)) for h in hits])
    scoped_ids = [h["id"] for h in hits]
    assert "a" in scoped_ids and "b" not in scoped_ids

    hits_all = await rt.search([1.0] + [0.0] * 7,
                               module_ids=("main-system", "other"))
    print("two-scope hits:", [(h["id"], round(h["score"], 3)) for h in hits_all])
    assert "b" in [h["id"] for h in hits_all]

    # scope is fail-closed: empty scope must raise
    try:
        await rt.search([1.0] * 8)
        print("FAIL: unscoped search accepted")
    except Exception as e:
        print("unscoped search rejected:", type(e).__name__, str(e)[:60])

    assert await rt.create_alias("smoke_alias", "smoke_col")
    print("alias target:", await rt.get_alias_target("smoke_alias"))

    print("points_count:", rt.points_count())
    print("resource count:", rt.count_resource_points("main-system", "r1"))

    assert await rt.delete_resource("main-system", "r1")
    print("after delete:",
          [(h["id"]) for h in await rt.search([1.0] + [0.0] * 7,
                                             module_ids=("main-system", "other"))])

    # dimension mismatch must fail closed (upsert returns False, engine rejects)
    ok_bad = await rt.upsert_points([PointStruct(
        id="bad", vector=[1.0, 2.0], payload={"module_id": "main-system"})])
    assert ok_bad is False, "dimension mismatch was accepted"
    print("dim mismatch rejected (upsert=False)")

    # payload sanitizer: forbidden fields stripped
    assert await rt.upsert_points([PointStruct(
        id="c", vector=[0.0, 1.0] + [0.0] * 6,
        payload={"module_id": "main-system", "resource_id": "r3",
                 "content": "SECRET-TEXT", "windows_path": "C:\\x"})])
    hits = await rt.search([0.0, 1.0] + [0.0] * 6, module_id="main-system")
    payloads = [h["payload"] for h in hits]
    print("sanitized payload:", payloads)
    assert all("content" not in p and "windows_path" not in p for p in payloads)

    # snapshot + restart persistence is exercised by the daemon itself;
    # here just confirm the client survives a fresh initialize
    rt2 = RustVectorRuntime(cfg)
    assert await rt2.initialize()
    print("re-init hits:", [(h["id"]) for h in await rt2.search(
        [0.0, 1.0] + [0.0] * 6, module_id="main-system")])

    print("SMOKE_OK")


asyncio.run(main())
