from __future__ import annotations

import importlib.util
import os
import stat
import sys
import uuid
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "platform_tools"
    / "file-sorter"
    / "src"
    / "sorter_v2.py"
)
SPEC = importlib.util.spec_from_file_location(
    "file_sorter_metadata_v2_module",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
sorter_v2 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sorter_v2
SPEC.loader.exec_module(sorter_v2)


def _cross_volume_plan(
    target: Path,
    source: Path,
    destination: Path,
):
    source_stat = source.stat()
    operation = sorter_v2.PlanOperation(
        operation_id=str(uuid.uuid4()),
        source=str(source.resolve()),
        destination=str(destination.resolve()),
        keyword="payload",
        folder=destination.parent.name,
        rule_source="test",
        source_size=source_stat.st_size,
        source_mtime_ns=source_stat.st_mtime_ns,
        transfer="cross-volume",
    )
    return sorter_v2.new_plan(
        target,
        profile_id="metadata-test-profile",
        rules_revision=1,
        quiet_seconds=0,
        operations=[operation],
        skipped=[],
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits required")
def test_cross_volume_move_preserves_mtime_and_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination_dir = tmp_path / "sorted"
    destination_dir.mkdir()
    source = tmp_path / "payload.bin"
    source.write_bytes(b"metadata-preserving payload")
    os.chmod(source, 0o640)
    timestamp_ns = 1_704_067_200_123_456_789
    os.utime(source, ns=(timestamp_ns, timestamp_ns))
    expected = source.stat()
    destination = destination_dir / source.name
    plan = _cross_volume_plan(tmp_path, source, destination)
    monkeypatch.setattr(sorter_v2, "same_volume", lambda *_args: False)

    result = sorter_v2.execute_plan(plan, state_root=tmp_path / "state")

    assert result["ok"] is True
    assert not source.exists()
    actual = destination.stat()
    assert actual.st_mtime_ns == expected.st_mtime_ns
    assert stat.S_IMODE(actual.st_mode) == stat.S_IMODE(expected.st_mode)
    assert destination.read_bytes() == b"metadata-preserving payload"


@pytest.mark.skipif(os.name == "nt", reason="POSIX extended attributes required")
def test_cross_volume_move_preserves_extended_attributes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "setxattr") or not hasattr(os, "getxattr"):
        pytest.skip("Python does not expose extended attributes")
    destination_dir = tmp_path / "sorted"
    destination_dir.mkdir()
    source = tmp_path / "payload.bin"
    source.write_bytes(b"extended attribute payload")
    attribute_name = "user.gptbridge-test"
    attribute_value = b"metadata survives"
    try:
        os.setxattr(source, attribute_name, attribute_value)
    except OSError as error:
        pytest.skip(f"filesystem does not support user xattrs: {error}")
    destination = destination_dir / source.name
    plan = _cross_volume_plan(tmp_path, source, destination)
    monkeypatch.setattr(sorter_v2, "same_volume", lambda *_args: False)

    result = sorter_v2.execute_plan(plan, state_root=tmp_path / "state")

    assert result["ok"] is True
    assert not source.exists()
    assert os.getxattr(destination, attribute_name) == attribute_value


def test_metadata_verification_failure_keeps_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination_dir = tmp_path / "sorted"
    destination_dir.mkdir()
    source = tmp_path / "payload.bin"
    source.write_bytes(b"source must survive")
    destination = destination_dir / source.name
    plan = _cross_volume_plan(tmp_path, source, destination)
    monkeypatch.setattr(sorter_v2, "same_volume", lambda *_args: False)
    original_verify = sorter_v2._verify_file_metadata

    def fail_published_metadata(path, expected, *, label):
        if label == "Published file":
            raise sorter_v2.SorterV2Error("simulated metadata mismatch")
        return original_verify(path, expected, label=label)

    monkeypatch.setattr(
        sorter_v2,
        "_verify_file_metadata",
        fail_published_metadata,
    )

    result = sorter_v2.execute_plan(plan, state_root=tmp_path / "state")

    assert result["ok"] is False
    assert source.read_bytes() == b"source must survive"
    # A published copy is never destroyed merely because metadata
    # verification failed; both copies remain available for recovery/review.
    assert destination.read_bytes() == b"source must survive"
    assert "metadata mismatch" in result["errors"][0]


def test_recovery_rechecks_metadata_before_deleting_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination_dir = tmp_path / "sorted"
    destination_dir.mkdir()
    source = tmp_path / "payload.bin"
    source.write_bytes(b"recovery payload")
    destination = destination_dir / source.name
    state_root = tmp_path / "state"
    plan = _cross_volume_plan(tmp_path, source, destination)
    monkeypatch.setattr(sorter_v2, "same_volume", lambda *_args: False)
    original_unlink = sorter_v2._unlink_file_preserving_failure
    interrupted = False

    def interrupt_source_delete(path, *, missing_ok=False):
        nonlocal interrupted
        if Path(path) == source and not interrupted:
            interrupted = True
            raise OSError("simulated interruption before source deletion")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(
        sorter_v2,
        "_unlink_file_preserving_failure",
        interrupt_source_delete,
    )
    result = sorter_v2.execute_plan(plan, state_root=state_root)
    assert result["ok"] is False
    assert source.exists() and destination.exists()

    monkeypatch.setattr(
        sorter_v2,
        "_unlink_file_preserving_failure",
        original_unlink,
    )
    changed_mtime_ns = destination.stat().st_mtime_ns + 10_000_000
    os.utime(destination, ns=(changed_mtime_ns, changed_mtime_ns))

    recovered = sorter_v2.recover_transactions(
        state_root=state_root,
        target_dir=tmp_path,
    )

    assert recovered
    assert recovered[0]["status"] == "completed_with_errors"
    assert source.read_bytes() == b"recovery payload"
    assert "metadata verification failed" in recovered[0]["errors"][0]


def test_exclusive_copy_does_not_overwrite_existing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"new")
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        sorter_v2._copy_file_exclusive_preserving_metadata(source, destination)

    assert destination.read_bytes() == b"existing"


@pytest.mark.skipif(os.name != "nt", reason="Windows file attributes required")
def test_cross_volume_move_preserves_windows_read_only_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination_dir = tmp_path / "sorted"
    destination_dir.mkdir()
    source = tmp_path / "readonly.bin"
    source.write_bytes(b"read-only payload")
    os.chmod(source, stat.S_IREAD)
    destination = destination_dir / source.name
    plan = _cross_volume_plan(tmp_path, source, destination)
    monkeypatch.setattr(sorter_v2, "same_volume", lambda *_args: False)

    result = sorter_v2.execute_plan(plan, state_root=tmp_path / "state")

    assert result["ok"] is True
    assert not source.exists()
    assert int(destination.stat().st_file_attributes) & 0x1
    os.chmod(destination, stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="Windows alternate streams required")
def test_cross_volume_move_preserves_windows_alternate_data_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination_dir = tmp_path / "sorted"
    destination_dir.mkdir()
    source = tmp_path / "payload.bin"
    source.write_bytes(b"default stream")
    stream_path = f"{source}:gptbridge-metadata"
    try:
        with open(stream_path, "wb") as stream:
            stream.write(b"named stream payload")
        source_streams = sorter_v2._windows_alternate_streams(source)
    except OSError as error:
        pytest.skip(f"filesystem does not support alternate streams: {error}")
    if not source_streams:
        pytest.skip("filesystem does not expose alternate streams")

    destination = destination_dir / source.name
    plan = _cross_volume_plan(tmp_path, source, destination)
    monkeypatch.setattr(sorter_v2, "same_volume", lambda *_args: False)

    result = sorter_v2.execute_plan(plan, state_root=tmp_path / "state")

    assert result["ok"] is True
    assert not source.exists()
    with open(f"{destination}:gptbridge-metadata", "rb") as stream:
        assert stream.read() == b"named stream payload"
    assert sorter_v2._windows_alternate_streams(destination) == source_streams
