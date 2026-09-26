"""Julia compute boundary — thin governed wrapper (contract julia-compute/v1).

A610 PYTHON-WORK-TRANSFER: Julia is the single formal execution owner for
statistical/scientific compute and for optimization/simulation work
(按需分析 — on-demand, non-resident).  This module is a *thin wrapper*:
it only transports an already-authorized job document to the governed
``Standalone tools/julia-compute`` endpoint and returns the result; all
numerical work executes inside Julia.  JAX/model training is unaffected.

Fail-closed: missing Julia toolchain or a contract violation raises
``JuliaComputeError`` — the caller degrades per policy instead of
silently substituting a Python implementation.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

_logger = logging.getLogger("gptbridge.compute.julia")

JULIA_COMPUTE_CONTRACT = "julia-compute/v1"
DEFAULT_TIMEOUT_S = 120.0
MAX_TIMEOUT_S = 900.0

_OPS: frozenset[str] = frozenset(
    {
        "stats.describe",
        "stats.quantiles",
        "stats.correlation",
        "linalg.lstsq",
        "optimize.nelder_mead",
        "simulate.monte_carlo",
    }
)


class JuliaComputeError(RuntimeError):
    """Fail-closed error for the Julia compute boundary."""


def _tool_root() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "Standalone tools"
        / "julia-compute"
    )


def _entrypoint() -> Path:
    return _tool_root() / "src" / "compute.jl"


def find_julia() -> Optional[Path]:
    """Locate the governed Julia runtime.

    Resolution order: ``JULIA_EXE`` override → PATH → the standard
    per-user install root (``%LOCALAPPDATA%\\Programs\\Julia-*``) →
    ``C:\\Program Files\\Julia-*``.  Highest version wins.
    """
    override = os.environ.get("JULIA_EXE")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    import shutil

    on_path = shutil.which("julia")
    if on_path:
        return Path(on_path)
    candidates: list[Path] = []
    for root in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        Path(os.environ.get("ProgramFiles", "C:\\Program Files")),
    ):
        if not root.is_dir():
            continue
        for entry in root.glob("Julia-*/bin/julia.exe"):
            if entry.is_file():
                candidates.append(entry)
    if not candidates:
        return None

    def _version_key(path: Path) -> tuple[int, ...]:
        parts = []
        for chunk in path.parent.parent.name.split("-"):
            for piece in chunk.split("."):
                if piece.isdigit():
                    parts.append(int(piece))
        return tuple(parts)

    return max(candidates, key=_version_key)


def julia_version() -> Optional[str]:
    """Report the resolved Julia version, or None when absent."""
    exe = find_julia()
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # "julia version 1.13.0"
    for token in proc.stdout.split():
        if token[0:1].isdigit():
            return token
    return proc.stdout.strip() or None


class JuliaCompute:
    """On-demand Julia compute client (spawn-per-call, no resident state)."""

    def __init__(
        self,
        julia_exe: Optional[Path | str] = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._exe = Path(julia_exe) if julia_exe else find_julia()
        self.timeout_s = max(1.0, min(float(timeout_s), MAX_TIMEOUT_S))

    @property
    def available(self) -> bool:
        return self._exe is not None and self._exe.is_file()

    def run(
        self,
        op: str,
        params: Optional[dict[str, Any]] = None,
        *,
        request_id: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> dict[str, Any]:
        """Execute one governed compute op; return its result payload."""
        if op not in _OPS:
            raise JuliaComputeError(f"JULIA_OP_NOT_CONTRACTED:{op}")
        if not self.available:
            raise JuliaComputeError("JULIA_UNAVAILABLE")
        job: dict[str, Any] = {
            "contract": JULIA_COMPUTE_CONTRACT,
            "schema": 1,
            "op": op,
            "params": params or {},
        }
        if request_id:
            job["request_id"] = str(request_id)
        timeout = max(1.0, min(float(timeout_s or self.timeout_s), MAX_TIMEOUT_S))
        entry = _entrypoint()
        try:
            proc = subprocess.run(
                [
                    str(self._exe),
                    "--startup-file=no",
                    "--history-file=no",
                    f"--project={_tool_root()}",
                    str(entry),
                ],
                input=json.dumps(job),
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                cwd=str(_tool_root()),
            )
        except subprocess.TimeoutExpired as exc:
            raise JuliaComputeError(f"JULIA_TIMEOUT:{op}") from exc
        except OSError as exc:
            raise JuliaComputeError(f"JULIA_SPAWN_FAILED:{exc}") from exc
        line = (proc.stdout or "").strip().splitlines()
        try:
            body = json.loads(line[-1]) if line else {}
        except json.JSONDecodeError as exc:
            raise JuliaComputeError(
                f"JULIA_BAD_RESPONSE:{(proc.stderr or '').strip()[:200]}"
            ) from exc
        if body.get("contract") != JULIA_COMPUTE_CONTRACT:
            raise JuliaComputeError("JULIA_CONTRACT_MISMATCH")
        if not body.get("ok"):
            raise JuliaComputeError(str(body.get("error") or "JULIA_OP_FAILED"))
        result = body.get("result")
        return result if isinstance(result, dict) else {"value": result}

    # -- typed conveniences ------------------------------------------------

    def describe(self, values: Sequence[float]) -> dict[str, Any]:
        return self.run("stats.describe", {"values": list(values)})

    def quantiles(
        self, values: Sequence[float], qs: Iterable[float]
    ) -> list[dict[str, float]]:
        return self.run(
            "stats.quantiles",
            {"values": list(values), "qs": [float(q) for q in qs]},
        )["quantiles"]

    def correlation(
        self, x: Sequence[float], y: Sequence[float]
    ) -> dict[str, Any]:
        return self.run(
            "stats.correlation", {"x": list(x), "y": list(y)}
        )

    def least_squares(
        self, a: Sequence[Sequence[float]], b: Sequence[float]
    ) -> dict[str, Any]:
        return self.run(
            "linalg.lstsq",
            {"a": [list(map(float, r)) for r in a], "b": list(map(float, b))},
        )

    def minimize(
        self,
        objective: str,
        x0: Sequence[float],
        *,
        a: Optional[Sequence[Sequence[float]]] = None,
        b: Optional[Sequence[float]] = None,
        c: float = 0.0,
        max_iter: int = 400,
        tol: float = 1e-8,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "objective": objective,
            "x0": list(map(float, x0)),
            "max_iter": int(max_iter),
            "tol": float(tol),
        }
        if a is not None:
            params["a"] = [list(map(float, r)) for r in a]
        if b is not None:
            params["b"] = list(map(float, b))
        params["c"] = float(c)
        return self.run("optimize.nelder_mead", params)

    def monte_carlo(
        self, model: str, *, trials: int = 10_000, seed: int = 42, **params: Any
    ) -> dict[str, Any]:
        return self.run(
            "simulate.monte_carlo",
            {"model": model, "trials": int(trials), "seed": int(seed), **params},
        )


__all__ = [
    "JULIA_COMPUTE_CONTRACT",
    "JuliaCompute",
    "JuliaComputeError",
    "find_julia",
    "julia_version",
]
