"""M1 system-rescue native shadow parity tests (module-language-migration).

Same contract as test_native_e3_shadow: the pure-C prototype in
``native/core/system_rescue.c`` is compared field-by-field against the
authoritative Python implementation in
``Standalone tools/system-rescue/.../platform_packager.py``.

The C layer is decision-free execution semantics only — all I/O facts are
injected — so each test builds the same fact set, runs the real Python
function against a real temp tree, and compares verdicts.

Skips while the running backend holds a locked ``_sovereign_native`` pyd
that predates the M1 bindings.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_CORE = PROJECT_ROOT / "main-system" / "src-core"
SRV = (
    PROJECT_ROOT
    / "Standalone tools"
    / "system-rescue"
    / "src"
    / "backend"
    / "services"
)
sys.path.insert(0, str(SRC_CORE))
sys.path.insert(0, str(SRV))

try:
    from core_system.native import _sovereign_native as _native

    _M1 = all(
        hasattr(_native, name)
        for name in (
            "sr_verify_tool_package",
            "sr_normalize_packager_error",
            "sr_all_ok",
            "sr_verify_archive",
            "sr_sha256_hex",
            "sr_cli_dispatch",
        )
    )
except ImportError:
    _M1 = False

requires_m1 = pytest.mark.skipif(
    not _M1, reason="native pyd predates M1 bindings (rebuild pending)"
)

from system_rescue.integration import platform_packager as pp  # noqa: E402


def _native_pkg_verdict(**facts: object) -> str:
    return _native.sr_verify_tool_package(
        bool(facts.get("release_dir_exists")),
        bool(facts.get("metadata_exists")),
        bool(facts.get("metadata_valid")),
        bool(facts.get("has_source_manifest")),
        int(facts.get("source_mtime", 0)),
        int(facts.get("package_mtime", 0)),
    )


def _python_pkg_verdict_code(result: dict) -> str:
    return "OK" if result.get("ok") else str(result.get("error_code"))


def _make_release(
    root: Path,
    tool_id: str,
    *,
    metadata: bool = True,
    metadata_valid: bool = True,
    source_manifest: bool = False,
    source_mtime: float = 0.0,
    package_mtime: float = 0.0,
) -> Path:
    release_dir = root / "release" / "child-tools" / tool_id
    release_dir.mkdir(parents=True, exist_ok=True)
    if metadata:
        meta = release_dir / "package-metadata.json"
        meta.write_text(
            '{"tool_id": "%s"}' % tool_id if metadata_valid else "{}",
            encoding="utf-8",
        )
        if package_mtime:
            import os

            os.utime(meta, (package_mtime, package_mtime))
    if source_manifest:
        manifest = root / tool_id / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"id": "%s"}' % tool_id, encoding="utf-8")
        if source_mtime:
            import os

            os.utime(manifest, (source_mtime, source_mtime))
    return release_dir


@requires_m1
def test_sha256_parity():
    samples = [
        b"",
        b"abc",
        b"x" * 55,
        b"x" * 56,
        b"x" * 64,
        bytes(range(256)) * 3,
        "星澄模型".encode("utf-8"),
    ]
    for payload in samples:
        assert _native.sr_sha256_hex(payload) == hashlib.sha256(
            payload
        ).hexdigest()


@requires_m1
def test_verify_tool_package_verdict_parity(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "PROJECT_ROOT", tmp_path)
    tool_id = "tool-x"

    scenarios = [
        # (setup kwargs, native facts)
        ({"metadata": False},
         {"release_dir_exists": True, "metadata_exists": False}),
        ({"metadata": True, "metadata_valid": False},
         {"release_dir_exists": True, "metadata_exists": True,
          "metadata_valid": False}),
        ({"metadata": True, "source_manifest": True,
          "source_mtime": 200.0, "package_mtime": 100.0},
         {"release_dir_exists": True, "metadata_exists": True,
          "metadata_valid": True, "has_source_manifest": True,
          "source_mtime": 200, "package_mtime": 100}),
        ({"metadata": True, "source_manifest": True,
          "source_mtime": 100.0, "package_mtime": 100.0},
         {"release_dir_exists": True, "metadata_exists": True,
          "metadata_valid": True, "has_source_manifest": True,
          "source_mtime": 100, "package_mtime": 100}),
        ({"metadata": True},
         {"release_dir_exists": True, "metadata_exists": True,
          "metadata_valid": True}),
    ]

    # package dir entirely absent
    result = pp._verify_tool_package(tool_id)
    assert _python_pkg_verdict_code(result) == "PACKAGE_MISSING"
    assert _native_pkg_verdict(release_dir_exists=False) == "PACKAGE_MISSING"

    for kwargs, facts in scenarios:
        for child in [p for p in tmp_path.iterdir()]:
            import shutil

            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        _make_release(tmp_path, tool_id, **kwargs)
        py_code = _python_pkg_verdict_code(pp._verify_tool_package(tool_id))
        c_code = _native_pkg_verdict(**facts)
        assert py_code == c_code, (kwargs, py_code, c_code)


@requires_m1
def test_normalize_packager_error_parity():
    cases = [
        ("PROCESS_TIMEOUT", "PACKAGER_TIMEOUT"),
        ("PROCESS_LAUNCH_FAILED", "PACKAGER_LAUNCH_FAILED"),
        ("PROCESS_OUTPUT_INVALID", "PACKAGER_OUTPUT_INVALID"),
        ("SOME_OTHER", "SOME_OTHER"),
    ]
    for raw, expected in cases:
        report = pp._normalize_packager_report(
            {"ok": False, "error_code": raw, "message": "m"}, 30
        )
        assert report["error_code"] == _native.sr_normalize_packager_error(raw)
        assert _native.sr_normalize_packager_error(raw) == expected
    assert _native.sr_normalize_packager_error(None) is None


@requires_m1
def test_verify_packaged_tool_parity(tmp_path):
    def py_code(result: dict) -> str:
        return "OK" if result.get("ok") else str(result.get("error_code"))

    def c_code(path: Path, expected: str | None, actual: str | None) -> str:
        sidecar = path.with_name(path.name + ".sha256")
        return _native.sr_verify_archive(
            path.is_file(), sidecar.is_file(), expected, actual
        )

    pkg = tmp_path / "pkg.bin"
    payload = b"packaged-bytes-" + bytes(range(64))
    digest = hashlib.sha256(payload).hexdigest()

    # missing package
    result = pp.verify_packaged_tool(pkg)
    assert py_code(result) == "PACKAGE_NOT_FOUND"
    assert c_code(pkg, None, None) == "PACKAGE_NOT_FOUND"

    # no sidecar -> ok
    pkg.write_bytes(payload)
    result = pp.verify_packaged_tool(pkg)
    assert py_code(result) == "OK"
    assert c_code(pkg, None, None) == "OK"

    sidecar = tmp_path / "pkg.bin.sha256"
    # matching sidecar (certutil-style trailing filename)
    sidecar.write_text(f"{digest.upper()}  pkg.bin\n", encoding="utf-8")
    result = pp.verify_packaged_tool(pkg)
    assert py_code(result) == "OK"
    assert c_code(pkg, digest.lower(), digest) == "OK"

    # mismatched sidecar
    sidecar.write_text("0" * 64 + "  pkg.bin", encoding="utf-8")
    result = pp.verify_packaged_tool(pkg)
    assert py_code(result) == "PACKAGE_CHECKSUM_MISMATCH"
    assert c_code(pkg, "0" * 64, digest) == "PACKAGE_CHECKSUM_MISMATCH"

    # empty sidecar -> UNREADABLE (Python IndexError path)
    sidecar.write_text("   \n", encoding="utf-8")
    result = pp.verify_packaged_tool(pkg)
    assert py_code(result) == "PACKAGE_CHECKSUM_UNREADABLE"
    # Caller injects expected_hex=None when the sidecar cannot be parsed.
    assert c_code(pkg, None, digest) == "PACKAGE_CHECKSUM_UNREADABLE"


@requires_m1
def test_verify_all_aggregation_parity(monkeypatch):
    monkeypatch.setattr(pp, "_discover_packaged_tools", lambda: ["a", "b"])

    def run(oks):
        results = iter(
            {"tool_id": t, "ok": ok} for t, ok in zip(["a", "b"], oks)
        )
        monkeypatch.setattr(
            pp, "_verify_tool_package", lambda _tid: next(results)
        )
        return pp.verify_all_packages()

    for oks in ([True, True], [True, False], [False, True]):
        py = run(oks)
        assert py["ok"] == _native.sr_all_ok(oks)

    # empty discovery -> ok (no packaged tools)
    monkeypatch.setattr(pp, "_discover_packaged_tools", lambda: [])
    assert pp.verify_all_packages()["ok"] == _native.sr_all_ok([])


@requires_m1
def test_cli_dispatch_parity(monkeypatch):
    calls: list[str] = []

    def record(name):
        def stub(*_args, **_kwargs):
            calls.append(name)
            return {"ok": True}

        return stub

    monkeypatch.setattr(pp, "verify_all_packages", record("verify_all"))
    monkeypatch.setattr(
        pp, "deep_verify_tool_package", record("deep_verify_tool")
    )
    monkeypatch.setattr(pp, "_verify_tool_package", record("verify_tool"))
    monkeypatch.setattr(
        pp, "package_platform_tool", record("package_tool")
    )
    monkeypatch.setattr(pp, "_run_packager_cli", record("package_all"))

    scenarios = [
        (["--all", "--verify"], "verify_all", "verify_all"),
        (["--all", "--tool", "x", "--verify"], "verify_all", "verify_all"),
        (["--tool", "x", "--verify", "--deep"], "deep_verify_tool",
         "deep_verify_tool"),
        (["--tool", "x", "--verify"], "verify_tool", "verify_tool"),
        (["--tool", "x", "--package"], "package_tool", "package_tool"),
        (["--all", "--package"], "package_all", "package_all"),
        (["--all"], "verify_all", "verify_all"),
        (["--verify"], "invalid_args", "invalid_args"),
        (["--tool", "x"], "invalid_args", "invalid_args"),
    ]
    for argv, py_op, c_op in scenarios:
        calls.clear()
        rc = pp._main([*argv, "--json"])
        py_observed = calls[0] if calls else "invalid_args"
        assert py_observed == py_op
        flags = {a.lstrip("-") for a in argv if a.startswith("--")}
        tool_idx = argv.index("--tool") if "--tool" in argv else -1
        tool = argv[tool_idx + 1] if tool_idx >= 0 else None
        assert _native.sr_cli_dispatch(
            "all" in flags,
            tool,
            "verify" in flags,
            "deep" in flags,
            "package" in flags,
        ) == c_op
        assert rc == (0 if py_observed != "invalid_args" else 1)
