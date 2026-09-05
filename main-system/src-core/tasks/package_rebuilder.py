from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


def _background_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": creationflags} if creationflags else {}


class ToolPackageRebuilder:
    """Main-system-owned adapter for the governed platform packager."""

    TIMEOUT_SECONDS = 20 * 60

    def __init__(self, project_root: Path, tool_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.tool_root = tool_root.resolve()
        self.package_script = (
            self.tool_root
            / "src-core"
            / "tasks"
            / "platform_packager.py"
        ).resolve()

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        return stat.S_ISREG(metadata.st_mode) and not bool(attributes & 0x400)

    def rebuild(self, target_tool_id: str) -> dict[str, Any]:
        target_id = str(target_tool_id or "").strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", target_id) is None:
            return {"ok": False, "error_code": "INVALID_TOOL_ID"}
        expected_script = (
            self.tool_root
            / "src-core"
            / "tasks"
            / "platform_packager.py"
        )
        if (
            self.package_script != expected_script
            or not self._is_regular_file(self.package_script)
            or not (self.project_root / target_id / "manifest.json").is_file()
        ):
            return {"ok": False, "error_code": "PACKAGE_REBUILDER_UNAVAILABLE"}

        environment = {
            key: value
            for key, value in os.environ.items()
            if "TOKEN" not in key.upper()
            and "SECRET" not in key.upper()
            and "PASSWORD" not in key.upper()
            and key != "GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP"
        }
        environment.update(
            {
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        try:
            completed = subprocess.run(
                [
                    str(Path(sys.executable).resolve()),
                    "-B",
                    "-X",
                    "utf8",
                    str(self.package_script),
                    target_id,
                    "--json",
                ],
                cwd=str(self.tool_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.TIMEOUT_SECONDS,
                check=False,
                **_background_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error_code": "PACKAGE_REPAIR_TIMEOUT"}
        except OSError as error:
            return {
                "ok": False,
                "error_code": "PACKAGE_REPAIR_START_FAILED",
                "message": str(error),
            }

        output = completed.stdout.strip()
        detail: dict[str, Any] = {}
        if output:
            try:
                parsed = json.loads(output.splitlines()[-1])
                if isinstance(parsed, dict):
                    detail = parsed
            except json.JSONDecodeError:
                detail = {"output": output[-4000:]}
        return {
            "ok": completed.returncode == 0 and detail.get("ok") is True,
            "owner": "main-system",
            "provider": "governed-platform-packager",
            "target_tool_id": target_id,
            "return_code": completed.returncode,
            "stderr": completed.stderr[-4000:],
            "detail": detail,
        }


__all__ = ["ToolPackageRebuilder"]
