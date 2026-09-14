"""system-rescue platform packager integration.

Provides the platform packaging integration point for the system-rescue tool,
which handles central packaging operations under main-system governance.

CLI usage:
    python platform_packager.py --all --verify --json
    python platform_packager.py --tool <tool_id> --verify --json
    python platform_packager.py --tool <tool_id> --package --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

try:  # information-layer-owned process adapter (A447)
    from shared_layer.process_control import GovernedProcessAdapter
except ImportError:  # pragma: no cover - fail closed when the layer is absent
    GovernedProcessAdapter = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[5]
MAIN_SYSTEM_ROOT = PROJECT_ROOT / "main-system"
PACKAGER_CLI = MAIN_SYSTEM_ROOT / "src-core" / "tasks" / "packager_main.py"

_PACKAGE_TIMEOUT_SECONDS = 1800
_VERIFY_TIMEOUT_SECONDS = 600

_PACKAGER_ADAPTER: Any | None = None


def _packager_adapter() -> Any | None:
    """Information-layer-owned adapter restricted to the packager interpreter."""
    global _PACKAGER_ADAPTER
    if GovernedProcessAdapter is None:
        return None
    if _PACKAGER_ADAPTER is None:
        _PACKAGER_ADAPTER = GovernedProcessAdapter([_packager_python()])
    return _PACKAGER_ADAPTER


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _packager_python() -> Path:
    venv_python = MAIN_SYSTEM_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        return venv_python
    return Path(sys.executable).resolve()


def _run_packager_cli(
    arguments: list[str],
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Delegate to the main-system central packager under governance.

    A447: the cross-process call runs through the information-layer-owned
    ``GovernedProcessAdapter`` (allowlisted interpreter + bounded timeout +
    audit record), never through a direct subprocess control channel.
    """
    if not PACKAGER_CLI.is_file():
        return {
            "ok": False,
            "error_code": "PACKAGER_MISSING",
            "message": f"central packager not found: {PACKAGER_CLI}",
        }
    adapter = _packager_adapter()
    if adapter is None:
        return {
            "ok": False,
            "error_code": "INFORMATION_LAYER_UNAVAILABLE",
            "message": "governed process adapter is unavailable",
        }
    environment = dict(os.environ)
    environment.update(
        {
            "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT": str(PROJECT_ROOT),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    report = adapter.run_json(
        [
            os.fspath(_packager_python()),
            "-B",
            "-s",
            os.fspath(PACKAGER_CLI),
            *arguments,
            "--json",
        ],
        requester="system-rescue/platform-packager",
        permission_token="system-rescue-packager-token",
        timeout_seconds=float(timeout_seconds),
        cwd=PACKAGER_CLI.parent,
        environment=environment,
    )
    error_code = report.get("error_code")
    if error_code == "PROCESS_TIMEOUT":
        return {
            "ok": False,
            "error_code": "PACKAGER_TIMEOUT",
            "message": f"central packager timed out after {timeout_seconds}s",
            "audit": report.get("audit"),
        }
    if error_code == "PROCESS_LAUNCH_FAILED":
        return {
            "ok": False,
            "error_code": "PACKAGER_LAUNCH_FAILED",
            "message": report.get("message", ""),
            "audit": report.get("audit"),
        }
    if error_code == "PROCESS_OUTPUT_INVALID":
        report["error_code"] = "PACKAGER_OUTPUT_INVALID"
    return report


def _discover_packaged_tools() -> list[str]:
    """Discover all tools that have a packaged executable."""
    release_dir = PROJECT_ROOT / "release" / "child-tools"
    if not release_dir.is_dir():
        return []
    tools: list[str] = []
    for candidate in sorted(release_dir.iterdir()):
        if candidate.is_dir():
            tools.append(candidate.name)
    return tools


def _verify_tool_package(tool_id: str) -> dict[str, Any]:
    """Verify the integrity of a single packaged tool."""
    release_dir = PROJECT_ROOT / "release" / "child-tools" / tool_id
    if not release_dir.is_dir():
        return {
            "tool_id": tool_id,
            "ok": False,
            "error_code": "PACKAGE_MISSING",
            "message": f"no packaged release for {tool_id}",
        }
    metadata_path = release_dir / "package-metadata.json"
    if not metadata_path.is_file():
        return {
            "tool_id": tool_id,
            "ok": False,
            "error_code": "PACKAGE_METADATA_MISSING",
            "message": f"package metadata missing for {tool_id}",
        }
    metadata = _load_json(metadata_path)
    if not metadata:
        return {
            "tool_id": tool_id,
            "ok": False,
            "error_code": "PACKAGE_METADATA_INVALID",
            "message": f"package metadata invalid for {tool_id}",
        }
    # Check if source is newer than package (stale check)
    source_manifest = None
    for candidate in (
        PROJECT_ROOT / tool_id / "manifest.json",
        PROJECT_ROOT / tool_id.replace("-", "_") / "manifest.json",
    ):
        if candidate.is_file():
            source_manifest = candidate
            break
    if source_manifest is None:
        # Search nested manifests
        for nested in PROJECT_ROOT.glob(f"*/manifest.json"):
            try:
                doc = json.loads(nested.read_text("utf-8"))
                if doc.get("id") == tool_id:
                    source_manifest = nested
                    break
            except (OSError, json.JSONDecodeError):
                continue
    if source_manifest is not None:
        source_mtime = source_manifest.stat().st_mtime
        pkg_mtime = metadata_path.stat().st_mtime
        if source_mtime > pkg_mtime:
            return {
                "tool_id": tool_id,
                "ok": False,
                "error_code": "STALE_PACKAGE",
                "message": f"source is newer than package for {tool_id}",
            }
    return {
        "tool_id": tool_id,
        "ok": True,
        "message": f"package verified for {tool_id}",
    }


def verify_all_packages() -> dict[str, Any]:
    """Verify all packaged tools."""
    tools = _discover_packaged_tools()
    if not tools:
        return {
            "ok": True,
            "results": [],
            "message": "no packaged tools to verify",
        }
    results = [_verify_tool_package(tid) for tid in tools]
    all_ok = all(r.get("ok") for r in results)
    return {
        "ok": all_ok,
        "results": results,
        "message": "all packaged tools verified" if all_ok else "some packages failed verification",
    }


def package_platform_tool(
    tool_id: str,
    output_dir: Path | None = None,
    *,
    skip_integrity: bool = False,
) -> dict[str, Any]:
    """Package a platform tool via the main-system central packager.

    system-rescue is the governed integration point for platform packaging;
    the actual assembly is delegated to ``main-system``'s central packager
    CLI, which owns Electron/runtime staging and upgrade verification.
    ``output_dir``/``skip_integrity`` are accepted for interface parity; the
    central packager owns its own output layout and integrity checks.
    """
    del output_dir, skip_integrity
    tool_id = str(tool_id or "").strip()
    if not tool_id:
        return {
            "ok": False,
            "error_code": "TOOL_ID_REQUIRED",
            "message": "tool_id is required",
        }
    report = _run_packager_cli([tool_id], timeout_seconds=_PACKAGE_TIMEOUT_SECONDS)
    results = report.get("results")
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict) and entry.get("tool_id") == tool_id:
                merged = dict(entry)
                merged.setdefault("ok", report.get("ok"))
                merged["delegated_to"] = "main-system-central-packager"
                return merged
    report.setdefault("tool_id", tool_id)
    report["delegated_to"] = "main-system-central-packager"
    return report


def deep_verify_tool_package(tool_id: str) -> dict[str, Any]:
    """Run the central packager's real verification for one tool."""
    tool_id = str(tool_id or "").strip()
    if not tool_id:
        return {
            "ok": False,
            "error_code": "TOOL_ID_REQUIRED",
            "message": "tool_id is required",
        }
    report = _run_packager_cli(
        [tool_id, "--verify"], timeout_seconds=_VERIFY_TIMEOUT_SECONDS
    )
    results = report.get("results")
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict) and entry.get("tool_id") == tool_id:
                merged = dict(entry)
                merged.setdefault("ok", report.get("ok"))
                merged["delegated_to"] = "main-system-central-packager"
                return merged
    report.setdefault("tool_id", tool_id)
    report["delegated_to"] = "main-system-central-packager"
    return report


def verify_packaged_tool(package_path: Path) -> dict[str, Any]:
    """Verify the integrity of a packaged tool archive.

    Besides existence/size, a ``<package>.sha256`` sidecar (hex digest,
    optionally followed by whitespace and a filename as produced by
    ``certutil``/``sha256sum``) is honored when present.
    """
    if not package_path.is_file():
        return {
            "ok": False,
            "error_code": "PACKAGE_NOT_FOUND",
            "message": str(package_path),
        }
    result: dict[str, Any] = {
        "ok": True,
        "package_path": str(package_path),
        "size_bytes": package_path.stat().st_size,
    }
    sidecar = package_path.with_name(package_path.name + ".sha256")
    if sidecar.is_file():
        try:
            expected = sidecar.read_text(encoding="utf-8").split()[0].strip().lower()
            actual = hashlib.sha256(package_path.read_bytes()).hexdigest()
        except (OSError, IndexError) as error:
            return {
                "ok": False,
                "error_code": "PACKAGE_CHECKSUM_UNREADABLE",
                "message": str(error),
            }
        result["sha256"] = actual
        result["sha256_expected"] = expected
        if expected != actual:
            result["ok"] = False
            result["error_code"] = "PACKAGE_CHECKSUM_MISMATCH"
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="system-rescue platform packager")
    parser.add_argument("--all", action="store_true", help="process all tools")
    parser.add_argument("--tool", default="", help="specific tool id")
    parser.add_argument("--verify", action="store_true", help="verify package integrity")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="delegate verification to the main-system central packager",
    )
    parser.add_argument("--package", action="store_true", help="create package")
    parser.add_argument("--json", action="store_true", help="output as JSON")
    args = parser.parse_args(argv)

    result: dict[str, Any]
    if args.all and args.verify:
        result = verify_all_packages()
    elif args.tool and args.verify and args.deep:
        result = deep_verify_tool_package(args.tool)
    elif args.tool and args.verify:
        result = _verify_tool_package(args.tool)
    elif args.tool and args.package:
        result = package_platform_tool(args.tool)
    elif args.all and args.package:
        result = _run_packager_cli(
            ["--all"], timeout_seconds=_PACKAGE_TIMEOUT_SECONDS
        )
    elif args.all:
        result = verify_all_packages()
    else:
        result = {
            "ok": False,
            "error_code": "INVALID_ARGS",
            "message": "specify --all or --tool, with --verify or --package",
        }

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_main())


__all__ = [
    "deep_verify_tool_package",
    "package_platform_tool",
    "verify_all_packages",
    "verify_packaged_tool",
]
