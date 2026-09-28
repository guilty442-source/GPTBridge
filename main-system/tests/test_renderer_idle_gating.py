"""Idle-gating contract tests for renderer timers (UI CPU reduction).

Pins the 2026-09-27 optimisation contract on the grandfathered UI shell:

- ``RuntimeServiceManager``'s 30 s safety-net heartbeat must skip the
  ``app:get-status`` IPC round-trip entirely while the window is hidden
  (a backgrounded shell cannot display stale status anyway — push events
  still flow, the first visible tick re-fetches);
- ``AppSloDrawer``'s SLO polling must defer while hidden;
- both timers must remain bounded intervals with cleanup, never
  unbounded self-rescheduling timeouts.

These are source-contract tests: the renderer files are JavaScript-ESM and
the dev harness covers runtime behaviour; here we pin the structural
invariant so a refactor that drops the gate fails CI without a desktop
shell instance.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_UI = ROOT / "main-system" / "src-ui" / "renderer"
RSM = SRC_UI / "services" / "RuntimeServiceManager.js"
SLO = SRC_UI / "ui" / "AppSloDrawer.jsx"

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
    ipc = body.find("app:get-status")
    assert gate, "heartbeat interval lost its hidden-window gate"
    assert ipc > 0, "heartbeat no longer invokes app:get-status"
    assert gate.start() < ipc, (
        "visibility check must precede the IPC call, not follow it"
    )


def test_heartbeat_is_bounded_and_cleaned_up() -> None:
    source = RSM.read_text(encoding="utf-8")
    assert re.search(r"setInterval\(async \(\) =>", source)
    assert "}, 30000)" in source or "}, 30_000)" in source or "}, 3e4)" in source, (
        "heartbeat must stay a fixed 30s interval"
    )
    assert "clearInterval(this.heartbeatTimer)" in source
    assert 'removeEventListener("ipc_event"' in source or "removeEventListener('ipc_event'" in source


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
    assert "POLL_INTERVAL_MS = 30_000" in source or "POLL_INTERVAL_MS = 3e4" in source
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


def _renderer_interval_sites():
    """(path, lineno) for every setInterval call in the renderer tree."""
    for path in sorted(SRC_UI.rglob("*.js")) + sorted(SRC_UI.rglob("*.jsx")):
        if "node_modules" in path.parts or "dist" in path.parts:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if "setInterval(" in line and "typeof setInterval" not in line:
                yield path, lines, lineno


def test_all_renderer_intervals_gated_or_marked() -> None:
    """Same rule the delegated audit enforces: every renderer interval
    either carries a hidden-window gate in its callback or an explicit
    ``idle-ok`` exemption marker."""
    bad = []
    for path, lines, lineno in _renderer_interval_sites():
        body_src = "\n".join(lines)
        gated = False
        # Find the arrow-callback setInterval whose call sits on this line.
        for match in re.finditer(
            r"setInterval\((?:async )?\(\)\s*=>\s*\{", body_src
        ):
            if body_src.count("\n", 0, match.start()) + 1 != lineno:
                continue
            start = match.end()
            depth, i = 1, start
            while i < len(body_src) and depth:
                if body_src[i] == "{":
                    depth += 1
                elif body_src[i] == "}":
                    depth -= 1
                i += 1
            gated = bool(_HIDDEN_GATE.search(body_src[start:i]))
            break
        marked = any(
            "idle-ok" in lines[i]
            for i in range(max(0, lineno - 4), lineno)
        )
        if not gated and not marked:
            bad.append(f"{path.name}:{lineno}")
    assert not bad, f"ungated renderer intervals: {bad}"


def test_idle_ok_exemptions_documented() -> None:
    """The intentionally-ungated timers must keep their exemption markers
    so the audit exemption stays auditable."""
    for rel, reason in (
        (
            "shared/hooks/useBackendSocket.js",
            "stale sampler is a local O(1) check, no IPC",
        ),
        (
            "shared/services/hmrService.js",
            "recovery-pending is already the idle gate",
        ),
    ):
        src = (SRC_UI / rel).read_text(encoding="utf-8")
        assert "idle-ok" in src, f"{rel} lost its idle-ok marker ({reason})"
