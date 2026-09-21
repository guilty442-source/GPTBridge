"""Backend stdout/stderr disk-logging regression tests (workflow B).

boot_core's relay persists every supervised-backend line to the rotating
``main-system/runtime/logs/backend-YYYYMMDD.log`` sink with an inline UTC
timestamp, keeps its existing 200-line in-memory diagnostic buffer, and stays
fail-open when the log directory cannot be written.
"""

from __future__ import annotations

import io
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))

from backend_log_sink import (  # noqa: E402
    BackendLogSink,
    get_backend_log_sink,
    reset_backend_log_sink,
)
from boot_core_lifecycle import BootCoreLifecycleMixin  # noqa: E402


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class _StdoutStub:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


class _RelayHarness(BootCoreLifecycleMixin):
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self._child_output: list[str] = []
        self._child_output_lock = threading.Lock()


@pytest.fixture(autouse=True)
def _isolated_sink() -> Iterator[None]:
    reset_backend_log_sink()
    yield
    reset_backend_log_sink()


def test_writes_timestamped_lines_to_daily_file(tmp_path: Path) -> None:
    moment = datetime(2026, 9, 16, 5, 4, 3, 21000, tzinfo=timezone.utc)
    sink = BackendLogSink(tmp_path, now=_Clock(moment))

    assert sink.write_line("backend ready") is True
    sink.close()

    content = (tmp_path / "backend-20260916.log").read_text("utf-8")
    assert content == "[2026-09-16T05:04:03.021Z] backend ready\n"


def test_rotation_bounds_total_files_and_keeps_latest(tmp_path: Path) -> None:
    moment = datetime(2026, 9, 16, tzinfo=timezone.utc)
    sink = BackendLogSink(tmp_path, max_bytes=120, backup_count=2, now=_Clock(moment))

    for index in range(20):
        assert sink.write_line(f"line-{index:03d}-{'x' * 20}") is True
    sink.close()

    base = tmp_path / "backend-20260916.log"
    names = sorted(path.name for path in tmp_path.glob("backend-*.log*"))
    assert names == [base.name, f"{base.name}.1", f"{base.name}.2"]

    latest = base.read_text("utf-8")
    assert "line-018" in latest and "line-019" in latest
    second_oldest = (tmp_path / f"{base.name}.2").read_text("utf-8")
    assert "line-014" in second_oldest and "line-015" in second_oldest


def test_date_rollover_opens_new_file_and_prunes_old_days(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 9, 16, 23, 59, tzinfo=timezone.utc))
    sink = BackendLogSink(tmp_path, max_bytes=60, backup_count=1, now=clock)

    for index in range(10):
        assert sink.write_line(f"day-one-{index}") is True
    assert len(list(tmp_path.glob("backend-20260916.log*"))) == 2

    clock.moment = datetime(2026, 9, 17, 0, 0, 1, tzinfo=timezone.utc)
    assert sink.write_line("day-two") is True
    sink.close()

    names = sorted(path.name for path in tmp_path.glob("backend-*.log*"))
    assert names == ["backend-20260916.log", "backend-20260917.log"]
    assert (tmp_path / "backend-20260917.log").read_text("utf-8").endswith(
        "day-two\n"
    )


def test_fail_open_when_log_path_is_not_writable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocked = tmp_path / "logs"
    blocked.write_text("occupied", encoding="utf-8")
    sink = BackendLogSink(blocked)

    assert sink.write_line("first") is False
    assert sink.disabled is True
    assert sink.disabled_reason

    # Once disabled the sink stays inert instead of raising or retrying.
    assert sink.write_line("second") is False
    assert "fail-open" in capsys.readouterr().err


def test_singleton_reuses_sink_per_directory(tmp_path: Path) -> None:
    first = get_backend_log_sink(tmp_path)
    assert get_backend_log_sink(tmp_path) is first
    assert get_backend_log_sink(tmp_path / "other") is not first


def test_relay_persists_disk_log_and_keeps_memory_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _RelayHarness(tmp_path)
    monkeypatch.setattr(sys, "stdout", _StdoutStub())
    payload = b"\n".join(f"relay-line-{i}".encode() for i in range(250)) + b"\n"

    harness._relay(io.BytesIO(payload))

    assert len(harness._child_output) <= 200
    assert harness._child_output[-1] == "relay-line-249"

    log_dir = tmp_path / "main-system" / "runtime" / "logs"
    log_files = list(log_dir.glob("backend-*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text("utf-8")
    assert content.count("\n") == 250
    assert "relay-line-249" in content
    assert "] relay-line-0\n" in content


def test_relay_drains_when_stdout_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pythonw.exe / detached launchers expose sys.stdout=None.

    The relay must keep draining the child's pipe so the backend never
    blocks on a full stdout buffer (observed: dead-grace restart loop).
    """
    harness = _RelayHarness(tmp_path)
    monkeypatch.setattr(sys, "stdout", None)
    payload = b"\n".join(f"headless-{i}".encode() for i in range(40)) + b"\n"

    harness._relay(io.BytesIO(payload))

    assert harness._child_output[-1] == "headless-39"
    log_dir = tmp_path / "main-system" / "runtime" / "logs"
    log_files = list(log_dir.glob("backend-*.log"))
    assert len(log_files) == 1
    assert "headless-39" in log_files[0].read_text("utf-8")


def test_relay_survives_broken_stdout_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stdout that raises BrokenPipeError mid-stream must not abort."""

    class _BrokenStdout:
        def __init__(self) -> None:
            self.buffer = self

        def write(self, _raw: bytes) -> int:
            raise BrokenPipeError("launcher gone")

        def flush(self) -> None:
            return None

    harness = _RelayHarness(tmp_path)
    monkeypatch.setattr(sys, "stdout", _BrokenStdout())

    harness._relay(io.BytesIO(b"first\nsecond\n"))

    assert harness._child_output == ["first", "second"]


def test_relay_survives_sink_construction_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_log_dir: object) -> BackendLogSink:
        raise OSError("disk offline")

    monkeypatch.setattr("boot_core_lifecycle.get_backend_log_sink", _boom)
    harness = _RelayHarness(tmp_path)
    monkeypatch.setattr(sys, "stdout", _StdoutStub())

    harness._relay(io.BytesIO(b"alpha\nbeta\n"))

    assert harness._child_output[-1] == "beta"
    assert any("fail-open" in line for line in harness._child_output)


def test_relay_survives_unwritable_sink_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    log_dir = workspace / "main-system" / "runtime" / "logs"
    log_dir.parent.mkdir(parents=True)
    log_dir.write_text("occupied", encoding="utf-8")
    harness = _RelayHarness(workspace)
    monkeypatch.setattr(sys, "stdout", _StdoutStub())

    harness._relay(io.BytesIO(b"gamma\ndelta\n"))

    assert harness._child_output[-1] == "delta"
    assert any("fail-open" in line for line in harness._child_output)
