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
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[5]
MAIN_SYSTEM_ROOT = PROJECT_ROOT / "main-system"

# Windows: suppress console window for background subprocess calls
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


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
    """Package a platform tool into a distributable archive.

    This is a thin integration shim that delegates to the main-system
    central platform packager under governance authorization.
    """
    return {
        "ok": False,
        "tool_id": tool_id,
        "error_code": "NOT_IMPLEMENTED",
        "message": "platform_packager integration pending main-system delegation",
    }


def verify_packaged_tool(package_path: Path) -> dict[str, Any]:
    """Verify the integrity of a packaged tool archive."""
    if not package_path.is_file():
        return {
            "ok": False,
            "error_code": "PACKAGE_NOT_FOUND",
            "message": str(package_path),
        }
    return {
        "ok": True,
        "package_path": str(package_path),
        "size_bytes": package_path.stat().st_size,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="system-rescue platform packager")
    parser.add_argument("--all", action="store_true", help="process all tools")
    parser.add_argument("--tool", default="", help="specific tool id")
    parser.add_argument("--verify", action="store_true", help="verify package integrity")
    parser.add_argument("--package", action="store_true", help="create package")
    parser.add_argument("--json", action="store_true", help="output as JSON")
    args = parser.parse_args(argv)

    result: dict[str, Any]
    if args.all and args.verify:
        result = verify_all_packages()
    elif args.tool and args.verify:
        result = _verify_tool_package(args.tool)
    elif args.tool and args.package:
        result = package_platform_tool(args.tool)
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


__all__ = ["package_platform_tool", "verify_packaged_tool", "verify_all_packages"]
