#!/usr/bin/env python
"""Standalone governed codex-amendment runner.

Replicates the codex-amendment-intake flow outside a running backend:
launcher mints the governance bootstrap, the --worker child builds a real
ToolboxService, wakes local-model through the governed path, bridges the
sync search callable onto the main loop exactly like the intake driver,
then advances staged requests (auto_execute) through the five-sovereign
gate.  Deadline rejections are terminal, so the runner mints -rN resubmits
with a freshly-read predecessor and retries while the tool stays warm.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE = ROOT / "main-system" / "src-core"
for extra in (
    CORE,
    ROOT / "main-system",
    ROOT / "governance_rule",
    ROOT / "shared-layer" / "src",
    ROOT,
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

CONVERGENCE = ROOT / "governance_rule" / "execution" / "audit" / "convergence"
SEARCHD_EXE = (
    Path(os.environ.get("LOCALAPPDATA", "."))
    / "GPTBridge" / "tools" / "searchd" / "searchd.exe"
)
SEARCHD_URL = "http://127.0.0.1:8091"
OWNER_TOOL_ID = "local-model"
CHANNEL_TOOL_ID = "xingcheng"
WEB_SEARCH_TIMEOUT_S = 30.0
MAX_ROUNDS = 6


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _issue_governance_bootstrap() -> str:
    import base64
    import secrets
    from dataclasses import asdict

    from governance_rule.execution.authentication import sign_launcher_attestation
    from governance_rule.execution.integrity import build_integrity_manifest

    launcher_key = secrets.token_bytes(32)
    issued_at = int(time.time())
    key_id = secrets.token_hex(16)
    integrity = build_integrity_manifest(
        str(ROOT), launcher_key, issued_at=issued_at, key_id=key_id
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


def _searchd_healthy(timeout: float = 1.5) -> bool:
    try:
        req = urllib.request.Request(
            f"{SEARCHD_URL}/healthz", headers={"User-Agent": "XingCheng/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def _restart_searchd() -> None:
    for proc_line in subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True
    ).stdout.splitlines():
        if ":8091" in proc_line and "LISTENING" in proc_line:
            pid = proc_line.split()[-1]
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    time.sleep(1.0)
    subprocess.Popen(
        [str(SEARCHD_EXE), "--listen", "127.0.0.1:8091", "--upstream-timeout", "3s"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    for _ in range(40):
        if _searchd_healthy():
            return
        time.sleep(0.5)


def _live_predecessor() -> dict:
    """Read live codex lineage for a resubmission predecessor block."""
    import psycopg
    from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose

    conn = psycopg.connect(
        resolve_dsn(DsnPurpose.RUNTIME).dsn,
        options="-c search_path=gptbridge_codex,pg_catalog "
        "-c default_transaction_read_only=on",
    )
    try:
        seq, version, head, epoch = conn.execute(
            "select sequence, version, entry_hash, version_epoch "
            "from revision_history order by sequence desc limit 1"
        ).fetchone()
    finally:
        conn.close()
    return {
        "codex_version": version,
        "version_identity": f"E{epoch}:{version}",
        "version_epoch": int(epoch),
        "history_head": head,
        "revision_sequence": int(seq),
    }


def _resubmit(last_path: Path, reason: str) -> Path:
    """Mint the next -rN artifact for a terminal-rejected request."""
    import re

    payload = json.loads(last_path.read_text(encoding="utf-8"))
    base = str(payload["request_id"])
    m = re.match(r"^(?P<stem>.+?)-r(?P<n>\d+)$", base)
    stem = m.group("stem") if m else base
    revs = []
    for p in CONVERGENCE.glob(f"codex-amendment-request-{stem}-r*.json"):
        rm = re.match(rf"^codex-amendment-request-{re.escape(stem)}-r(\d+)\.json$", p.name)
        if rm:
            revs.append(int(rm.group(1)))
    n = max(revs, default=0) + 1
    payload["request_id"] = f"{stem}-r{n}"
    payload["requested_by"] = "decision-sovereign"
    payload["title"] = f"[r{n}] " + str(payload["title"]).split("] ", 1)[-1]
    payload["resubmission_of"] = base
    payload["resubmission_reason"] = reason
    payload["predecessor"] = _live_predecessor()
    out = CONVERGENCE / f"codex-amendment-request-{payload['request_id']}.json"
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out


def _build_service():
    from core_system.governance_runtime import MainSystemGovernance
    from governance import PermissionSovereign
    from tasks.toolbox_service import ToolboxService

    governance = MainSystemGovernance.from_environment(ROOT)
    sovereign = PermissionSovereign(None, governance=governance)
    return ToolboxService(
        ROOT, governance=governance, permission_sovereign=sovereign
    )


async def _worker() -> int:
    from governance_rule.execution.codex_amendment_driver import advance_all
    from governance_rule.execution.codex_amendment_lifecycle import (
        CodexAmendmentRequestLedger,
    )

    loop = asyncio.get_running_loop()
    service = _build_service()
    sovereign = service.permission_sovereign

    async def search_roundtrip(query: str) -> dict:
        request_id = f"codex-audit-search-{time.time_ns()}"
        try:
            queued = await service.request_tool_execution(
                {
                    "tool_id": OWNER_TOOL_ID,
                    "request_id": request_id,
                    "_governed_command": "xingcheng_web_search",
                    "query": query,
                }
            )
        except Exception as error:
            return {"ok": False, "source": "xingcheng-web-search",
                    "query": query, "error": f"queue:{type(error).__name__}"}
        if not isinstance(queued, dict) or queued.get("ok") is not True:
            return {"ok": False, "source": "xingcheng-web-search",
                    "query": query, "error": "queue-denied"}
        deadline = time.monotonic() + WEB_SEARCH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                response = await asyncio.to_thread(
                    sovereign.tool_execution_response, CHANNEL_TOOL_ID, request_id
                )
            except Exception:
                response = None
            if isinstance(response, dict) and str(
                response.get("status") or ""
            ) in {"completed", "failed", "cancelled"}:
                body = response.get("response")
                if response.get("status") == "completed" and isinstance(body, dict):
                    body.setdefault("source", "xingcheng-web-search")
                    return body
                return {"ok": False, "source": "xingcheng-web-search",
                        "query": query,
                        "error": f"tool-{response.get('status')}"}
            await asyncio.sleep(0.5)
        return {"ok": False, "source": "xingcheng-web-search",
                "query": query, "error": "timeout"}

    def channel_search(query: str) -> dict:
        future = asyncio.run_coroutine_threadsafe(search_roundtrip(query), loop)
        try:
            return future.result(timeout=WEB_SEARCH_TIMEOUT_S + 10.0)
        except Exception as error:
            return {"ok": False, "source": "xingcheng-web-search",
                    "query": query,
                    "error": f"roundtrip:{type(error).__name__}"}

    log("ensuring searchd healthy on 8091 (NTFS image)")
    if not _searchd_healthy():
        _restart_searchd()
    log(f"searchd healthy={_searchd_healthy()}")

    log("starting local-model via governed toolbox path")
    start_result = await service.start_tool(
        {
            "tool_id": OWNER_TOOL_ID,
            "background": True,
            "request_id": f"codex-audit-wake-{time.time_ns()}",
        }
    )
    log(f"start_tool: {json.dumps(start_result)[:200]}")

    # drain + warm: submit warmup searches until one returns ok:true
    warm_ok = False
    warm_deadline = time.monotonic() + 540
    attempt = 0
    while time.monotonic() < warm_deadline:
        attempt += 1
        result = await search_roundtrip("codex-audit-warmup")
        log(f"warmup {attempt}: ok={result.get('ok')} err={result.get('error')}")
        if result.get("ok") is True:
            warm_ok = True
            break
        if not _searchd_healthy():
            log("searchd unhealthy mid-warmup; restarting")
            _restart_searchd()
        await asyncio.sleep(3)
    if not warm_ok:
        log("WARN: warmup never returned ok:true — proceeding anyway")

    ledger = CodexAmendmentRequestLedger()
    stems = [
        "language-version-pin-and-stale-counts-20260926",
        "retired-engine-registry-sync-20260926",
        "module-optimization-matrix-20260926",
        "artifact-name-timestamp-lineage-20260926",
    ]

    def _latest_artifact(stem: str) -> Path | None:
        import re

        best: tuple[int, Path] | None = None
        for p in CONVERGENCE.glob(f"codex-amendment-request-{stem}*.json"):
            rm = re.match(
                rf"^codex-amendment-request-{re.escape(stem)}-r(\d+)\.json$",
                p.name,
            )
            rev = int(rm.group(1)) if rm else -1
            if best is None or rev > best[0]:
                best = (rev, p)
        return best[1] if best else None

    def _family_done(stem: str) -> bool:
        for p in CONVERGENCE.glob(f"codex-amendment-request-{stem}*.json"):
            try:
                rid = json.loads(p.read_text(encoding="utf-8"))["request_id"]
            except Exception:
                continue
            rec = ledger.load_record(rid)
            if rec and rec.get("state") == "executed":
                return True
        return False

    for round_no in range(1, MAX_ROUNDS + 1):
        log(f"=== advance round {round_no} ===")
        if not _searchd_healthy():
            log("searchd unhealthy before round; restarting")
            _restart_searchd()
        # resubmit any family whose latest artifact is terminal-but-not-executed
        for stem in stems:
            if _family_done(stem):
                continue
            latest = _latest_artifact(stem)
            if latest is None:
                continue
            rid = json.loads(latest.read_text(encoding="utf-8"))["request_id"]
            rec = ledger.load_record(rid)
            state = str(rec.get("state") or "") if rec else ""
            if state == "rejected":
                new_path = _resubmit(latest, f"round-{round_no}: {state or 'unrecorded'}")
                log(f"  resubmitted {rid} as {new_path.stem}")
        results = await asyncio.to_thread(
            asyncio.run,
            advance_all(ledger=ledger, search=channel_search, auto_execute=True),
        )
        for r in results:
            rid = r.get("request_id")
            if any(str(rid).startswith(s) or f"request-{s}" == str(rid) for s in stems):
                log(f"  {rid}: state={r.get('state')} stage={r.get('stage')} "
                    f"err={str(r.get('error'))[:100]}")
        if all(_family_done(s) for s in stems):
            break
        await asyncio.sleep(2)

    try:
        await service.shutdown_managed_tools()
    except Exception:
        pass
    states = {}
    for stem in stems:
        for p in sorted(CONVERGENCE.glob(f"codex-amendment-request-{stem}*.json")):
            try:
                rec = ledger.load_record(
                    json.loads(p.read_text(encoding="utf-8"))["request_id"]
                )
                states[p.stem] = rec.get("state") if rec else "no-record"
            except Exception as e:
                states[p.stem] = f"err:{e}"
    log("FINAL STATES:")
    for k, v in states.items():
        log(f"  {k} => {v}")
    return 0 if all(_family_done(s) for s in stems) else 1


def main() -> int:
    if "--worker" in sys.argv:
        return asyncio.run(_worker())
    from core_system.governance_runtime import GOVERNANCE_BOOTSTRAP_ENV

    env = dict(os.environ)
    env[GOVERNANCE_BOOTSTRAP_ENV] = _issue_governance_bootstrap()
    env["GPTBRIDGE_PROJECT_ROOT"] = str(ROOT)
    env["GPTBRIDGE_WORKSPACE_ROOT"] = str(ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-B", str(Path(__file__)), "--worker"],
        cwd=str(ROOT), env=env,
    )
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
