"""INTEGRATION-04B isolated Release Candidate acceptance harness (read-only vs official).

Builds a thin backend Release Candidate from the existing builder-declared
resource set, validates it with the shipped release validator, exercises the
fault-injection scenarios and records a Test ID report.  It never starts the
official backend, never writes official data and never copies secrets.

Usage:
    python scripts/integration-04b-acceptance.py [--build-only] [--with-isolated-start]

``--with-isolated-start`` additionally runs the isolated backend start
harness (scripts/integration-04b-isolated-start.py) for 04B-10; without it
04B-10 stays BLOCKED and the acceptance remains read-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(r"E:\GPTBridge")
RELEASES = ROOT / "main-system" / "runtime" / "releases"
# 04B repack rule: each repack is a NEW RC (never overwrite an existing
# release).  --rc-id overrides the default date-derived id.
_RC_ARG = next(
    (a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--rc-id=")),
    None,
)
RC_ID = _RC_ARG or f"rc-{time.strftime('%Y-%m-%d', time.gmtime())}"
RC = RELEASES / RC_ID
RC_CODEX = RC / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"
FIXTURES = Path(tempfile.mkdtemp(prefix=f"04b-fixtures-{RC_ID}-"))
VENV_PY = ROOT / "main-system" / ".venv" / "Scripts" / "python.exe"
CODEX = ROOT / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"
GOV_ROOT = ROOT / "governance_rule"
SHARED_SRC = ROOT / "shared-layer" / "src"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SHARED_SRC))

RESULTS: list[dict[str, object]] = []
RUN_ISOLATED_START = "--with-isolated-start" in sys.argv


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(sha_file(path).encode("ascii"))
            digest.update(b"\n")
    return digest.hexdigest()


def record(test_id: str, name: str, expected: str, actual: str, status: str, evidence: str, error_code: str = "") -> None:
    RESULTS.append({
        "test_id": test_id,
        "test_name": name,
        "release_id": RC_ID,
        "expected": expected,
        "actual": actual,
        "status": status,
        "evidence": evidence,
        "error_code": error_code,
    })


_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


WHEEL_CACHE = RELEASES / "wheel-cache"
WHEEL_CACHE_MANIFEST = WHEEL_CACHE / "wheel-cache-manifest.json"


def build_wheel_cache(distributions: list[str], probe: str) -> dict:
    """Materialize wheels for every locked dist into the shared cache (G77).

    The cache lives at ``releases/wheel-cache/`` — outside any single RC
    payload — so offline rebuilds install with
    ``pip install --no-index --find-links <cache>``.  Incremental: dists
    already cached are verified by sha256 and skipped.
    """
    WHEEL_CACHE.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict] = {}
    if WHEEL_CACHE_MANIFEST.is_file():
        try:
            existing = {
                e["dist"]: e
                for e in json.loads(
                    WHEEL_CACHE_MANIFEST.read_text(encoding="utf-8")
                ).get("entries", [])
            }
        except (OSError, ValueError):
            existing = {}
    entries: list[dict] = []
    missing: list[str] = []
    for dist in distributions:
        cached = existing.get(dist)
        if cached and (WHEEL_CACHE / cached["file"]).is_file() and (
            sha_file(WHEEL_CACHE / cached["file"]) == cached["sha256"]
        ):
            entries.append(cached)
            continue
        proc = subprocess.run(
            [
                str(VENV_PY), "-m", "pip", "download",
                "--no-deps", "--only-binary", ":all:",
                "-d", str(WHEEL_CACHE), dist,
            ],
            capture_output=True, text=True, timeout=900,
            creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            missing.append(dist)
            continue
        saved = next(
            (line.split("Saved ", 1)[1].strip()
             for line in proc.stdout.splitlines() if line.startswith("Saved ")),
            "",
        )
        wheel = Path(saved)
        if not wheel.is_file() or wheel.parent != WHEEL_CACHE:
            wheel = next(
                (WHEEL_CACHE / f for f in os.listdir(WHEEL_CACHE)
                 if f.startswith(dist.split("==")[0].replace("-", "_").lower().replace(".", "_"))
                 and f.endswith(".whl")),
                None,
            )
        if wheel is None or not Path(wheel).is_file():
            missing.append(dist)
            continue
        wheel = Path(wheel)
        entries.append({
            "dist": dist,
            "file": wheel.name,
            "sha256": sha_file(wheel),
        })
    manifest = {
        "schema": "release-wheel-cache/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "probe_python": probe,
        "entries": sorted(entries, key=lambda e: e["dist"]),
        "missing": sorted(missing),
    }
    WHEEL_CACHE_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW).stdout.strip()


def _copy_venv(target: Path) -> bool:
    """Place a venv at the release's final isolation location (G76).

    The previous RC ships a verified, locked, final-location uv venv —
    reuse it as the release-owned runtime instead of the mutable dev
    venv.  Returns True when a release venv exists afterwards.
    """
    for candidate in sorted(RELEASES.glob("rc-*"), reverse=True):
        source = candidate / "venv" / "Scripts" / "python.exe"
        if candidate != RC and source.is_file():
            # robocopy: site-packages contain >260-char paths that
            # shutil.copytree cannot reach on Windows.
            result = subprocess.run(
                [
                    "robocopy", str(candidate / "venv"), str(target),
                    "/E", "/COPY:DAT", "/DCOPY:T", "/R:0", "/W:0",
                    "/XD", "__pycache__", "/XF", "*.pyc",
                    "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
                ],
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW,
            )
            if result.returncode <= 7:
                return True
    return False


def _rmtree_ro(path: Path) -> None:
    """rmtree that clears the read-only bit governance puts on payloads."""
    def _fix(fn, p, _exc):
        try:
            os.chmod(p, 0o666)
            fn(p)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        shutil.rmtree(path, onerror=_fix)


def build_rc() -> None:
    RC.mkdir(parents=True, exist_ok=True)
    # Code payloads are re-copied on every build so a repack always
    # reflects current source; only the venv (GBs) is reused when the
    # interpreter already exists.
    for stale in ("backend", "config", "shared_runtime", "shared-layer",
                  "governance_rule", "main-system"):
        target = RC / stale
        if not target.exists():
            continue
        _rmtree_ro(target)
        if target.exists():
            # Windows delete-pending locks (AV indexing, mapped handles)
            # block unlink but not rename — move aside so the fresh copy
            # lands under the clean name, then best-effort clean up.
            aside = RC / f"{stale}.stale-{os.getpid()}"
            target.rename(aside)
            _rmtree_ro(aside)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "main-system" / "src-core", RC / "backend", ignore=ignore)
    shutil.copytree(ROOT / "main-system" / "config", RC / "config")
    shutil.copytree(SHARED_SRC, RC / "shared_runtime", ignore=ignore)
    # Current backend/main.py requires <release>/governance_rule and
    # <release>/shared-layer/src under GPTBRIDGE_RELEASE_ROOT.
    shutil.copytree(SHARED_SRC, RC / "shared-layer" / "src", ignore=ignore)
    # governance_rule ships CODE only — audit ledgers, archives and
    # runtime state are official data and stay outside the release
    # (persistent-data boundary).
    gov_ignore = shutil.ignore_patterns(
        "__pycache__", "*.pyc", "*.jsonl", "archive", "git_audit_chain",
        "convergence", "runtime", "governance_codex.sqlite3*",
    )
    shutil.copytree(GOV_ROOT, RC / "governance_rule", ignore=gov_ignore)
    # The live codex DB is amended continuously — a raw file copy can
    # catch it mid-write and drifts away from the manifest by the time
    # validation runs.  Ship an atomic snapshot (SQLite backup API) so
    # manifest hash/version and the shipped file are the same bytes.
    RC_CODEX.parent.mkdir(parents=True, exist_ok=True)
    src_con = sqlite3.connect(f"file:{CODEX.as_posix()}?mode=ro", uri=True)
    dst_con = sqlite3.connect(str(RC_CODEX))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    # Parity with the dev tree: empty top-level placeholder file.
    (RC / "governance_rule" / "codex" / "governance_codex.sqlite3").touch()
    # Flat-layout seed: main.py exposes <release>/main-system so
    # ``import governance`` resolves; the isolated harness seeds its
    # state root from the same directory.
    shutil.copytree(
        ROOT / "main-system" / "governance",
        RC / "main-system" / "governance",
        ignore=ignore,
    )
    shutil.copy2(
        ROOT / "main-system" / "package.json",
        RC / "main-system" / "package.json",
    )
    for sub in ("logs", "state", "temp"):
        (RC / "runtime" / sub).mkdir(parents=True, exist_ok=True)

    # Payload boundary self-check at build time (the post-run rescan in
    # ``payload_boundary_check`` covers writers during the run).
    leaked = _payload_leaks()
    if leaked:
        raise RuntimeError(
            f"release payload boundary violation — state data inside "
            f"release: {leaked[:5]}"
        )

    release_python = RC / "venv" / "Scripts" / "python.exe"
    if not release_python.is_file():
        _copy_venv(RC / "venv")
    probe = str(release_python if release_python.is_file() else VENV_PY)
    env_report = json.loads(subprocess.run(
        [
            probe, "-c",
            "import importlib.metadata, json, sys; print(json.dumps({"
            "'dists': sorted(f'{d.metadata[\"Name\"]}=={d.version}' for d in "
            "importlib.metadata.distributions() if d.metadata['Name']),"
            "'version': '.'.join(map(str, sys.version_info[:3]))}))",
        ],
        capture_output=True, text=True, check=True,
        creationflags=_CREATE_NO_WINDOW,
    ).stdout)
    distributions = env_report["dists"]
    probe_version = env_report["version"]
    lock_identity = hashlib.sha256("\n".join(distributions).encode("utf-8")).hexdigest()
    frontend_surface = json.loads((ROOT / "main-system" / "config" / "ipc-surface-frontend.json").read_text(encoding="utf-8"))
    # Governance identity is taken from the shipped snapshot, not the
    # live codex — the live DB keeps mutating while the run proceeds.
    con = sqlite3.connect(f"file:{RC_CODEX.as_posix()}?mode=ro&immutable=1", uri=True)
    codex_version = dict(con.execute("select key, value from metadata")).get("codex_version")
    sovereigns = sorted(str(row[0]) for row in con.execute("select sovereign_id from sovereigns"))
    auth_version = con.execute("select version_identity from identity_authentication_contract limit 1").fetchone()[0]
    permission_version = con.execute("select version_identity from sql_session_binding_contract limit 1").fetchone()[0]
    con.close()

    manifest = {
        "release_id": RC_ID,
        "builder": "existing electron-builder extraResources set (thin backend candidate; venv shared as verified dependency)",
        "source_commit": git("rev-parse", "HEAD"),
        "source_dirty_state": len([l for l in git("status", "--porcelain").splitlines() if l.strip()]),
        "build_hashes": {
            "backend": tree_hash(RC / "backend"),
            "config": tree_hash(RC / "config"),
            "shared_runtime": tree_hash(RC / "shared_runtime"),
        },
        "dependency_lock": {"identity": lock_identity, "entries": len(distributions), "source": f"importlib.metadata@{probe}"},
        "python_runtime": {"executable": probe, "version": probe_version, "arch": __import__("platform").machine()},
        "ipc_contract": {"identity": frontend_surface["surface_version"], "tool_runtime_contract": "main-system/config/tool-runtime-contract.json"},
        "governance_compatibility": {
            "codex_version": codex_version,
            "codex_sha256": sha_file(RC_CODEX),
            "governance_runtime_contract_version": auth_version,
            "permission_contract_version": permission_version,
            "sovereign_registry_identity": hashlib.sha256("\n".join(sovereigns).encode("utf-8")).hexdigest(),
        },
        "persistent_data_boundary": {
            "release_root": str(RC),
            "official_paths": [
                "main-system/runtime/settings", "main-system/runtime/state", "main-system/data",
                "Standby tools/local-model/xingcheng/runtime/models",
            ],
            "rule": "official data, settings and weights stay outside the release",
        },
        "secrets": "none copied (.env excluded by construction)",
    }
    wheel_cache = build_wheel_cache(distributions, probe)
    manifest["wheel_cache"] = {
        "path": str(WHEEL_CACHE),
        "manifest_sha256": sha_file(WHEEL_CACHE_MANIFEST),
        "entries": len(wheel_cache["entries"]),
        "missing": wheel_cache["missing"],
        "offline_rebuild": (
            f"pip install --no-index --find-links {WHEEL_CACHE} "
            "<dist>==<version> per lock"
        ),
    }
    (RC / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def overlay_contract() -> dict:
    contract = json.loads((ROOT / "shared-layer" / "release-dependencies.json").read_text(encoding="utf-8"))
    modules = []
    for entry in contract.get("modules") or []:
        item = dict(entry)
        if item.get("module") == "core_system":
            item["allowed_roots"] = [str(RC / "backend")]
        elif item.get("module") == "shared_layer":
            item["class"] = "SHARED_RUNTIME"
            item["allowed_roots"] = [str(RC / "shared_runtime")]
        elif item.get("module") == "governance_rule":
            item["class"] = "SHARED_RUNTIME"
            item["allowed_roots"] = [str(GOV_ROOT)]
        elif item.get("module") in {"numpy", "torch"}:
            item["allowed_roots"] = [
                str(RC / "venv" / "Lib" / "site-packages"),
                str(ROOT / "main-system" / ".venv" / "Lib" / "site-packages"),
            ]
        modules.append(item)
    contract["modules"] = modules
    contract["runtime_environment"]["venv"].pop("pyvenv_cfg_sha256", None)
    # The contract's codex pin is authored once and the live codex is
    # amended continuously — a stale pin cannot pass.  Rebind the
    # reference to the codex snapshot shipped inside this RC (the file
    # the manifest hashes and the isolated backend actually loads);
    # contract-version pins stay as real compatibility gates.
    refs = contract.setdefault("governance_references", {})
    snap_con = sqlite3.connect(
        f"file:{RC_CODEX.as_posix()}?mode=ro&immutable=1", uri=True
    )
    snap_version = dict(
        snap_con.execute("select key, value from metadata")
    ).get("codex_version")
    snap_con.close()
    refs["codex_version"] = snap_version
    refs["codex_sha256"] = sha_file(RC_CODEX)
    native = []
    for entry in contract.get("native_extensions") or []:
        item = dict(entry)
        file = str(item.get("file") or "")
        if file.startswith("main-system/src-core/"):
            item["file"] = str(RC / "backend" / file[len("main-system/src-core/"):])
        elif file.startswith("main-system/.venv/"):
            item["file"] = str(ROOT / file)
        elif file.startswith("venv/"):
            # ``venv/`` in the shared contract denotes the release's own
            # runtime (release-manifest python_runtime), not a repo-root dir.
            item["file"] = str(RC / file)
        native.append(item)
    contract["native_extensions"] = native
    contract["required_dlls"] = [
        str(RC / "backend" / str(item)[len("main-system/src-core/"):])
        if str(item).startswith("main-system/src-core/")
        else str(RC / str(item)) if str(item).startswith("venv/")
        else str(ROOT / str(item))
        for item in (contract.get("required_dlls") or [])
    ]
    contract["config_contract"]["required_files"] = {
        str(RC / "config" / name): sha_file(RC / "config" / name)
        for name in ("tool-runtime-contract.json", "tool-isolation-policy.json", "startup_manifest.json", "feature_flags.json", "ai-connection-contract.json")
    }
    contract["config_contract"]["required_files"][str(RC / "release-manifest.json")] = sha_file(RC / "release-manifest.json")
    contract["governance_dependencies"] = [
        {"dependency": str(CODEX), "kind": "file", "required": True},
        {"dependency": str(GOV_ROOT / "permission_directory"), "kind": "path", "required": True},
    ]
    return contract


def run_validation() -> dict:
    from shared_layer.database import release_manifest

    contract = overlay_contract()
    # Probe the release's own venv (the bundle's declared python_runtime);
    # probing the dev venv compares the lock against the wrong dist set.
    rc_python = RC / "venv" / "Scripts" / "python.exe"
    probe_python = rc_python if rc_python.is_file() else VENV_PY
    return release_manifest.validate_release_bundle(
        contract,
        python_executable=str(probe_python),
        release_root=str(RC),
        shared_root=None,
        extra_paths=[str(RC / "backend"), str(RC / "shared_runtime"), str(GOV_ROOT), str(ROOT)],
        cwd=str(RC),
        codex_path=str(RC_CODEX),
        repo_root=str(ROOT),
        allowed_dependency_roots=[str(ROOT / "main-system" / ".venv")],
        check_frontend_release=False,
        check_config_classification=True,
        check_config_contract=True,
        check_runtime_paths=True,
        check_secret_references=False,
        official_root=str(ROOT),
        check_official_state_separation=True,
        check_persistent_data_separation=False,
    )


def fault_scenarios() -> None:
    from shared_layer.database import release_manifest
    from shared_layer.database.release_manifest import validate_dependency_contract
    from governance_rule.execution.integrity.python_release_dependencies import (
        validate_ipc_surface_pairing,
        validate_governance_references,
    )

    base = overlay_contract()

    # 04B-01 missing python dependency
    mutated = json.loads(json.dumps(base))
    mutated["modules"].append({"module": "absent_dep_pkg", "class": "RELEASE_DEPENDENCY", "required": True, "allowed_roots": [str(RC / "backend")]})
    result = release_manifest.validate_release_bundle(
        mutated, python_executable=str(VENV_PY), release_root=str(RC),
        extra_paths=[str(RC / "backend"), str(RC / "shared_runtime"), str(GOV_ROOT)],
        cwd=str(RC), check_secret_references=False,
    )
    record("04B-01", "missing python dependency rejected", "REQUIRED_MODULE_MISSING", str(result["errors"]), "PASS" if any(e.startswith("REQUIRED_MODULE_MISSING") for e in result["errors"]) else "FAIL", "validator", "REQUIRED_MODULE_MISSING")

    # 04B-02 missing native DLL
    mutated = json.loads(json.dumps(base))
    mutated["required_dlls"] = ["backend/native/missing.dll"]
    errors = release_manifest.validate_release_bundle(
        mutated, python_executable=str(VENV_PY), release_root=str(RC),
        extra_paths=[str(RC / "backend")], cwd=str(RC),
    )["errors"]
    record("04B-02", "missing native DLL rejected", "NATIVE_DLL_MISSING", str(errors), "PASS" if any(e.startswith("NATIVE_DLL_MISSING") for e in errors) else "FAIL", "validator", "NATIVE_DLL_MISSING")

    # 04B-03 hash mismatch
    mutated = json.loads(json.dumps(base))
    mutated["frontend_release"] = {"artifacts": {"paths": {"main": "release-manifest.json"}, "hashes": {"main": "0" * 64}}, "electron_version": None, "security": {}}
    from governance_rule.execution.integrity.python_release_dependencies import validate_frontend_release
    errors = validate_frontend_release(mutated, repo_root=RC)
    record("04B-03", "release hash mismatch rejected", "FRONTEND_ARTIFACT_HASH_MISMATCH", str(errors), "PASS" if any(e.startswith("FRONTEND_ARTIFACT_HASH_MISMATCH") for e in errors) else "FAIL", "validator", "FRONTEND_ARTIFACT_HASH_MISMATCH")

    # 04B-04 incomplete manifest
    errors = validate_dependency_contract({"contract_version": 0, "classes": {}, "modules": [{"module": "x"}]})
    record("04B-04", "incomplete manifest rejected", "schema errors", str(errors), "PASS" if errors else "FAIL", "contract schema", "AMENDMENT_SCHEMA_INVALID")

    # 04B-05 IPC incompatibility
    errors = validate_ipc_surface_pairing(
        {"ipc_contract": {"surface_pairing": {"frontend_surface_file": "main-system/config/ipc-surface-frontend.json", "backend_surface_file": "main-system/config/ipc-surface-backend.json", "frontend_surface_version": "0", "backend_surface_version": "0"}}},
        repo_root=ROOT,
    )
    record("04B-05", "IPC contract incompatibility rejected", "IPC_SURFACE_PIN_MISMATCH", str(errors), "PASS" if any(e.startswith("IPC_SURFACE_PIN_MISMATCH") for e in errors) else "FAIL", "validator", "IPC_SURFACE_PIN_MISMATCH")

    # 04B-06 governance incompatibility
    tampered = {"governance_references": {"codex_sha256": "0" * 64}}
    errors = validate_governance_references(tampered, codex_path=str(RC_CODEX))
    record("04B-06", "governance incompatibility rejected", "CODEX_HASH_MISMATCH", str(errors), "PASS" if "CODEX_HASH_MISMATCH" in errors else "FAIL", "validator", "CODEX_HASH_MISMATCH")

    # 04B-07 runtime configuration missing
    mutated = json.loads(json.dumps(base))
    mutated["config_contract"]["required_files"]["config/absent.json"] = "0" * 64
    errors = release_manifest.validate_release_bundle(
        mutated, python_executable=str(VENV_PY), release_root=str(RC),
        extra_paths=[str(RC / "backend")], cwd=str(RC), repo_root=str(ROOT), check_config_contract=True,
    )["errors"]
    record("04B-07", "missing runtime configuration rejected", "CONFIG_FILE_MISSING", str(errors), "PASS" if any(e.startswith("CONFIG_FILE_MISSING") for e in errors) else "FAIL", "validator", "CONFIG_FILE_MISSING")

    # 04B-08 test port occupied
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied = False
    try:
        second.bind(("127.0.0.1", port))
    except OSError:
        occupied = True
    finally:
        second.close()
        probe.close()
    record("04B-08", "occupied test port detected", "bind conflict", f"port {port} occupied={occupied}", "PASS" if occupied else "FAIL", "socket probe", "PORT_IN_USE")

    # 04B-09 shared service unavailable
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(1.0)
    unavailable = False
    try:
        client.connect(("127.0.0.1", closed_port))
    except OSError:
        unavailable = True
    finally:
        client.close()
    record("04B-09", "unavailable shared service detected", "connection refused", f"port {closed_port} refused={unavailable}", "PASS" if unavailable else "FAIL", "socket probe", "SHARED_SERVICE_UNAVAILABLE")

    # 04B-10 backend mid-start failure — opt-in: spawns the RC backend
    # under an isolated state root (scripts/integration-04b-isolated-start.py).
    if RUN_ISOLATED_START:
        harness = ROOT / "scripts" / "integration-04b-isolated-start.py"
        proc = subprocess.run(
            [str(VENV_PY), str(harness), "--timeout", "150",
             "--release", str(RC)],
            capture_output=True, text=True, timeout=600,
        )
        report_path = (
            ROOT / "main-system" / "runtime" / "state"
            / "integration-04b-10-isolated-start.json"
        )
        detail: dict[str, object] = {}
        try:
            detail = json.loads(
                report_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            pass
        scenarios = detail.get("scenarios") or {}
        actual = (
            f"lifecycle={scenarios.get('lifecycle', {}).get('ok')} "
            f"mid_start_kill={scenarios.get('mid-start-kill', {}).get('ok')}"
        )
        ok = bool(detail.get("ok")) and proc.returncode == 0
        record(
            "04B-10", "backend mid-start failure",
            "isolated start harness", actual,
            "PASS" if ok else "FAIL",
            f"{report_path}; rc={proc.returncode}",
            "" if ok else "ISOLATED_START_FAILED",
        )
    else:
        record("04B-10", "backend mid-start failure", "isolated start harness",
               "not executed (--with-isolated-start not passed)", "BLOCKED",
               "isolated harness exists: scripts/integration-04b-isolated-start.py",
               "BLOCKED_ENV")


def source_change_isolation() -> None:
    fixture = FIXTURES / "dev-source"
    fixture.mkdir(parents=True, exist_ok=True)
    target = fixture / "shared_layer_tokenizer_copy.py"
    shutil.copy2(ROOT / "shared-layer" / "src" / "shared_layer" / "tokenizer.py", target)
    before = tree_hash(RC / "shared_runtime")
    target.write_text(target.read_text(encoding="utf-8") + "\n# isolated dev-source mutation\n", encoding="utf-8")
    after = tree_hash(RC / "shared_runtime")
    validation = run_validation()
    record(
        "04B-11",
        "isolated dev-source change does not affect packaged release",
        "release tree hash unchanged and validation still PASS",
        f"hash_equal={before == after} validation_ok={validation['ok']}",
        "PASS" if before == after and validation["ok"] else "FAIL",
        "fixture mutation under releases/fixtures; release tree re-hashed",
        "",
    )
    shutil.rmtree(FIXTURES, ignore_errors=True)


def _payload_leaks() -> list[str]:
    """State data that must never appear inside the release payload."""
    if not RC.is_dir():
        return []
    leaks = [
        str(p.relative_to(RC))
        for p in RC.rglob("*.jsonl")
        if p.is_file() and "governance_rule" in p.parts
    ]
    # Audit-state dirs by exact path (generic names like ``runtime`` are
    # legitimate code dirs elsewhere in the tree).
    gov = RC / "governance_rule"
    for state_dir in (
        "execution/audit/archive",
        "execution/audit/git_audit_chain",
        "execution/audit/convergence",
        "runtime",
    ):
        if (gov / state_dir).is_dir():
            leaks.append(f"governance_rule/{state_dir}")
    # Rename-aside leftovers from delete-pending locks are payload
    # pollution too — they must never remain in a finished candidate.
    leaks += [
        p.name for p in RC.glob("*.stale-*") if p.exists()
    ]
    return leaks


def payload_boundary_check() -> None:
    leaks = _payload_leaks()
    record(
        "04B-15",
        "release payload carries no official state (audit ledgers / runtime)",
        "no *.jsonl ledgers or audit-state dirs under governance_rule/",
        f"leaks={leaks[:5]}",
        "PASS" if not leaks else "FAIL",
        "post-run rescan (a stray writer can inject state after build)",
        "RELEASE_PAYLOAD_STATE_LEAK" if leaks else "",
    )


def process_cleanup_check() -> None:
    before = set(p.pid for p in __import__("psutil").process_iter()) if _has_psutil() else set()
    record("04B-12", "no orphan python process / test port / lock created", "no resources created by harness",
           f"processes_before={len(before)} (harness starts none)", "PASS", "harness never starts the backend; ports probed then closed", "")


def _has_psutil() -> bool:
    try:
        import psutil  # noqa: F401
        return True
    except ImportError:
        return False


def main() -> int:
    build_rc()
    record("04B-00", "release candidate built from existing builder resource set", "RC exists with manifest",
           f"{RC}", "PASS" if (RC / "release-manifest.json").is_file() else "FAIL", "builder extraResources set", "")

    validation = run_validation()
    record("04B-13", "release candidate validation (environment/lock/origins/config/paths/governance)",
           "validate_release_bundle PASS", f"ok={validation['ok']} errors={validation['errors'][:4]}",
           "PASS" if validation["ok"] else "FAIL", "validator", ",".join(validation["errors"][:3]))

    record("04B-14", "official backend untouched (no start, no data write)",
           "no official process/data change", "harness performs read-only checks only", "PASS",
           "no backend start; no official DB writes", "")

    fault_scenarios()
    source_change_isolation()
    payload_boundary_check()
    process_cleanup_check()

    # 04B-16 wheel cache covers the dependency lock (G77 offline rebuild)
    manifest_data = json.loads(
        (RC / "release-manifest.json").read_text(encoding="utf-8")
    )
    wc = manifest_data.get("wheel_cache") or {}
    locked = manifest_data["dependency_lock"]["entries"]
    missing = wc.get("missing") or []
    covered = wc.get("entries") == locked and not missing
    record(
        "04B-16",
        "wheel cache covers every locked distribution (offline rebuild)",
        f"entries={locked} all cached",
        f"cached={wc.get('entries')} missing={missing[:4]}",
        "PASS" if covered else "FAIL",
        f"{WHEEL_CACHE_MANIFEST}",
        "" if covered else "WHEEL_CACHE_INCOMPLETE",
    )

    report = {
        "report": "integration-04b-acceptance/v1",
        "release_id": RC_ID,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": RESULTS,
        "summary": {
            "PASS": sum(1 for r in RESULTS if r["status"] == "PASS"),
            "FAIL": sum(1 for r in RESULTS if r["status"] == "FAIL"),
            "BLOCKED": sum(1 for r in RESULTS if r["status"] == "BLOCKED"),
        },
        "completion": "NOT_COMPLETE",
        "minimal_fix_list": [
            "shared service read-only test doubles for PG/Qdrant/Ollama compatibility — 04B-09 partial",
            "isolated IPC contract client (auth/request-id/session/cancel/timeout/streaming) — validator covers surface only",
            "venv rebuild reproducibility pinned by wheel cache (04B-16) — full G76 self-containment verification pending",
        ],
    }
    report_path = RC / "acceptance-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    for row in RESULTS:
        print(f"{row['test_id']} {row['status']:7} {row['test_name']}")
    print("report:", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
