from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

import core_logger as core_logger_module
from core_logger import CoreLogger
from ipc.server import _toolbox_result_log_payload


def test_core_logger_rotates_and_compacts_large_payload(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(core_logger_module, "MAX_LOG_FILE_BYTES", 128)
    monkeypatch.setattr(core_logger_module, "MAX_PAYLOAD_TEXT_CHARS", 32)

    logger = CoreLogger(tmp_path)
    log_path = tmp_path / "runtime" / "logs" / "core.log"
    log_path.write_text("x" * 256, encoding="utf-8")

    logger.write(
        "core",
        "large result",
        {
            "stdout": "y" * 1000,
            "nested": {"a": {"b": {"c": {"deep": "z" * 1000}}}},
        },
    )

    assert log_path.exists()
    assert log_path.with_name("core.log.1").exists()

    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert record["payload"]["stdout"].endswith("[truncated 968 chars]")
    assert "[truncated" in record["payload"]["nested"]["a"]["b"]["c"]


def test_core_logger_redacts_sensitive_keys_and_environment_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXAMPLE_API_KEY", "super-secret-provider-value")
    logger = CoreLogger(tmp_path)

    log_path = logger.write(
        "core",
        "redaction",
        {
            "access_token": "direct-token-value",
            "stdout": "provider said super-secret-provider-value",
            "nested": {"password": "plain-text-password"},
        },
    )

    record_text = log_path.read_text(encoding="utf-8")
    record = json.loads(record_text)
    assert record["payload"]["access_token"] == "[REDACTED]"
    assert record["payload"]["nested"]["password"] == "[REDACTED]"
    assert "super-secret-provider-value" not in record_text
    assert "[REDACTED]" in record["payload"]["stdout"]


def test_core_logger_suppresses_repeated_failures_within_window(
    tmp_path: Path,
) -> None:
    logger = CoreLogger(tmp_path)
    for _ in range(5):
        logger.write(
            "error",
            "same failure",
            {"error": "locked resource"},
        )

    records = [
        json.loads(line)
        for line in (
            tmp_path / "runtime" / "logs" / "error.log"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1


def test_core_logger_replaces_oversized_json_with_safe_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    byte_limit = 1024
    monkeypatch.setattr(core_logger_module, "MAX_LOG_RECORD_BYTES", byte_limit)
    logger = CoreLogger(tmp_path)

    log_path = logger.write(
        "core",
        "large structured result",
        {
            f"field_{index}": "界" * 4000
            for index in range(50)
        },
    )

    record_bytes = log_path.read_bytes()
    assert len(record_bytes) <= byte_limit
    record = json.loads(record_bytes.decode("utf-8"))
    assert record["category"] == "core"
    assert record["message"] == "large structured result"
    assert record["payload"]["truncated"] is True
    assert record["payload"]["reason"] == "log record exceeded byte limit"
    assert record["payload"]["original_size_bytes"] > byte_limit
    assert record["payload"]["payload_type"] == "dict"
    assert record["payload"]["top_level_field_count"] == 50
    assert "界" * 100 not in record_bytes.decode("utf-8")


def test_toolbox_result_log_omits_raw_tool_output() -> None:
    payload = {
        "ok": True,
        "tool_id": "sample-tool",
        "request_id": "request-1",
        "status": "completed",
        "exit_code": 0,
        "stdout": '{"portfolio":{"account":"private"}}',
        "stderr": "private diagnostic",
        "output": {
            "stdout": "duplicate private output",
            "stderr": "duplicate private error",
        },
    }

    summary = _toolbox_result_log_payload(payload)

    serialized = json.dumps(summary)
    assert summary["ok"] is True
    assert summary["tool_id"] == "sample-tool"
    assert summary["stdout_bytes"] > 0
    assert summary["stderr_bytes"] > 0
    assert "stdout" not in summary
    assert "stderr" not in summary
    assert "private" not in serialized
