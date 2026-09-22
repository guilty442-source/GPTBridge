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


# --- governed-tool-runtime ABI subset (star-governed-tool-runtime-abi/v1) ---

_M1_GT = _M1 and all(
    hasattr(_native, name)
    for name in (
        "gt_tool_id_valid",
        "gt_session_token_valid",
        "gt_port_valid",
        "gt_env_gate",
        "gt_workspace_instance_id",
        "gt_shutdown_gate",
        "gt_ws_gate",
        "gt_request_valid",
        "gt_idle_next_ms",
        "gt_wait_timeout_ms",
        "gt_health_degraded",
    )
)
requires_m1_gt = pytest.mark.skipif(
    not _M1_GT, reason="native pyd predates M1 governed-tool bindings"
)

sys.path.insert(0, str(PROJECT_ROOT))
from governance_rule.execution.tool_runtime.governed_runtime_constants import (  # noqa: E402
    TOKEN_PATTERN,
)


def _py_token_valid(token: str | None) -> bool:
    if token is None:
        return False
    return TOKEN_PATTERN.fullmatch(str(token).strip().lower()) is not None


@requires_m1_gt
def test_gt_format_validation_parity():
    tool_ids = ["system-rescue", "a1", "x", "_lead", "UPPER", "has space",
                "a" * 63, "a" * 64, "", "tool.id"]
    import re

    pattern = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
    for tid in tool_ids:
        assert _native.gt_tool_id_valid(tid) == bool(pattern.fullmatch(tid))
    assert not _native.gt_tool_id_valid(None)

    tokens = [
        "a" * 64,
        "A" * 64,  # Python .lower() accepts uppercase
        "  " + "b" * 64 + "\n",  # Python .strip() accepts padding
        "g" * 64,
        "a" * 63,
        "",
        None,
    ]
    for tok in tokens:
        assert _native.gt_session_token_valid(tok) == _py_token_valid(tok)

    for port in [0, 80, 1023, 1024, 8765, 65535, 65536, 99999]:
        assert _native.gt_port_valid(port) == (1024 <= port <= 65535)


@requires_m1_gt
def test_gt_workspace_instance_id_parity():
    # governed_runtime_maintenance.workspace_instance_id:
    # sha256(f"{tool_id}:{port}")[:16] — the authoritative (mixin) formula.
    for tool_id, port in [("system-rescue", 8765), ("investment-mobile", 9000),
                          ("x9", 1024)]:
        expected = hashlib.sha256(
            f"{tool_id}:{port}".encode("utf-8")
        ).hexdigest()[:16]
        assert _native.gt_workspace_instance_id(tool_id, port) == expected


@requires_m1_gt
def test_gt_shutdown_and_ws_gate_parity():
    import hmac

    def py_shutdown(env_tok, supplied):
        env = str(env_tok or "")
        provided = str(supplied or "")
        return bool(env) and hmac.compare_digest(provided, env)

    for env, sup in [("tok", "tok"), ("tok", "tok2"), ("", "x"), (None, "x"),
                     ("tok", ""), ("tok", None), ("tok", "TOK")]:
        assert _native.gt_shutdown_gate(env, sup) == py_shutdown(env, sup)

    def py_ws(session_tok, supplied_tok, expected_inst, supplied_inst):
        st = str(session_tok or "")
        supt = str(supplied_tok or "").lower()
        return hmac.compare_digest(supt, st) and supplied_inst == expected_inst

    tok = "a" * 64
    for args in [(tok, tok, "i1", "i1"), (tok, tok.upper(), "i1", "i1"),
                 (tok, "b" * 64, "i1", "i1"), (tok, tok, "i1", "i2"),
                 (tok, "", "i1", "i1")]:
        assert _native.gt_ws_gate(*args) == py_ws(*args)


@requires_m1_gt
def test_gt_request_prevalidation_parity():
    def py_valid(command, payload_is_dict, request_id, payload_tool_id, self_id):
        cmd = str(command or "").strip()
        if not cmd or not payload_is_dict:
            return False
        rid = str(request_id or "").strip()
        if not rid or len(rid) > 256:
            return False
        return str(payload_tool_id or self_id) == self_id

    cases = [
        ("cmd", True, "r1", None, "tool-x"),
        ("cmd", True, "r1", "tool-x", "tool-x"),
        ("cmd", True, "r1", "other", "tool-x"),
        ("", True, "r1", None, "tool-x"),
        ("cmd", False, "r1", None, "tool-x"),
        ("cmd", True, "", None, "tool-x"),
        ("cmd", True, "r" * 256, None, "tool-x"),
        ("cmd", True, "r" * 257, None, "tool-x"),
    ]
    for args in cases:
        assert _native.gt_request_valid(*args) == py_valid(*args)


@requires_m1_gt
def test_gt_idle_backoff_parity():
    # Replay the real _worker ladder: idle_poll=0.25s; timeout ->
    # min(x1.5, 0.5); notify/request -> reset 0.25; wait_timeout =
    # 0.05 if notify pending else max(idle_poll, 0.05).
    idle_s = 0.25
    c_idle_ms = 0  # seed below baseline; first call treats as 250ms
    for notified in [0, 0, 0, 1, 0, 0, 1, 0]:
        if notified:
            idle_s = 0.25
        else:
            idle_s = min(idle_s * 1.5, 0.5)
        c_idle_ms = _native.gt_idle_next_ms(c_idle_ms, notified)
        assert c_idle_ms == round(idle_s * 1000)

        py_wait = 0.05 if notified else max(idle_s, 0.05)
        assert _native.gt_wait_timeout_ms(c_idle_ms, notified) == round(
            py_wait * 1000
        )


@requires_m1_gt
def test_gt_channel_health_parity():
    for fails in [0, 1, 2, 3, 4, 10]:
        assert _native.gt_health_degraded(fails) == (fails >= 3)


@requires_m1_gt
def test_gt_env_gate_parity():
    tok = "a" * 64
    for facts in [
        (True, True, tok, 8765, True, True),
        (False, True, tok, 8765, True, False),
        (True, False, tok, 8765, True, False),
        (True, True, "bad", 8765, True, False),
        (True, True, tok, 80, True, False),
        (True, True, tok, 8765, False, False),
    ]:
        *args, expected = facts
        assert _native.gt_env_gate(*args) == expected
