from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
QDRANT_ENDPOINT = "http://127.0.0.1:6333"
WARM_MODEL = "gemma4:e2b-it-qat"


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


def main() -> int:
    workspace = Path(__file__).resolve().parents[2]
    qdrant_root = workspace / "local-model" / "runtime" / "qdrant"
    report: dict[str, Any] = {"startup_order": ["postgresql", "qdrant", "ollama"]}

    dsn = os.environ.get("GPTBRIDGE_POSTGRES_DSN", "").strip()
    try:
        import psycopg

        if not dsn:
            raise RuntimeError("GPTBRIDGE_POSTGRES_DSN_REQUIRED")

        def postgres_health() -> None:
            with psycopg.connect(dsn, connect_timeout=3) as connection:
                connection.execute("SELECT 1").fetchone()

        ready, detail = retry(postgres_health)
    except Exception as error:
        ready, detail = False, str(error)
    report["postgresql"] = {"ready": ready, "detail": detail}

    if not retry(lambda: get_json(f"{QDRANT_ENDPOINT}/collections"), 1)[0]:
        executable = qdrant_root / "bin" / "qdrant.exe"
        if executable.is_file():
            environment = dict(os.environ)
            environment["QDRANT__SERVICE__HOST"] = "127.0.0.1"
            environment["QDRANT__STORAGE__STORAGE_PATH"] = str(qdrant_root / "storage")
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            subprocess.Popen(
                [str(executable)], cwd=str(qdrant_root), env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags,
            )
    ready, detail = retry(lambda: get_json(f"{QDRANT_ENDPOINT}/collections"))
    report["qdrant"] = {"ready": ready, "detail": detail}

    if not retry(lambda: get_json(f"{OLLAMA_ENDPOINT}/api/tags"), 1)[0]:
        ollama_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
        app = ollama_root / "ollama app.exe"
        server = ollama_root / "ollama.exe"
        if app.is_file():
            start_hidden([str(app)])
        elif server.is_file():
            start_hidden([str(server), "serve"])
    ready, detail = retry(lambda: get_json(f"{OLLAMA_ENDPOINT}/api/tags"))
    report["ollama"] = {"ready": ready, "detail": detail}

    if ready:
        try:
            post_json(
                f"{OLLAMA_ENDPOINT}/api/generate",
                {"model": WARM_MODEL, "prompt": "", "stream": False, "keep_alive": -1},
            )
            report["warm_model"] = {"ready": True, "model": WARM_MODEL}
        except (OSError, RuntimeError, urllib.error.URLError) as error:
            report["warm_model"] = {"ready": False, "model": WARM_MODEL, "detail": str(error)}

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
