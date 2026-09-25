"""Julia compute boundary tests (A610 PYTHON-WORK-TRANSFER).

The transport (subprocess) is stubbed for contract tests; a live Julia
round-trip runs only when a real toolchain is resolvable.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core_system.julia_compute import (
    JuliaCompute,
    JuliaComputeError,
    find_julia,
)


def _client(tmp_path: Path) -> JuliaCompute:
    fake = tmp_path / "julia.exe"
    fake.write_text("stub")
    return JuliaCompute(fake)


def test_uncontracted_op_rejected(tmp_path) -> None:
    client = _client(tmp_path)
    with pytest.raises(JuliaComputeError, match="JULIA_OP_NOT_CONTRACTED"):
        client.run("eval.arbitrary", {"code": "1+1"})


def test_missing_julia_fails_closed(tmp_path) -> None:
    client = JuliaCompute(tmp_path / "absent" / "julia.exe")
    assert client.available is False
    with pytest.raises(JuliaComputeError, match="JULIA_UNAVAILABLE"):
        client.describe([1.0, 2.0])


def test_request_shape_and_result(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {"ok": True, "contract": "julia-compute/v1",
             "op": "stats.describe", "result": {"n": 3, "mean": 2.0}}
        )

    def _run(argv, **kwargs):
        captured["argv"] = argv
        captured["job"] = json.loads(kwargs["input"])
        return _Proc()

    monkeypatch.setattr("core_system.julia_compute.subprocess.run", _run)
    client = _client(tmp_path)
    out = client.describe([1.0, 2.0, 3.0])
    assert out["mean"] == 2.0
    assert captured["job"]["contract"] == "julia-compute/v1"
    assert captured["job"]["op"] == "stats.describe"
    assert captured["job"]["params"]["values"] == [1.0, 2.0, 3.0]
    argv = captured["argv"]
    assert argv[1] == "--startup-file=no"
    assert any("--project=" in a for a in argv)


def test_error_result_raises(monkeypatch, tmp_path) -> None:
    class _Proc:
        returncode = 2
        stderr = ""
        stdout = json.dumps(
            {"ok": False, "contract": "julia-compute/v1",
             "error": "STATS_VALUES_EMPTY"}
        )

    monkeypatch.setattr(
        "core_system.julia_compute.subprocess.run", lambda *a, **k: _Proc()
    )
    with pytest.raises(JuliaComputeError, match="STATS_VALUES_EMPTY"):
        _client(tmp_path).describe([])


def test_bad_response_raises(monkeypatch, tmp_path) -> None:
    class _Proc:
        returncode = 0
        stderr = "boom"
        stdout = "not-json"

    monkeypatch.setattr(
        "core_system.julia_compute.subprocess.run", lambda *a, **k: _Proc()
    )
    with pytest.raises(JuliaComputeError, match="JULIA_BAD_RESPONSE"):
        _client(tmp_path).describe([1.0])


def test_timeout_raises(monkeypatch, tmp_path) -> None:
    import subprocess as sp

    def _run(*a, **k):
        raise sp.TimeoutExpired(cmd="julia", timeout=1)

    monkeypatch.setattr("core_system.julia_compute.subprocess.run", _run)
    with pytest.raises(JuliaComputeError, match="JULIA_TIMEOUT"):
        _client(tmp_path).describe([1.0])


def test_live_julia_roundtrip() -> None:
    exe = find_julia()
    if exe is None:
        pytest.skip("julia toolchain not installed")
    client = JuliaCompute(exe)
    desc = client.describe([1.0, 2.0, 3.0, 4.0])
    assert desc["n"] == 4
    assert desc["mean"] == pytest.approx(2.5)
    fit = client.least_squares(
        a=[[1.0, 0.0], [1.0, 1.0], [1.0, 2.0], [1.0, 3.0]],
        b=[1.0, 2.0, 2.0, 4.0],
    )
    assert fit["x"][0] == pytest.approx(0.9, abs=0.05)
    sim = client.monte_carlo(
        "normal", trials=5000, seed=1, mu=2.0, sigma=0.5
    )
    assert sim["mean"] == pytest.approx(2.0, abs=0.05)
