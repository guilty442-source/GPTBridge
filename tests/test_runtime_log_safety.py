from __future__ import annotations

import json
import os
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

import core_logger as core_logger_module
import settings.service as settings_service_module
from core_logger import CoreLogger
from settings.service import SharedSettingsManager


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


def test_log_exports_are_tail_limited_and_pruned(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings_service_module, "MAX_EXPORT_SOURCE_BYTES", 64)
    monkeypatch.setattr(settings_service_module, "MAX_RUNTIME_EXPORT_FILES_PER_PREFIX", 2)
    monkeypatch.setattr(settings_service_module, "MAX_RUNTIME_EXPORT_TOTAL_BYTES", 1024 * 1024)

    logs_root = tmp_path / "runtime" / "logs"
    logs_root.mkdir(parents=True)
    source_log = logs_root / "core.log"
    source_log.write_text(
        "early error line should be outside tail\n"
        + ("filler\n" * 40)
        + "late failure line should be exported\n",
        encoding="utf-8",
    )

    export_dir = tmp_path / "runtime" / "exports"
    export_dir.mkdir(parents=True)
    for index in range(4):
        old_export = export_dir / f"operation-logs-20260101_00000{index}.zip"
        old_export.write_text("old", encoding="utf-8")
        ts = time.time() - (10 - index)
        os.utime(old_export, (ts, ts))

    manager = SharedSettingsManager(object(), tmp_path)

    operation_result = manager._export_logs()
    assert operation_result["ok"] is True

    with zipfile.ZipFile(operation_result["archive"], "r") as archive:
        exported_log = archive.read("core.log").decode("utf-8")

    assert "late failure line should be exported" in exported_log
    assert "early error line should be outside tail" not in exported_log

    error_result = manager._export_error_logs()
    assert error_result["ok"] is True
    assert error_result["matched_lines"] == 1

    summary = Path(error_result["summary"]).read_text(encoding="utf-8")
    assert "late failure line should be exported" in summary
    assert "early error line should be outside tail" not in summary

    remaining_operation_exports = list(export_dir.glob("operation-logs-*.zip"))
    assert len(remaining_operation_exports) <= 2
