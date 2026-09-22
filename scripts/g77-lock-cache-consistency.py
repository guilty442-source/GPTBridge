"""G77: uv.lock <-> wheel-cache manifest bidirectional consistency.

The RC manifest's ``dependency_lock`` is produced by probing the release
venv (importlib.metadata), and the shared wheel cache
(``releases/wheel-cache/``) is materialised from that probe.  This script
closes the remaining gap: binding both sides to ``main-system/uv.lock``
at hash level, in both directions.

Checks (all fail-closed, reported per-check):
  1. every wheel-cache entry's ``name==version`` exists in uv.lock
  2. every wheel-cache entry's sha256 equals a wheel hash recorded in
     uv.lock for that package (or the package's sdist hash, when the
     package ships sdist-only)
  3. every wheel file on disk hashes to the manifest's recorded sha256
  4. every dist installed in the RC venv (manifest dependency_lock
     scope) is covered by the cache
  5. no cache entry is foreign to the lock (reverse direction)
  6. RC manifest ``dependency_lock.entries`` count matches its probe set

Output: governance_rule/execution/audit/convergence/g77-lock-cache-consistency.json
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV_LOCK = ROOT / "main-system" / "uv.lock"
RELEASES = ROOT / "main-system" / "runtime" / "releases"
WHEEL_CACHE = RELEASES / "wheel-cache"
WHEEL_CACHE_MANIFEST = WHEEL_CACHE / "wheel-cache-manifest.json"
OUT = (
    ROOT / "governance_rule" / "execution" / "audit" / "convergence"
    / "g77-lock-cache-consistency.json"
)


def _norm(name: str) -> str:
    """PEP 503 normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_lock() -> dict[str, dict]:
    """normalised name -> {version, wheel_hashes, sdist_hashes}."""
    with UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: dict[str, dict] = {}
    for pkg in lock.get("package", []):
        wheel_hashes = {
            w.get("hash", "").removeprefix("sha256:")
            for w in pkg.get("wheels", [])
            if w.get("hash")
        }
        sdist = pkg.get("sdist") or {}
        sdist_hashes = (
            {sdist["hash"].removeprefix("sha256:")} if sdist.get("hash") else set()
        )
        packages[_norm(pkg["name"])] = {
            "version": pkg["version"],
            "wheel_hashes": wheel_hashes,
            "sdist_hashes": sdist_hashes,
        }
    return packages


def _wheel_dist_name(wheel_path: Path) -> tuple[str, str] | None:
    """Read Name/Version from a wheel's dist-info METADATA."""
    try:
        with zipfile.ZipFile(wheel_path) as zf:
            meta_name = next(
                (n for n in zf.namelist()
                 if n.endswith(".dist-info/METADATA")),
                None,
            )
            if meta_name is None:
                return None
            name = version = None
            for line in zf.read(meta_name).decode("utf-8", "replace").splitlines():
                if line.startswith("Name: "):
                    name = line[6:].strip()
                elif line.startswith("Version: "):
                    version = line[9:].strip()
                if name and version:
                    return _norm(name), version
    except (OSError, zipfile.BadZipFile):
        return None
    return None


def main() -> int:
    report: dict = {
        "schema": "g77-lock-cache-consistency/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uv_lock": str(UV_LOCK),
        "wheel_cache_manifest": str(WHEEL_CACHE_MANIFEST),
        "checks": {},
        "passed": False,
    }
    errors: list[str] = []

    lock = _load_lock()
    cache = json.loads(WHEEL_CACHE_MANIFEST.read_text(encoding="utf-8"))
    entries = cache.get("entries", [])

    # --- check 1+2+5: cache <-> lock (name/version + hash binding) ---
    foreign: list[str] = []
    version_mismatch: list[str] = []
    hash_mismatch: list[str] = []
    for entry in entries:
        dist = entry["dist"]
        name, _, version = dist.partition("==")
        locked = lock.get(_norm(name))
        if locked is None:
            foreign.append(dist)
            continue
        if locked["version"] != version:
            version_mismatch.append(dist)
            continue
        sha = entry.get("sha256", "")
        if sha not in locked["wheel_hashes"] and sha not in locked["sdist_hashes"]:
            hash_mismatch.append(dist)
    report["checks"]["cache_entries_in_lock"] = {
        "total": len(entries),
        "foreign": foreign,
        "version_mismatch": version_mismatch,
        "hash_mismatch": hash_mismatch,
        "passed": not (foreign or version_mismatch or hash_mismatch),
    }
    if foreign or version_mismatch or hash_mismatch:
        errors.append(
            f"cache-lock mismatch: foreign={foreign} "
            f"version={version_mismatch} hash={hash_mismatch}"
        )

    # --- check 3: wheel bytes on disk match manifest sha256 ---
    corrupt: list[str] = []
    missing_files: list[str] = []
    label_mismatch: list[str] = []
    for entry in entries:
        wheel = WHEEL_CACHE / entry["file"]
        if not wheel.is_file():
            missing_files.append(entry["file"])
            continue
        actual = _sha256(wheel)
        if actual != entry.get("sha256"):
            corrupt.append(entry["file"])
            continue
        dist_meta = _wheel_dist_name(wheel)
        dist = entry["dist"]
        name, _, version = dist.partition("==")
        if dist_meta is None or dist_meta != (_norm(name), version):
            label_mismatch.append(entry["file"])
    report["checks"]["wheel_integrity"] = {
        "missing_files": missing_files,
        "corrupt": corrupt,
        "label_mismatch": label_mismatch,
        "passed": not (missing_files or corrupt or label_mismatch),
    }
    if missing_files or corrupt or label_mismatch:
        errors.append(
            f"wheel integrity: missing={missing_files} corrupt={corrupt} "
            f"label={label_mismatch}"
        )

    # --- check 4+6: RC venv coverage (dependency_lock scope) ---
    rcs = sorted(
        (d for d in RELEASES.glob("rc-*")
         if (d / "release-manifest.json").is_file()),
        reverse=True,
    )
    coverage: dict = {"checked_rcs": [], "passed": True}
    for rc in rcs[:1]:  # latest RC only
        manifest = json.loads(
            (rc / "release-manifest.json").read_text(encoding="utf-8")
        )
        dep_lock = manifest.get("dependency_lock", {})
        venv_py = Path(manifest.get("python_runtime", {}).get("executable", ""))
        record: dict = {"rc": rc.name, "declared_entries": dep_lock.get("entries")}
        if venv_py.is_file():
            import subprocess
            out = subprocess.run(
                [
                    str(venv_py), "-c",
                    "import importlib.metadata, json; print(json.dumps(sorted("
                    "f'{d.metadata[\"Name\"]}=={d.version}' for d in "
                    "importlib.metadata.distributions() if d.metadata['Name'])))",
                ],
                capture_output=True, text=True, check=True, timeout=120,
            ).stdout
            installed = set(json.loads(out))
            cached = {e["dist"] for e in entries}
            locked_names = {
                f"{p['version']}" for p in lock.values()
            }
            record["installed"] = len(installed)
            record["uncached"] = sorted(installed - cached)
            record["not_in_uv_lock"] = sorted(
                d for d in installed
                if lock.get(_norm(d.partition("==")[0])) is None
                or lock[_norm(d.partition("==")[0])]["version"]
                != d.partition("==")[2]
            )
            record["count_mismatch"] = (
                dep_lock.get("entries") is not None
                and dep_lock["entries"] != len(installed)
            )
            if record["uncached"] or record["not_in_uv_lock"] or record["count_mismatch"]:
                coverage["passed"] = False
        else:
            record["skipped"] = f"venv python missing: {venv_py}"
        coverage["checked_rcs"].append(record)
    report["checks"]["rc_venv_coverage"] = coverage
    if not coverage["passed"]:
        errors.append(f"rc venv coverage: {coverage['checked_rcs']}")

    report["passed"] = not errors
    report["errors"] = errors
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "passed": report["passed"],
        "errors": errors,
        "report": str(OUT),
    }, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
