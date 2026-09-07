from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

# Lifecycle states from the startup contract.
STATE_READY = "READY"
STATE_DEGRADED = "DEGRADED"
STATE_FAILED = "FAILED"
STATE_RECOVERING = "RECOVERING"

# Exit codes consumed by launcher/scripts/start.ps1
EXIT_ALL_CRITICAL_UP = 0  # READY or DEGRADED; launch may continue
EXIT_CRITICAL_FAILED = 2  # local sqlite unavailable; do not pretend READY

OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
WARM_MODEL = "gemma4:e2b-it-qat"

# Hybrid startup: the critical launch path is only the local sqlite store. The
# local semantic vector index and Ollama are independent degradable services
# that may be probed concurrently, and the warm-model preload is a non-blocking
# background task.
DEGRADABLE_WORKERS = 2
WARM_MODEL_TIMEOUT = float(os.environ.get("GPTBRIDGE_WARM_MODEL_TIMEOUT", "60"))


def retry(operation: Callable[[], Any], attempts: int = 6) -> tuple[bool, str]:
    delay = 0.25
    last_error = "unavailable"
    for _ in range(attempts):
        try:
            operation()
            return True, "ready"
        except Exception as error:  # startup boundary records degraded services
            last_error = str(error)
            time.sleep(delay)
            delay = min(delay * 2, 4.0)
    return False, last_error


def get_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Connection": "keep-alive"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("INVALID_LOCAL_SERVICE_RESPONSE")
    return value


def post_json(url: str, payload: dict[str, Any], timeout: float = 120.0) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Connection": "keep-alive"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def start_hidden(command: list[str], working_directory: Path | None = None) -> None:
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        command,
        cwd=str(working_directory) if working_directory else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def service_entry(
    component: str,
    ready: bool,
    detail: str,
    *,
    critical: bool,
    state: str,
    fault_code: str,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "component": component,
        "critical": critical,
        "ready": ready,
        "status": state,
        "fault_code": fault_code,
        "message": detail,
    }
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    return entry


def probe_local_sqlite(workspace: Path) -> tuple[bool, str]:
    database = (
        workspace / "local-model" / "runtime" / "state" / "local-rag-keywords.sqlite3"
    )

    def sqlite_health() -> None:
        with sqlite3.connect(str(database), timeout=3) as connection:
            connection.execute("SELECT 1").fetchone()

    return retry(sqlite_health)


def probe_local_vector(workspace: Path) -> tuple[bool, str]:
    database = (
        workspace / "local-model" / "runtime" / "state" / "local-rag-vectors.sqlite3"
    )

    def vector_health() -> None:
        with sqlite3.connect(str(database), timeout=3) as connection:
            connection.execute("SELECT 1").fetchone()

    return retry(vector_health, 3)


def probe_and_start_ollama() -> tuple[bool, str]:
    ready, detail = retry(lambda: get_json(f"{OLLAMA_ENDPOINT}/api/tags"), 1)
    if ready:
        return True, "ready"
    ollama_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
    app = ollama_root / "ollama app.exe"
    server = ollama_root / "ollama.exe"
    if app.is_file():
        start_hidden([str(app)])
    elif server.is_file():
        start_hidden([str(server), "serve"])
    return retry(lambda: get_json(f"{OLLAMA_ENDPOINT}/api/tags"))


def warm_model_if_ready(ollama_ready: bool) -> dict[str, Any] | None:
    if not ollama_ready:
        return None
    try:
        post_json(
            f"{OLLAMA_ENDPOINT}/api/generate",
            {"model": WARM_MODEL, "prompt": "", "stream": False, "keep_alive": -1},
            timeout=min(WARM_MODEL_TIMEOUT, 120.0),
        )
        return service_entry(
            "warm_model",
            True,
            "ready",
            critical=False,
            state=STATE_READY,
            fault_code="WARM_MODEL_READY",
        )
    except (OSError, RuntimeError, urllib.error.URLError) as error:
        return service_entry(
            "warm_model",
            False,
            str(error),
            critical=False,
            state=STATE_RECOVERING,
            fault_code="WARM_MODEL_UNAVAILABLE",
        )


def warm_model_detached() -> dict[str, Any]:
    """Fire warm-model preload in a detached subprocess that survives orchestrator exit.

    The daemon-thread approach was unreliable: the orchestrator process exits
    immediately after writing its report, killing any daemon thread before the
    warm request could complete.  A detached subprocess survives the parent
    exit and lets Ollama finish loading the model.
    """

    script = (
        "import json, urllib.request;\n"
        "req = urllib.request.Request(\n"
        "    'http://127.0.0.1:11434/api/generate',\n"
        f"    data=json.dumps({{'model': {WARM_MODEL!r}, 'prompt': '', 'stream': False, 'keep_alive': -1}}).encode('utf-8'),\n"
        "    method='POST',\n"
        "    headers={'Content-Type': 'application/json'},\n"
        ");\n"
        "try:\n"
        "    urllib.request.urlopen(req, timeout=120).read()\n"
        "except Exception:\n"
        "    pass\n"
    )
    start_hidden([sys.executable, "-c", script])
    return service_entry(
        "warm_model",
        True,
        "preloading-in-detached-background",
        critical=False,
        state=STATE_RECOVERING,
        fault_code="WARM_MODEL_PRELOADING",
    )


def append_startup_journal(entry: dict[str, Any]) -> None:
    journal_path = (
        Path(os.environ.get("GPTBRIDGE_STATE_ROOT", ""))
        / "launcher"
        / "state"
        / "startup-journal.jsonl"
    )
    if not journal_path:
        return
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({**entry, "timestamp": time.time()}, ensure_ascii=False)
                + "\n"
            )
    except OSError:
        pass


def main() -> int:
    workspace = Path(__file__).resolve().parents[2]
    report: dict[str, Any] = {
        "startup_order": ["local-sqlite", "vector", "ollama", "warm_model"],
        "mode": "hybrid-parallel-degradables",
    }
    started_at = time.monotonic()
    append_startup_journal({"event": "orchestrator.start", "mode": report["mode"]})

    # Hybrid start: begin the critical local sqlite probe and, in parallel,
    # probe the independent degradable services (vector index, Ollama).
    # The critical path (sqlite) gates launch; degradables are allowed to
    # settle in the background and their results are collected with a short
    # timeout so a slow Ollama startup does not block the launcher.
    local_sqlite: tuple[bool, str] = (False, "not-probed")
    vector_result: tuple[bool, str] = (False, "not-probed")
    ollama_result: tuple[bool, str] = (False, "not-probed")
    with ThreadPoolExecutor(max_workers=DEGRADABLE_WORKERS + 1) as pool:
        sqlite_future: Future[tuple[bool, str]] = pool.submit(
            probe_local_sqlite, workspace
        )
        vector_future: Future[tuple[bool, str]] = pool.submit(
            probe_local_vector, workspace
        )
        ollama_future: Future[tuple[bool, str]] = pool.submit(
            probe_and_start_ollama
        )

        # Wait for the critical path first — this gates launch.
        local_sqlite = sqlite_future.result()
        critical_up = local_sqlite[0]

        # Vector index and Ollama are independent and degradable; give them
        # a bounded grace period rather than blocking indefinitely.  A slow
        # Ollama startup (10-25s) should not delay the launcher.
        DEGRADABLE_GRACE_SECONDS = 5.0
        for future, label in (
            (vector_future, "vector"),
            (ollama_future, "ollama"),
        ):
            try:
                result = future.result(timeout=DEGRADABLE_GRACE_SECONDS)
            except TimeoutError:
                result = (False, f"{label}-probe-timeout-grace-{DEGRADABLE_GRACE_SECONDS}s")
            if label == "vector":
                vector_result = result
            else:
                ollama_result = result

    sqlite_ready, sqlite_detail = local_sqlite
    sqlite_entry = service_entry(
        "local-sqlite",
        sqlite_ready,
        sqlite_detail,
        critical=True,
        state=STATE_READY if sqlite_ready else STATE_FAILED,
        fault_code="LOCAL_SQLITE_READY" if sqlite_ready else "LOCAL_SQLITE_UNAVAILABLE",
    )
    report["local-sqlite"] = sqlite_entry

    vector_ready, vector_detail = vector_result
    vector_entry = service_entry(
        "vector",
        vector_ready,
        vector_detail,
        critical=False,
        state=STATE_READY if vector_ready else STATE_RECOVERING,
        fault_code="VECTOR_READY" if vector_ready else "VECTOR_UNAVAILABLE",
    )
    report["vector"] = vector_entry

    ollama_ready, ollama_detail = ollama_result
    ollama_entry = service_entry(
        "ollama",
        ollama_ready,
        ollama_detail,
        critical=False,
        state=STATE_READY if ollama_ready else STATE_RECOVERING,
        fault_code="OLLAMA_READY" if ollama_ready else "OLLAMA_UNAVAILABLE",
    )
    report["ollama"] = ollama_entry

    # Warm model is a non-blocking background preload; it never gates launch.
    # We give it a short grace period to finish; otherwise it keeps warming in a
    # background thread and the report records a pending preload so the launcher
    # can proceed immediately.
    if ollama_ready:
        report["warm_model"] = warm_model_detached()

    degradable_up = all(
        entry.get("ready") is True
        for entry in report.values()
        if isinstance(entry, dict) and entry.get("component") in {"vector", "ollama"}
    )
    if not critical_up:
        report["state"] = STATE_FAILED
    elif degradable_up:
        report["state"] = STATE_READY
    else:
        report["state"] = STATE_DEGRADED

    report["critical_services"] = ["local-sqlite"]
    report["degradable_services"] = ["vector", "ollama"]
    report["exit_code"] = EXIT_ALL_CRITICAL_UP if critical_up else EXIT_CRITICAL_FAILED
    report["total_duration_ms"] = int((time.monotonic() - started_at) * 1000)

    append_startup_journal(
        {
            "event": "orchestrator.done",
            "state": report["state"],
            "critical_up": critical_up,
            "total_duration_ms": report["total_duration_ms"],
        }
    )

    print(json.dumps(report, ensure_ascii=False))
    return EXIT_ALL_CRITICAL_UP if critical_up else EXIT_CRITICAL_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
