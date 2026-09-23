"""G29 batch>1 production path — C++ generate_batch through the governed
router. Skips gracefully when the checkpoint/bundle is unavailable."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

pytestmark = pytest.mark.skipif(
    os.environ.get("XINGCHENG_CPP_RUNTIME") != "required",
    reason="requires XINGCHENG_CPP_RUNTIME=required",
)


@pytest.fixture(scope="module")
def router():
    from xingcheng.infrastructure import native_engine

    if not native_engine.flag_enabled():
        pytest.skip("native engine flag off")
    return native_engine


def test_batch_generate_contract(router):
    r = router.generate_via_native_engine(
        {"prompts": ["Hello", "1+1=?", "test"], "max_tokens": 4}
    )
    assert r.get("ok") is True, r
    assert r.get("batch_size") == 3
    results = r.get("results") or []
    assert len(results) == 3
    for item in results:
        assert item.get("ok") is True
        assert item.get("cpp_runtime") is True
        assert item.get("eval_count") == 4
        assert isinstance(item.get("text"), str)


def test_batch_deterministic(router):
    req = {"prompts": ["alpha", "beta"], "max_tokens": 6, "seed": 7}
    a = router.generate_via_native_engine(req)
    b = router.generate_via_native_engine(req)
    assert a.get("ok") and b.get("ok")
    assert [r["text"] for r in a["results"]] == [
        r["text"] for r in b["results"]
    ]


def test_batch_fail_closed(router):
    empty = router.generate_via_native_engine({"prompts": []})
    assert empty.get("ok") is False
    assert empty.get("error_code") in (
        "CPP_RUNTIME_BATCH_EMPTY",
        "NATIVE_ENGINE_BATCH_EMPTY",
    )
    oversize = router.generate_via_native_engine(
        {"prompts": ["x"] * 17}
    )
    assert oversize.get("ok") is False
    assert oversize.get("error_code") in (
        "CPP_RUNTIME_BATCH_TOO_LARGE",
        "NATIVE_ENGINE_BATCH_TOO_LARGE",
    )


def test_single_path_unchanged(router):
    r = router.generate_via_native_engine(
        {"prompt": "Hello", "max_tokens": 3}
    )
    assert r.get("ok") is True
    assert r.get("cpp_runtime") is True
    assert isinstance(r.get("text"), str)
