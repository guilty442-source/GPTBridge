from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packager_base import (
    PACKAGE_METADATA_NAME,
    PROJECT_ROOT,
    load_package_metadata,
    load_tool_runtime_contract,
    verify_packaged_app,
)
from packager_bundle import package_source_roots
from packager_metadata import (
    resolve_entry,
    resolve_executable_name,
    standalone_backend_port,
    validate_tool_version_baseline,
)

from core_system.versioning import component_version

_CENTRAL_VERSION = component_version("packager")


def verify_tool_package(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    version_check = validate_tool_version_baseline(tool_id, manifest)
    if not version_check["ok"]:
        return version_check
    executable_name = resolve_executable_name(tool_id, manifest)
    dist_dir = tool_dir / "dist"
    exe_path = dist_dir / f"{executable_name}.exe"
    app_dir = dist_dir / "resources" / "app"
    if not exe_path.exists():
        return {
            "ok": False,
            "tool_id": tool_id,
            "error_code": "PACKAGE_MISSING",
            "message": f"Standalone executable not found: {exe_path}",
        }
    result = verify_packaged_app(app_dir, project_root=PROJECT_ROOT)
    if not result.get("ok"):
        return {
            **result,
            "tool_id": tool_id,
            "exe_path": str(exe_path),
        }

    metadata = load_package_metadata(app_dir)
    try:
        app_manifest = json.loads(
            (app_dir / "manifest.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        return {
            **result,
            "ok": False,
            "tool_id": tool_id,
            "exe_path": str(exe_path),
            "error_code": "PACKAGE_SEMANTICS_INVALID",
            "message": f"Standalone app manifest is invalid: {error}",
        }
    standalone = (
        app_manifest.get("standalone")
        if isinstance(app_manifest, dict)
        else None
    )
    expected_port = standalone_backend_port(tool_id)
    expected_version = str(manifest.get("version") or _CENTRAL_VERSION)
    request_channel = manifest.get("request_channel")
    governed_channel = (
        isinstance(request_channel, dict)
        and request_channel.get("model") == "governance-authenticated-shared-layer"
    )
    channel_runtime_entry = (
        str(request_channel.get("runtime_entry") or "").strip()
        if isinstance(request_channel, dict)
        else ""
    )
    expected_backend_entry = (
        f"independent_tool/{tool_id}/{channel_runtime_entry}"
        if governed_channel
        else "src-core/main.py"
    )
    runtime_contract = load_tool_runtime_contract()
    expected_protocol_version = runtime_contract["protocol_version"]
    expected_contract_version = runtime_contract["contract_version"]
    expected_python_runtime = "python/python.exe"
    semantic_errors: list[str] = []
    expected_source_roots = package_source_roots(
        tool_dir,
        resolve_entry(tool_dir, manifest),
    )
    packaged_source_roots = metadata.get("source_roots")
    normalized_packaged_roots = (
        sorted(str(item) for item in packaged_source_roots)
        if isinstance(packaged_source_roots, list)
        else []
    )
    if expected_source_roots and (
        not normalized_packaged_roots
        or not set(expected_source_roots).issubset(normalized_packaged_roots)
    ):
        semantic_errors.append(
            "package source roots do not match the current packaging contract"
        )
    if not isinstance(app_manifest, dict) or not isinstance(standalone, dict):
        semantic_errors.append("app manifest has no standalone configuration")
    else:
        if str(app_manifest.get("id") or "") != tool_id:
            semantic_errors.append("app manifest tool_id does not match")
        if str(app_manifest.get("version") or "") != expected_version:
            semantic_errors.append("app manifest version does not match")
        if (
            type(standalone.get("backend_port")) is not int
            or standalone.get("backend_port") != expected_port
        ):
            semantic_errors.append(
                "app manifest backend_port is not deterministic"
            )
        if standalone.get("isolated_backend") is not True:
            semantic_errors.append(
                "app manifest isolated_backend must be true"
            )
        if standalone.get("backend_entry") != expected_backend_entry:
            semantic_errors.append("app manifest backend_entry does not match")
        if standalone.get("governed_channel") != (
            "shared-layer" if governed_channel else ""
        ):
            semantic_errors.append("app manifest governed channel does not match")
        if (
            type(standalone.get("protocol_version")) is not int
            or standalone.get("protocol_version")
            != expected_protocol_version
        ):
            semantic_errors.append(
                "app manifest protocol_version does not match"
            )
        if standalone.get("python_runtime") != expected_python_runtime:
            semantic_errors.append(
                "app manifest python_runtime does not match"
            )
        if (
            int(standalone.get("runtime_contract_version") or 1)
            != expected_contract_version
        ):
            semantic_errors.append(
                "app manifest runtime contract version does not match"
            )
        if (
            str(standalone.get("backend_service_version") or "")
            != expected_version
        ):
            semantic_errors.append(
                "app manifest backend service version does not match"
            )

    if str(metadata.get("tool_id") or "") != tool_id:
        semantic_errors.append("package metadata tool_id does not match")
    if str(manifest.get("id") or tool_id) != tool_id:
        semantic_errors.append("source manifest tool_id does not match")
    if str(metadata.get("tool_version") or "") != expected_version:
        semantic_errors.append("package metadata tool version does not match")
    if (
        str(metadata.get("backend_service_version") or "")
        != expected_version
    ):
        semantic_errors.append(
            "package metadata backend service version does not match"
        )
    if (
        type(metadata.get("backend_port")) is not int
        or metadata.get("backend_port") != expected_port
    ):
        semantic_errors.append(
            "package metadata backend_port is not deterministic"
        )
    if metadata.get("isolated_backend") is not True:
        semantic_errors.append("package metadata isolated_backend must be true")
    if metadata.get("backend_entry") != expected_backend_entry:
        semantic_errors.append("package metadata backend_entry does not match")
    if metadata.get("governed_channel") != (
        "shared-layer" if governed_channel else ""
    ):
        semantic_errors.append("package metadata governed channel does not match")
    if (
        type(metadata.get("protocol_version")) is not int
        or metadata.get("protocol_version") != expected_protocol_version
    ):
        semantic_errors.append(
            "package metadata protocol_version does not match"
        )
    if metadata.get("python_runtime") != expected_python_runtime:
        semantic_errors.append("package metadata python_runtime does not match")
    if (
        int(metadata.get("runtime_contract_version") or 1)
        != expected_contract_version
    ):
        semantic_errors.append(
            "package metadata runtime contract version does not match"
        )

    if isinstance(standalone, dict):
        paired_fields = (
            "backend_port",
            "isolated_backend",
            "backend_entry",
            "governed_channel",
            "protocol_version",
            "runtime_contract_version",
            "python_runtime",
            "backend_service_version",
        )
        if any(
            metadata.get(field) != standalone.get(field)
            for field in paired_fields
        ):
            semantic_errors.append(
                "package metadata and app manifest standalone settings differ"
            )
    if not (app_dir / expected_backend_entry).is_file():
        semantic_errors.append("packaged backend entry is missing")
    if not (app_dir / expected_python_runtime).is_file():
        semantic_errors.append("packaged Python runtime is missing")

    if semantic_errors:
        return {
            **result,
            "ok": False,
            "tool_id": tool_id,
            "exe_path": str(exe_path),
            "error_code": "PACKAGE_SEMANTICS_INVALID",
            "message": "; ".join(dict.fromkeys(semantic_errors)),
        }
    return {
        **result,
        "tool_id": tool_id,
        "exe_path": str(exe_path),
    }


def verify_upgrade_result(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Verify a promoted package and retry one safe rebuild if necessary."""

    if not result.get("ok"):
        return result
    verification = verify_tool_package(tool_id, tool_dir, manifest)
    if verification.get("ok"):
        return {
            **result,
            "upgrade_auto_retry": False,
            "post_verification": verification,
        }

    from packager_orchestration import package_tool

    retry = package_tool(tool_id, tool_dir, manifest)
    retry["upgrade_auto_retry"] = True
    retry["initial_post_verification"] = verification
    if not retry.get("ok"):
        return retry

    final_verification = verify_tool_package(tool_id, tool_dir, manifest)
    retry["post_verification"] = final_verification
    if final_verification.get("ok"):
        return retry
    return {
        **retry,
        "ok": False,
        "error_code": "UPGRADE_POST_VERIFY_FAILED",
        "message": str(
            final_verification.get("message")
            or "Package verification failed after automatic retry"
        ),
    }
