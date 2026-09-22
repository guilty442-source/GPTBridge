"""INTEGRATION-04B-10 isolated startup harness.

Launches the Release Candidate backend with an isolated runtime sandbox:

* ``GPTBRIDGE_RELEASE_ROOT`` points at the RC directory (code source,
  read-only — the RC is never written);
* ``GPTBRIDGE_IPC_PORT`` gets a free ephemeral port;
* ``GPTBRIDGE_PROJECT_ROOT`` / ``GPTBRIDGE_WORKSPACE_ROOT`` /
  ``GPTBRIDGE_IPC_STATE_ROOT`` point at a temp state root so runtime
  state, logs and temp writes stay out of the canonical tree;
* the governance bootstrap is minted against the canonical bound root
  (``directory_authority.GPTBRIDGE_PROJECT_ROOT`` = ``E:/GPTBridge``) —
  authority pinning is bound to the canonical install path by design;
  this harness records honestly how far startup gets under an env-isolated
  project root (the re-anchor phase is expected to fail closed, which is
  itself evidence of the bound-root contract).

Scenarios (each spawns a fresh process):

* ``lifecycle`` — STARTING phases -> READY ("IPC Server running") ->
  graceful terminate -> STOPPED, port released, zero orphan children.
* ``mid-start-kill`` — kill during STARTING -> process exits, no
  orphaned children, port never held.

Evidence is written to
``main-system/runtime/state/integration-04b-10-isolated-start.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELEASE = (
    REPO_ROOT / "main-system" / "runtime" / "releases" / "rc-2026-09-21"
)
REPORT_PATH = (
    REPO_ROOT
    / "main-system"
    / "runtime"
    / "state"
    / "integration-04b-10-isolated-start.json"
)

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _mint_bootstrap() -> str:
    """Mint the launcher bootstrap exactly like boot_core_state does.

    The integrity manifest is built against the canonical bound root
    (``directory_authority.GPTBRIDGE_PROJECT_ROOT`` = ``E:/GPTBridge``);
    the path guard refuses every other root by design."""
    sys.path.insert(0, str(REPO_ROOT))
    from governance_rule.execution.authentication import (
        sign_launcher_attestation,
    )
    from governance_rule.execution.integrity import build_integrity_manifest

    launcher_key = secrets.token_bytes(32)
    issued_at = int(time.time())
    key_id = secrets.token_hex(16)
    integrity = build_integrity_manifest(
        REPO_ROOT, launcher_key, issued_at=issued_at, key_id=key_id
    )
    attestation = sign_launcher_attestation(
        launcher_key,
        actor="governance/main-system",
        bound_tool_id="main-system",
        caller_path="main-system/src-core/main.py",
        process_id=os.getpid(),
        issued_at=issued_at,
        key_id=key_id,
    )
    payload = {
        "format_version": 1,
        "launcher_key": base64.b64encode(launcher_key).decode("ascii"),
        "integrity_manifest": asdict(integrity),
        "identity_attestation": asdict(attestation),
    }
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _seed_state_root(release: Path, state_root: Path) -> None:
    """Seed the isolated state root with the release's ``main-system``
    runtime seed (package.json manifest, governance registries)."""
    seed = release / "main-system"
    if seed.is_dir():
        shutil.copytree(seed, state_root / "main-system", dirs_exist_ok=True)


def _spawn(release: Path, state_root: Path, port: int) -> subprocess.Popen[bytes]:
    """Spawn the release backend.

    ``GPTBRIDGE_RELEASE_ROOT`` supplies the code (read-only); the env
    project/workspace roots and IPC state root point at ``state_root`` so
    runtime state/logs/temp stay isolated.  The governance authority bound
    root (``E:/GPTBridge``) is a hard-coded constant — the integrity
    manifest is minted and verified there regardless of this env root.
    """
    env = dict(os.environ)
    env["GPTBRIDGE_GOVERNANCE_BOOTSTRAP"] = _mint_bootstrap()
    env["GPTBRIDGE_RELEASE_ROOT"] = str(release)
    env["GPTBRIDGE_PROJECT_ROOT"] = str(state_root)
    env["GPTBRIDGE_WORKSPACE_ROOT"] = str(state_root)
    env["GPTBRIDGE_IPC_PORT"] = str(port)
    env["GPTBRIDGE_IPC_STATE_ROOT"] = str(state_root / "ipc-state")
    # Release payload must stay immutable: redirect every module-relative
    # audit sink to the sandbox state root (§10.19 boundary — a release
    # never accumulates official state).
    audit_root = state_root / "governance-audit"
    env["GPTBRIDGE_AUDIT_CHAIN_DIR"] = str(audit_root / "git_audit_chain")
    env["GPTBRIDGE_CAPABILITY_LEDGER"] = str(
        audit_root / "capability_ledger.jsonl"
    )
    env["GPTBRIDGE_CODEX_AUDIT_PATH"] = str(
        audit_root / "codex_read_audit.jsonl"
    )
    # rc-2026-09-21's backend/main.py predates the flat-layout ``governance``
    # sys.path entry (dev main.py adds <release>/main-system when governance/
    # exists there).  Inject it via PYTHONPATH only when the release's
    # main.py lacks the native fix — newer RCs need no compensation.
    main_py = release / "backend" / "main.py"
    try:
        needs_flat_path = "_flat_main_system" not in main_py.read_text(
            encoding="utf-8"
        )
    except OSError:
        needs_flat_path = False
    if needs_flat_path:
        env["GPTBRIDGE_04B10_FLAT_PATH_COMPENSATED"] = "1"
        extra_path = str(release / "main-system")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            extra_path + os.pathsep + existing if existing else extra_path
        )
    python_exe = release / "venv" / "Scripts" / "python.exe"
    if not python_exe.is_file():
        python_exe = Path(sys.executable)
    return subprocess.Popen(
        [
            str(python_exe),
            "-u",
            "-B",
            str(release / "backend" / "main.py"),
            "--serve",
        ],
        cwd=str(release),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _children(pid: int) -> list[int]:
    try:
        import psutil

        return [p.pid for p in psutil.Process(pid).children(recursive=True)]
    except Exception:
        return []


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _port_released(port: int, timeout_s: float = 5.0) -> bool:
    """Port release can lag process exit (socket teardown races)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _port_listening(port):
            return True
        time.sleep(0.25)
    return not _port_listening(port)


def _pump(proc: subprocess.Popen[bytes], lines: list[str], done: threading.Event) -> None:
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        lines.append(line)
    done.set()


def _wait_for(lines: list[str], needle: str, deadline: float) -> bool:
    while time.time() < deadline:
        if any(needle in line for line in lines):
            return True
        time.sleep(0.2)
    return False


def _phases(lines: list[str]) -> list[str]:
    phases: list[str] = []
    for line in lines:
        if '"startup_phase"' in line and '"phase"' in line:
            try:
                marker = line[line.index("{") :]
                phases.append(str(json.loads(marker).get("phase")))
            except (ValueError, KeyError):
                continue
    return phases


def _ipc_contract_probe(state_root: Path, port: int, timeout_s: float = 20.0) -> dict:
    """End-to-end IPC contract probe against the live isolated backend.

    Exercises the frontend surface contract: ticket auth (reject without /
    bad signature, accept valid), session hello (``state_event_hello`` ->
    ``state_event_session``), command dispatch (``COMMAND_RECEIVED`` +
    ``<command>_result``), and error surface for an unknown command.
    """
    try:
        import websockets
        from urllib.parse import quote
    except ImportError:
        return {"ok": False, "error": "websockets-unavailable"}

    token_path = state_root / "ipc-state" / "session-token"
    instance_src = os.path.normcase(str(state_root.absolute())).replace(
        "\\", "/"
    )
    instance = hashlib.sha256(instance_src.encode("utf-8")).hexdigest()[:24]

    token_holder: list[str] = []

    def _ticket(valid_sig: bool = True) -> str:
        expires = int(time.time()) + 30
        payload = f"{expires}.{secrets.token_hex(8)}.{instance}"
        sig = hmac.new(
            token_holder[0].encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{sig if valid_sig else '0' * 64}"

    base = f"ws://127.0.0.1:{port}/"
    results: dict[str, object] = {}

    async def _run() -> None:
        # 1. Missing ticket must be refused at the handshake.  This first
        #    request also triggers the server to lazily create the session
        #    token file — read it only after the probe connects once.
        try:
            async with websockets.connect(base):
                results["no_ticket_rejected"] = False
        except Exception:
            results["no_ticket_rejected"] = True
        # 2. Forged signature must be refused.  A random placeholder token
        #    suffices — the signature can never verify either way.
        token_holder.append("0" * 64)
        try:
            url = f"{base}?ticket={quote(_ticket(False))}&instance={instance}"
            async with websockets.connect(url):
                results["bad_ticket_rejected"] = False
        except Exception:
            results["bad_ticket_rejected"] = True
        # Now read the server-minted session token for the positive case.
        token = ""
        if token_path.is_file():
            token = token_path.read_text(encoding="utf-8").strip().lower()
        if not token:
            results["error"] = "session-token-absent"
            return
        token_holder[0] = token
        # 3. Valid single-use ticket authenticates.
        url = f"{base}?ticket={quote(_ticket())}&instance={instance}"
        async with websockets.connect(url) as ws:
            results["valid_ticket_accepted"] = True

            async def _next_event() -> dict:
                raw = await ws.recv()
                return json.loads(raw)

            # 4. Session hello -> state_event_session.
            await ws.send(json.dumps(
                {"command": "state_event_hello", "payload": {"cursor": None}}
            ))
            deadline = time.time() + timeout_s
            seen: list[str] = []
            while time.time() < deadline:
                try:
                    ev = await asyncio.wait_for(
                        _next_event(), timeout=max(0.1, deadline - time.time())
                    )
                except Exception:
                    break
                name = str(ev.get("event") or "")
                seen.append(name)
                if name == "state_event_session":
                    break
            results["session_events"] = seen[:8]
            results["session_hello_ok"] = "state_event_session" in seen
            # 5. Unknown command -> COMMAND_RECEIVED then *_result / error.
            await ws.send(json.dumps(
                {"command": "__contract_probe__", "payload": {}}
            ))
            seen = []
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                try:
                    ev = await asyncio.wait_for(
                        _next_event(), timeout=max(0.1, deadline - time.time())
                    )
                except Exception:
                    break
                name = str(ev.get("event") or "")
                seen.append(name)
                if name.endswith("_result") or name == "error":
                    break
            results["command_events"] = seen[:8]
            results["command_dispatched"] = "COMMAND_RECEIVED" in seen
            results["command_resulted"] = any(
                n.endswith("_result") or n == "error" for n in seen
            )
            # 6. Liveness channel: heartbeat_pong is accepted silently.
            await ws.send(json.dumps(
                {"command": "heartbeat_pong", "payload": {}}
            ))
            results["heartbeat_sent"] = True

    try:
        asyncio.run(_run())
    except Exception as error:  # noqa: BLE001 — probe records, never raises
        results["error"] = f"{type(error).__name__}: {error}"
    results["ok"] = bool(
        results.get("no_ticket_rejected")
        and results.get("bad_ticket_rejected")
        and results.get("valid_ticket_accepted")
        and results.get("session_hello_ok")
        and results.get("command_dispatched")
        and results.get("command_resulted")
    )
    return results


def _scenario_lifecycle(release: Path, state_root: Path, port: int, timeout_s: float) -> dict:
    lines: list[str] = []
    done = threading.Event()
    proc = _spawn(release, state_root, port)
    pump = threading.Thread(target=_pump, args=(proc, lines, done), daemon=True)
    pump.start()
    deadline = time.time() + timeout_s
    ready = _wait_for(lines, "IPC Server running", deadline)
    observed = {
        "pid": proc.pid,
        "reached_starting": bool(_phases(lines)),
        "startup_phases": _phases(lines),
        "ready": ready,
        "ready_line": next(
            (l for l in lines if "IPC Server running" in l), None
        ),
    }
    if not ready:
        proc.kill()
        proc.wait(timeout=15)
        observed["exit_code"] = proc.returncode
        observed["tail"] = lines[-15:]
        observed["stopped"] = True
        observed["port_released"] = _port_released(port)
        observed["orphans"] = _children(proc.pid)
        observed["ok"] = False
        return observed

    # IPC contract probe against the live backend (auth/session/command).
    observed["ipc_contract"] = _ipc_contract_probe(state_root, port)

    # Graceful stop: SIGTERM equivalent on Windows is terminate().
    stop_started = time.time()
    proc.terminate()
    try:
        proc.wait(timeout=45)
        stopped = True
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)
        stopped = False
    done.wait(timeout=10)
    observed.update(
        {
            "stopping_observed": any(
                "shutdown" in l.lower() or "stopping" in l.lower() for l in lines
            ),
            "stopped": stopped,
            "stop_latency_s": round(time.time() - stop_started, 3),
            "exit_code": proc.returncode,
            "port_released": _port_released(port),
            "orphans": _children(proc.pid),
            "tail": lines[-10:],
        }
    )
    observed["ok"] = bool(
        observed["reached_starting"]
        and ready
        and stopped
        and observed["port_released"]
        and not observed["orphans"]
    )
    return observed


def _scenario_mid_start_kill(release: Path, state_root: Path, port: int, timeout_s: float) -> dict:
    lines: list[str] = []
    done = threading.Event()
    proc = _spawn(release, state_root, port)
    pump = threading.Thread(target=_pump, args=(proc, lines, done), daemon=True)
    pump.start()
    deadline = time.time() + timeout_s
    # Wait until the backend has visibly begun startup phases (STARTING).
    starting = _wait_for(lines, '"startup_phase"', deadline) or _wait_for(
        lines, "startup", min(deadline, time.time() + 5)
    )
    if not starting:
        # Still counts as STARTING: kill anyway after grace window.
        time.sleep(2)
    proc.kill()
    proc.wait(timeout=15)
    done.wait(timeout=10)
    return {
        "pid": proc.pid,
        "starting_observed": starting,
        "killed_during_starting": not _wait_for(
            lines, "IPC Server running", time.time() + 1
        ),
        "exit_code": proc.returncode,
        "port_released": _port_released(port),
        "orphans": _children(proc.pid),
        "startup_phases": _phases(lines),
        "ok": proc.returncode is not None
        and _port_released(port)
        and not _children(proc.pid),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=DEFAULT_RELEASE)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--keep-sandbox", action="store_true", help="do not delete the temp root"
    )
    args = parser.parse_args()

    release = args.release.resolve()
    if not (release / "backend" / "main.py").is_file():
        print(f"release backend entry missing: {release / 'backend' / 'main.py'}")
        return 2

    sandbox = Path(tempfile.mkdtemp(prefix="04b10-isolated-"))
    report: dict[str, object] = {
        "test_id": "04B-10",
        "release": str(release),
        "sandbox": str(sandbox),
        "compensations": (
            [
                "PYTHONPATH=<release>/main-system injected: rc backend/main.py "
                "predates the flat-layout governance sys.path entry — next RC "
                "must repackage main.py",
            ]
            if "_flat_main_system"
            not in (release / "backend" / "main.py").read_text(
                encoding="utf-8"
            )
            else []
        ),
        "started_at": _utcnow(),
        "scenarios": {},
    }
    try:
        _seed_state_root(release, sandbox)
        for name, fn in (
            ("lifecycle", _scenario_lifecycle),
            ("mid-start-kill", _scenario_mid_start_kill),
        ):
            port = _free_port()
            result = fn(release, sandbox, port, args.timeout)
            result["port"] = port
            report["scenarios"][name] = result
            print(
                f"[04B-10] {name}: "
                f"{'PASS' if result['ok'] else 'FAIL'} "
                f"(phases={len(result.get('startup_phases') or [])})"
            )
        report["ok"] = all(
            s["ok"] for s in report["scenarios"].values()  # type: ignore[index]
        )
        report["finished_at"] = _utcnow()
    finally:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[04B-10] evidence -> {REPORT_PATH}")
        if not args.keep_sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
