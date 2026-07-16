from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src-core"
sys.path.insert(0, str(CORE))

from governance.enforcer import GovernanceEnforcer  # noqa: E402


CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
GOVERNANCE_CODE_ROOTS = (
    ROOT / "governance" / "code",
    ROOT / "src-core" / "governance",
    ROOT / "src-governance",
)
GOVERNANCE_CODE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".json"}


def _expect_blocked(result: dict[str, object], label: str, errors: list[str]) -> None:
    if result.get("allowed") is not False:
        errors.append(f"{label} was not blocked")


def main() -> int:
    errors: list[str] = []
    for code_root in GOVERNANCE_CODE_ROOTS:
        if not code_root.is_dir():
            continue
        for source_path in code_root.rglob("*"):
            if (
                source_path.is_file()
                and source_path.suffix.lower() in GOVERNANCE_CODE_EXTENSIONS
                and CJK_PATTERN.search(source_path.read_text(encoding="utf-8"))
            ):
                errors.append(
                    "governance source must use programming-language identifiers "
                    f"and English machine messages: {source_path.relative_to(ROOT)}"
                )
    enforcer = GovernanceEnforcer(ROOT)
    _expect_blocked(
        enforcer.can_modify_file(ROOT / "src-core" / "main.py", "core", "audit", "x = 1"),
        "main-program self modification",
        errors,
    )
    _expect_blocked(
        enforcer.can_modify_file(
            ROOT / "platform_tools" / "file-sorter" / "src" / "main.py",
            "toolbox",
            "audit",
            "x = 1",
        ),
        "main-program independent-tool modification",
        errors,
    )
    _expect_blocked(
        enforcer.can_modify_file(
            Path("C:/Windows/System32/drivers/etc/hosts"), "core", "audit", "x = 1"
        ),
        "outside-project modification",
        errors,
    )

    runtime_result = enforcer.can_create_file(
        ROOT / "runtime" / "state" / "main" / "audit.tmp", "core", "audit", "x = 1"
    )
    if runtime_result.get("allowed") is not True:
        errors.append("main runtime-state write was unexpectedly blocked")

    for manifest_path in sorted((ROOT / "platform_tools").glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid manifest {manifest_path}: {error}")
            continue
        tool_id = str(manifest.get("id") or "")
        if tool_id != manifest_path.parent.name:
            errors.append(f"tool identity mismatch: {manifest_path}")
        if manifest.get("enabled", True) is False:
            continue
        permissions = manifest.get("permissions")
        if not isinstance(permissions, dict):
            errors.append(f"{tool_id} is missing permissions")
            continue
        if permissions.get("code_scope") != "tool-root-only":
            errors.append(f"{tool_id} code scope is not tool-root-only")
        if permissions.get("database_scope") != "tool-database-only":
            errors.append(f"{tool_id} database scope is not isolated")
        allowed_markers = permissions.get("allow_modify")
        if not isinstance(allowed_markers, list):
            errors.append(f"{tool_id} allow_modify marker must be a list")
        if tool_id != "project-cleaner" and "authorized-project" in (allowed_markers or []):
            errors.append(f"{tool_id} cannot claim project-wide modification")
        environment_keys = {
            str(item).upper()
            for item in ((manifest.get("environment") or {}).get("allow") or [])
        }
        if tool_id == "ai-collaboration" and any(
            key.startswith("GPTBRIDGE_LOCAL_LLM") or key == "GPTBRIDGE_DISABLE_LOCAL_LLM"
            for key in environment_keys
        ):
            errors.append("external AI tool cannot inherit local AI environment")
        local_model_keys = {
            key for key in environment_keys if key.startswith("GPTBRIDGE_LOCAL_LLM")
        }
        if local_model_keys and tool_id != "local-ai":
            errors.append(f"{tool_id} cannot inherit local model configuration")
        ai_connections = (manifest.get("capabilities") or {}).get("ai-connections")
        if isinstance(ai_connections, dict):
            if ai_connections.get("transport") != "authenticated-websocket":
                errors.append(f"{tool_id} AI peers must use authenticated WebSocket")
            if ai_connections.get("queue_when_offline") is not False:
                errors.append(f"{tool_id} cannot queue offline AI commands")
            if ai_connections.get("share_database") is not False:
                errors.append(f"{tool_id} cannot share AI databases")
        repair = (manifest.get("capabilities") or {}).get("auto-repair")
        if not isinstance(repair, dict):
            errors.append(f"{tool_id} is missing auto-repair capability")
            continue
        if repair.get("authority") != "tool-root-only":
            errors.append(f"{tool_id} auto-repair authority is not tool-root-only")
        entry = manifest_path.parent / str(repair.get("entry") or "")
        try:
            entry.resolve(strict=False).relative_to(manifest_path.parent.resolve())
        except ValueError:
            errors.append(f"{tool_id} auto-repair entry escapes its tool root")
        if not entry.is_file():
            errors.append(f"{tool_id} auto-repair entry is missing")

    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    print("[PASS] runtime governance and per-tool capability boundaries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
