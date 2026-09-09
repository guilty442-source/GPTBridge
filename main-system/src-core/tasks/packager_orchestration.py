from __future__ import annotations

import json
import shutil
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packager_base import (
    ELECTRON_DIST_DIR,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_METADATA_NAME,
    PROJECT_ROOT,
    PromotionRecoveryRequired,
    SOURCE_IGNORED_DIRECTORY_NAMES,
    collect_file_hashes,
    snapshot_digest,
    verify_packaged_app,
    load_tool_runtime_contract,
)
from packager_bundle import (
    copy_backend_source_bundle,
    package_source_excluded_paths,
    package_source_roots,
)
from packager_locking import package_operation_lock
from packager_base import PackageOperationBusy
from packager_metadata import (
    resolve_entry,
    resolve_executable_name,
    standalone_backend_port,
    validate_tool_version_baseline,
)
from packager_processes import (
    restart_packaged_executable,
    running_executable_process_ids,
    stop_running_executable_for_upgrade,
    stop_verified_packaged_backend,
)
from packager_recovery import prune_completed_recovery_roots
from packager_renderer import build_platform_renderer, copy_app_templates
from packager_runtime import (
    copy_electron_runtime,
    copy_portable_python_runtime,
    validate_staged_python_runtime,
)
from packager_distribution import promote_staged_distribution


def package_tool(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    try:
        with package_operation_lock(tool_id, tool_dir):
            return _package_tool_locked(tool_id, tool_dir, manifest)
    except PackageOperationBusy as error:
        return {
            "ok": False,
            "tool_id": tool_id,
            "error_code": "PACKAGE_OPERATION_ACTIVE",
            "message": str(error),
        }
    except Exception as error:
        return {
            "ok": False,
            "tool_id": tool_id,
            "error_code": "PACKAGE_LOCK_FAILED",
            "message": str(error),
        }


def _package_tool_locked(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    version_check = validate_tool_version_baseline(tool_id, manifest)
    if not version_check["ok"]:
        return version_check
    entry = resolve_entry(tool_dir, manifest)
    backend_port = standalone_backend_port(tool_id)
    runtime_contract = load_tool_runtime_contract()
    if not entry.exists() or entry.suffix.lower() != ".py":
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": f"Python runtime entry not found: {entry}",
        }

    electron_exe = ELECTRON_DIST_DIR / "electron.exe"
    if not electron_exe.exists():
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": f"Electron runtime not found: {electron_exe}",
        }

    renderer_result = build_platform_renderer(tool_id)
    if not renderer_result.get("ok"):
        return {
            "ok": False,
            "tool_id": tool_id,
            "entry": str(entry),
            "message": "Platform renderer build failed",
            "renderer_output": renderer_result.get("output", ""),
        }

    renderer_dir = Path(str(renderer_result["renderer_path"]))
    executable_name = resolve_executable_name(tool_id, manifest)
    dist_dir = tool_dir / "dist"
    exe_path = dist_dir / f"{executable_name}.exe"
    running_process_ids = running_executable_process_ids(exe_path)
    restart_after_upgrade = bool(running_process_ids)
    desktop_stopped_for_upgrade = False
    desktop_restarted = False

    package_root = tool_dir / "build" / f"package-{uuid.uuid4().hex}"
    staged_dist = package_root / "dist"
    staged_exe = staged_dist / f"{executable_name}.exe"
    preserve_package_root = False
    promotion_recovery_root: Path | None = None

    try:
        source_roots = package_source_roots(tool_dir, entry)
        source_excluded_paths = package_source_excluded_paths(tool_dir, entry)
        source_excluded_path_set = frozenset(source_excluded_paths)
        source_files = collect_file_hashes(
            PROJECT_ROOT,
            source_roots,
            ignored_directory_names=SOURCE_IGNORED_DIRECTORY_NAMES,
            excluded_relative_paths=source_excluded_path_set,
        )
        source_digest = snapshot_digest(source_files)
        copy_electron_runtime(staged_dist, staged_exe)

        app_dir = staged_dist / "resources" / "app"
        app_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(renderer_dir, app_dir / "renderer")
        runtime_path = copy_backend_source_bundle(
            tool_id,
            tool_dir,
            entry,
            app_dir,
        )
        python_runtime_path = copy_portable_python_runtime(app_dir)

        request_channel = manifest.get("request_channel")
        governed_channel = (
            isinstance(request_channel, dict)
            and request_channel.get("model")
            == "governance-authenticated-shared-layer"
        )
        channel_runtime_entry = (
            str(request_channel.get("runtime_entry") or "").strip()
            if isinstance(request_channel, dict)
            else ""
        )
        backend_entry_relative = (
            f"independent_tool/{tool_id}/{channel_runtime_entry}"
            if governed_channel
            else "src-core/main.py"
        )
        if governed_channel and (
            not channel_runtime_entry
            or Path(channel_runtime_entry).is_absolute()
            or ".." in Path(channel_runtime_entry).parts
        ):
            raise RuntimeError("Invalid governed request channel runtime entry")

        app_manifest = dict(manifest)
        app_manifest["id"] = tool_id
        app_manifest["version"] = str(manifest.get("version", "1.00000"))
        app_manifest["standalone"] = {
            "backend_entry": backend_entry_relative,
            "backend_service_version": str(manifest.get("version", "1.0.0")),
            "backend_port": backend_port,
            "isolated_backend": True,
            "protocol_version": runtime_contract["protocol_version"],
            "runtime_contract_version": runtime_contract["contract_version"],
            "python_runtime": "python/python.exe",
            "governed_channel": "shared-layer" if governed_channel else "",
        }
        (app_dir / "manifest.json").write_text(
            json.dumps(app_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (app_dir / "package.json").write_text(
            json.dumps(
                {
                    "name": f"gptbridge-tool-{tool_id}",
                    "version": str(manifest.get("version", "1.0.0")),
                    "main": "main.cjs",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        copy_app_templates(app_dir)
        validate_staged_python_runtime(
            app_dir,
            tool_id,
            backend_entry_relative,
        )

        current_source_files = collect_file_hashes(
            PROJECT_ROOT,
            source_roots,
            ignored_directory_names=SOURCE_IGNORED_DIRECTORY_NAMES,
            excluded_relative_paths=source_excluded_path_set,
        )
        if current_source_files != source_files:
            raise RuntimeError(
                "Package inputs changed during the build; retry after edits finish"
            )

        payload_files = collect_file_hashes(
            app_dir,
            ["."],
            excluded_relative_paths=frozenset({PACKAGE_METADATA_NAME}),
        )
        package_metadata = {
            "format_version": PACKAGE_FORMAT_VERSION,
            "tool_id": tool_id,
            "tool_version": str(manifest.get("version", "1.0.0")),
            "backend_service_version": str(manifest.get("version", "1.0.0")),
            "backend_port": backend_port,
            "isolated_backend": True,
            "protocol_version": runtime_contract["protocol_version"],
            "runtime_contract_version": runtime_contract["contract_version"],
            "backend_entry": backend_entry_relative,
            "python_runtime": "python/python.exe",
            "governed_channel": "shared-layer" if governed_channel else "",
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_roots": source_roots,
            "source_excluded_paths": source_excluded_paths,
            "source_files": source_files,
            "source_digest": source_digest,
            "payload_files": payload_files,
            "payload_digest": snapshot_digest(payload_files),
        }
        (app_dir / PACKAGE_METADATA_NAME).write_text(
            json.dumps(package_metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        staged_verification = verify_packaged_app(
            app_dir,
            project_root=PROJECT_ROOT,
        )
        if not staged_verification.get("ok"):
            raise RuntimeError(
                f"Staged package verification failed: "
                f"{staged_verification.get('message', 'unknown error')}"
            )

        latest_process_ids = running_executable_process_ids(exe_path)
        if latest_process_ids:
            running_process_ids = latest_process_ids
            restart_after_upgrade = True
        stop_verified_packaged_backend(
            tool_id,
            dist_dir / "resources" / "app",
        )
        if restart_after_upgrade:
            if not stop_running_executable_for_upgrade(
                exe_path,
                running_process_ids,
            ):
                raise RuntimeError(
                    "The verified standalone tool did not stop for its "
                    "production upgrade"
                )
            desktop_stopped_for_upgrade = True
        promotion_recovery_root = promote_staged_distribution(
            staged_dist,
            dist_dir,
        )
        if promotion_recovery_root is not None:
            preserve_package_root = True
            live_verification = verify_packaged_app(
                dist_dir / "resources" / "app",
                project_root=PROJECT_ROOT,
            )
            if not live_verification.get("ok"):
                raise RuntimeError(
                    "Promoted package verification failed before old package "
                    f"cleanup: {live_verification.get('message', 'unknown error')}"
                )
            shutil.rmtree(promotion_recovery_root)
            promotion_recovery_root = None
            preserve_package_root = False
        if restart_after_upgrade:
            desktop_restarted = restart_packaged_executable(exe_path)
            if not desktop_restarted:
                raise RuntimeError(
                    "The package was promoted but the upgraded tool could "
                    "not be restarted automatically"
                )
    except PromotionRecoveryRequired as exc:
        preserve_package_root = True
        if (
            desktop_stopped_for_upgrade
            and not running_executable_process_ids(exe_path)
        ):
            desktop_restarted = restart_packaged_executable(exe_path)
        return {
            "ok": False,
            "tool_id": tool_id,
            "entry": str(entry),
            "exe_path": str(exe_path),
            "error_code": "PROMOTION_ROLLBACK_INCOMPLETE",
            "message": str(exc),
            "recovery_path": str(exc.recovery_root),
            "rollback_errors": list(exc.rollback_errors),
            "desktop_restarted": desktop_restarted,
        }
    except Exception as exc:
        retained_recovery = (
            (package_root / "promotion-aborted.json").exists()
            or (package_root / "promotion-recovery-manifest.json").exists()
            or (package_root / "previous-dist").exists()
            or any(package_root.glob("failed-new-dist*"))
        )
        if retained_recovery:
            preserve_package_root = True
        if (
            desktop_stopped_for_upgrade
            and not running_executable_process_ids(exe_path)
        ):
            desktop_restarted = restart_packaged_executable(exe_path)
        return {
            "ok": False,
            "tool_id": tool_id,
            "entry": str(entry),
            "exe_path": str(exe_path),
            "error_code": (
                "PROMOTION_FAILED_RECOVERY_RETAINED"
                if retained_recovery
                else "PACKAGE_FAILED"
            ),
            "message": str(exc),
            "diagnostic": traceback.format_exc(limit=12),
            "recovery_path": (
                str(package_root) if retained_recovery else ""
            ),
            "desktop_restarted": desktop_restarted,
        }
    finally:
        contains_recovery = (
            (package_root / "previous-dist").exists()
            or (package_root / "previous-runtime").exists()
            or (package_root / "previous-app").exists()
            or any(package_root.glob("failed-new-dist*"))
            or (package_root / "promotion-recovery-manifest.json").exists()
            or (package_root / "promotion-aborted.json").exists()
        )
        if (
            package_root.exists()
            and not preserve_package_root
            and not contains_recovery
        ):
            shutil.rmtree(package_root, ignore_errors=True)

    pruned_recovery_paths = prune_completed_recovery_roots(tool_dir)
    return {
        "ok": exe_path.exists(),
        "tool_id": tool_id,
        "entry": str(entry),
        "exe_path": str(exe_path),
        "renderer_path": str(dist_dir / "resources" / "app" / "renderer"),
        "runtime_path": (
            str(dist_dir / "resources" / "app" / runtime_path.relative_to(app_dir))
            if runtime_path
            else ""
        ),
        "python_runtime_path": str(
            dist_dir
            / "resources"
            / "app"
            / python_runtime_path.relative_to(app_dir)
        ),
        "package_digest": staged_verification.get("package_digest", ""),
        "backend_port": backend_port,
        "runtime_contract_version": runtime_contract["contract_version"],
        "production_upgrade": (
            "verified-stop-promote-restart"
            if restart_after_upgrade
            else "verified-promote"
        ),
        "desktop_restarted": desktop_restarted,
        "pruned_recovery_paths": pruned_recovery_paths,
        "recovery_path": (
            str(promotion_recovery_root)
            if promotion_recovery_root is not None
            else ""
        ),
    }
