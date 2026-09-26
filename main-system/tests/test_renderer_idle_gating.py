"""Idle-gating contract tests for renderer timers (UI CPU reduction).

Pins the 2026-09-27 optimisation contract on the grandfathered UI shell:

- ``RuntimeServiceManager``'s 30 s safety-net heartbeat must skip the
  ``app:get-status`` IPC round-trip entirely while the window is hidden
  (a backgrounded shell cannot display stale status anyway — push events
  still flow, the first visible tick re-fetches);
- ``AppSloDrawer``'s SLO polling must defer while hidden;
- both timers must remain bounded intervals with cleanup, never
  unbounded self-rescheduling timeouts.

These are source-contract tests: the renderer files are TypeScript and
the dev harness (``scripts/test_*.ts``) covers runtime behaviour; here we
pin the structural invariant so a refactor that drops the gate fails CI
without an Electron instance.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_UI = ROOT / "main-system" / "src-ui" / "renderer"
RSM = SRC_UI / "services" / "RuntimeServiceManager.ts"
SLO = SRC_UI / "ui" / "AppSloDrawer.tsx"

_HIDDEN_GATE = re.compile(
    r"document\.visibilityState\s*===\s*['\"]hidden['\"]"
)


def _interval_body(source: str) -> str:
    """Extract the callback body of the setInterval call."""
    match = re.search(r"setInterval\((?:async )?\(\)\s*=>\s*\{", source)
    assert match, "no arrow-callback setInterval found"
    start = match.end()
    depth = 1
    i = start
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, "unbalanced braces in setInterval callback"
    return source[start:i]


def test_heartbeat_skips_ipc_when_hidden() -> None:
    source = RSM.read_text(encoding="utf-8")
    body = _interval_body(source)
    gate = _HIDDEN_GATE.search(body)
    ipc = body.find("invoke('app:get-status'")
    assert gate, "heartbeat interval lost its hidden-window gate"
    assert ipc > 0, "heartbeat no longer invokes app:get-status"
    assert gate.start() < ipc, (
        "visibility check must precede the IPC call, not follow it"
    )


def test_heartbeat_is_bounded_and_cleaned_up() -> None:
    source = RSM.read_text(encoding="utf-8")
    assert re.search(r"setInterval\(async \(\) =>", source)
    assert "}, 30000)" in source, "heartbeat must stay a fixed 30s interval"
    assert "clearInterval(this.heartbeatTimer)" in source
    assert "removeEventListener('ipc_event'" in source


def test_slo_poll_skips_ipc_when_hidden() -> None:
    source = SLO.read_text(encoding="utf-8")
    body = _interval_body(source)
    gate = _HIDDEN_GATE.search(body)
    fetch = body.find("void fetchReport()")
    assert gate, "SLO poll interval lost its hidden-window gate"
    assert fetch > 0
    assert gate.start() < fetch, (
        "visibility check must precede the fetchReport call"
    )


def test_slo_poll_is_bounded_and_cleaned_up() -> None:
    source = SLO.read_text(encoding="utf-8")
    assert "POLL_INTERVAL_MS = 30_000" in source
    assert "window.clearInterval(timer)" in source
    # The interval must live inside the `open`-gated effect — a poll that
    # runs while the drawer is closed would be pure waste.
    assert re.search(r"\[open\]\)", source), (
        "SLO poll effect must remain scoped to drawer-open"
    )


def test_no_self_rescheduling_timeout_in_heartbeat() -> None:
    """setTimeout chains re-arm even when the work is pointless; the
    contract is bounded intervals only."""
    source = RSM.read_text(encoding="utf-8")
    heartbeat = source[source.index("startHeartbeat"):source.index("stopHeartbeat")]
    assert "setTimeout" not in heartbeat
