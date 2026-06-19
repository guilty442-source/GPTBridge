from __future__ import annotations

import asyncio
import json
import platform
import py_compile
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from patch_engine.patch_engine import PatchEngine

from .process_utils import terminate_process_tree
from .subsystem_backup import directory_size_bytes


REFERENCE_OPTIONS = (
    "python_desktop",
    "electron_desktop",
    "electron_python",
    "cli_tool",
    "ai_tool",
    "web_dashboard",
    "api_service",
    "automation_bot",
    "extension_tool",
    "blank_minimal",
)

REFERENCE_LABELS = {
    "python_desktop": "Python desktop application",
    "electron_desktop": "Electron desktop application",
    "electron_python": "Electron + Python hybrid application",
    "cli_tool": "CLI command line tool",
    "ai_tool": "AI tool",
    "web_dashboard": "Web dashboard",
    "api_service": "API service",
    "automation_bot": "Automation bot",
    "extension_tool": "Plugin or extension tool",
    "blank_minimal": "Blank minimal template",
}

SAFE_FILE_SUFFIXES = {".py", ".txt", ".md", ".json", ".ts", ".tsx", ".js", ".jsx", ".css", ".html"}
DEFAULT_FILE = "main.py"
METADATA_FILE = "manifest.json"
LEGACY_METADATA_FILE = "tool.json"
WINDOWS_TARGET = "Windows 11"

PROJECT_TEMPLATES: dict[str, dict[str, Any]] = {
    "python_desktop": {
        "tech_stack": "python",
        "entry": "main.py",
        "dirs": ["src", "src/ui", "src/core", "src/config", "src/services", "src/utils", "assets", "build", "tests"],
        "files": ["main.py", "config.json", "requirements.txt", "README.md", ".gitignore"],
    },
    "electron_desktop": {
        "tech_stack": "electron",
        "entry": "src/main/main.ts",
        "dirs": ["src", "src/main", "src/renderer", "src/preload", "src/config", "src/services", "src/utils", "assets", "build", "tests"],
        "files": ["package.json", "src/main/main.ts", "src/preload/preload.ts", "README.md", ".gitignore"],
    },
    "electron_python": {
        "tech_stack": "electron-python",
        "entry": "frontend/src/main/main.ts",
        "dirs": [
            "frontend",
            "frontend/src/main",
            "frontend/src/renderer",
            "frontend/src/preload",
            "backend",
            "backend/core",
            "backend/services",
            "backend/config",
            "backend/utils",
            "shared",
            "assets",
            "build",
            "tests",
        ],
        "files": ["package.json", "requirements.txt", "README.md", ".gitignore"],
    },
    "cli_tool": {
        "tech_stack": "python-cli",
        "entry": "main.py",
        "dirs": ["src", "src/core", "src/commands", "src/config", "src/services", "src/utils", "tests"],
        "files": ["main.py", "config.json", "requirements.txt", "README.md", ".gitignore"],
    },
    "ai_tool": {
        "tech_stack": "python-ai",
        "entry": "main.py",
        "dirs": ["src", "src/core", "src/providers", "src/prompts", "src/agents", "src/config", "src/services", "src/utils", "tests"],
        "files": ["main.py", "config.json", "requirements.txt", "README.md", ".gitignore"],
    },
    "web_dashboard": {
        "tech_stack": "web",
        "entry": "src/pages/index.tsx",
        "dirs": ["src", "src/components", "src/pages", "src/layouts", "src/services", "src/config", "src/utils", "public", "build", "tests"],
        "files": ["package.json", "README.md", ".gitignore"],
    },
    "api_service": {
        "tech_stack": "python-api",
        "entry": "main.py",
        "dirs": ["src", "src/routes", "src/controllers", "src/services", "src/models", "src/config", "src/utils", "tests"],
        "files": ["main.py", "requirements.txt", "README.md", ".gitignore"],
    },
    "automation_bot": {
        "tech_stack": "python-automation",
        "entry": "main.py",
        "dirs": ["src", "src/tasks", "src/workflows", "src/services", "src/config", "src/utils", "logs", "tests"],
        "files": ["main.py", "config.json", "requirements.txt", "README.md", ".gitignore"],
    },
    "extension_tool": {
        "tech_stack": "browser-extension",
        "entry": "manifest.json",
        "dirs": ["src", "src/background", "src/content", "src/popup", "src/options", "src/utils", "assets", "build"],
        "files": ["manifest.json", "README.md", ".gitignore"],
    },
    "blank_minimal": {
        "tech_stack": "python",
        "entry": "main.py",
        "dirs": ["src", "src/core"],
        "files": ["main.py", "README.md", ".gitignore"],
    },
}


def sanitize_tool_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", value).strip("-") or "ChildTool"


def normalize_output_root(project_root: Path) -> Path:
    return (project_root / "platform_tools").resolve()


def is_windows_target_host() -> bool:
    return platform.system().lower() == "windows"


class ChildToolWorkspace:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.output_root = normalize_output_root(self.project_root)
        self.workspace_root = self.output_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def refresh(self) -> None:
        self.output_root = normalize_output_root(self.project_root)
        self.workspace_root = self.output_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def project_dir(self, tool_name: str) -> Path:
        self.refresh()
        sanitized = sanitize_tool_name(tool_name)
        indexed = self._indexed_project_dir(sanitized)
        if indexed is not None:
            return indexed
        return (self.workspace_root / sanitized).resolve()

    def list_projects(self) -> list[dict[str, Any]]:
        self.refresh()
        projects: list[dict[str, Any]] = []
        for path in sorted(self.workspace_root.glob("*")):
            if not path.is_dir():
                continue
            if not self._read_metadata(path):
                continue
            projects.append(self.project_overview(path))
        return projects

    def project_overview(self, tool_dir: Path) -> dict[str, Any]:
        metadata = self._read_metadata(tool_dir)
        stat = tool_dir.stat()
        return {
            "name": str(metadata.get("name", tool_dir.name)),
            "path": str(tool_dir),
            "template": str(metadata.get("reference_type", "python_desktop")),
            "tech_stack": str(metadata.get("tech_stack", "")),
            "version": str(metadata.get("version", "0.1.0")),
            "status": str(metadata.get("status", "ready")),
            "size_bytes": directory_size_bytes([tool_dir]),
            "updated_at": metadata.get("updated_at", datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")),
            "last_used_at": metadata.get("last_used_at", ""),
        }

    def create_project(self, tool_name: str, reference_type: str = "python_desktop") -> dict[str, Any]:
        sanitized = sanitize_tool_name(tool_name)
        reference = reference_type if reference_type in REFERENCE_OPTIONS else "python_desktop"
        tool_dir = self.project_dir(sanitized)
        return self._create_project_at(tool_dir, sanitized, reference)

    def create_project_at(self, selected_root: str, tool_name: str, reference_type: str = "python_desktop") -> dict[str, Any]:
        self.refresh()
        root = self.workspace_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        sanitized = sanitize_tool_name(tool_name)
        reference = reference_type if reference_type in REFERENCE_OPTIONS else "python_desktop"
        tool_dir = (root / sanitized).resolve()
        if root not in tool_dir.parents:
            raise ValueError("project path escaped selected root")
        self._validate_project_root(tool_dir)
        result = self._create_project_at(tool_dir, sanitized, reference)
        self._register_project(sanitized, tool_dir)
        return result

    def open_project_root(self, selected_root: str) -> dict[str, Any]:
        tool_dir = Path(selected_root).resolve()
        metadata = self._read_metadata(tool_dir)
        if not metadata:
            return {"ok": False, "message": "selected folder is not a GPTBridge child project"}
        name = sanitize_tool_name(str(metadata.get("name", tool_dir.name)))
        self._validate_project_root(tool_dir)
        self._register_project(name, tool_dir)
        reference = str(metadata.get("reference_type", "python_desktop"))
        entry = str(metadata.get("entry", DEFAULT_FILE))
        entry_path = tool_dir / entry
        return {
            "ok": True,
            "toolName": name,
            "reference_type": reference if reference in REFERENCE_OPTIONS else "python_desktop",
            "target_platform": metadata.get("target_platform", WINDOWS_TARGET),
            "tech_stack": metadata.get("tech_stack", ""),
            "version": metadata.get("version", "0.1.0"),
            "project_dir": str(tool_dir),
            "file_path": entry,
            "content": entry_path.read_text(encoding="utf-8", errors="replace") if entry_path.exists() else "",
            "overview": self.project_overview(tool_dir),
            "message": "child project opened",
        }

    def _create_project_at(self, tool_dir: Path, sanitized: str, reference: str) -> dict[str, Any]:
        tool_dir.mkdir(parents=True, exist_ok=True)
        template = PROJECT_TEMPLATES[reference]
        for rel_dir in template["dirs"]:
            (tool_dir / rel_dir).mkdir(parents=True, exist_ok=True)

        metadata = {
            "id": sanitized.lower(),
            "name": sanitized,
            "version": "0.1.0",
            "status": "ready",
            "enabled": True,
            "entry": template["entry"],
            "runtime": {
                "type": "python" if template["tech_stack"].startswith("python") else template["tech_stack"],
                "entry": template["entry"],
                "workingDirectory": ".",
            },
            "executable": {
                "path": f"dist/{sanitized}.exe",
                "name": sanitized,
                "console": reference == "cli_tool",
            },
            "description": f"{sanitized} standalone platform tool.",
            "reference_type": reference,
            "target_platform": WINDOWS_TARGET,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "tech_stack": template["tech_stack"],
        }
        self._write_if_missing(tool_dir / METADATA_FILE, json.dumps(metadata, indent=2) + "\n")
        for rel_file in template["files"]:
            self._write_if_missing(tool_dir / rel_file, self._file_template(sanitized, reference, rel_file))
        entry_path = tool_dir / template["entry"]
        if not entry_path.exists() and entry_path.suffix.lower() in SAFE_FILE_SUFFIXES:
            self._write_if_missing(entry_path, self._file_template(sanitized, reference, template["entry"]))

        return {
            "ok": True,
            "toolName": sanitized,
            "reference_type": reference,
            "tech_stack": template["tech_stack"],
            "target_platform": WINDOWS_TARGET,
            "project_dir": str(tool_dir),
            "file_path": template["entry"],
            "content": entry_path.read_text(encoding="utf-8", errors="replace") if entry_path.exists() else "",
            "overview": self.project_overview(tool_dir),
            "reference_options": list(REFERENCE_OPTIONS),
            "message": "child tool project ready",
        }

    def open_file(self, tool_name: str, file_path: str = DEFAULT_FILE) -> dict[str, Any]:
        tool_dir = self.project_dir(tool_name)
        target = self._safe_file(tool_dir, file_path)
        if not target.exists():
            return {
                "ok": False,
                "toolName": sanitize_tool_name(tool_name),
                "file_path": file_path,
                "message": "child tool file does not exist",
            }
        return {
            "ok": True,
            "toolName": sanitize_tool_name(tool_name),
            "project_dir": str(tool_dir),
            "file_path": self._relative_to_tool(tool_dir, target),
            "content": target.read_text(encoding="utf-8", errors="replace"),
            "message": "child tool file opened",
        }

    def open_selected_file(self, selected_path: str) -> dict[str, Any]:
        target = self._safe_workspace_file(selected_path)
        if not target.exists():
            return {"ok": False, "message": "selected child tool file does not exist"}
        tool_name, rel_file = self._tool_and_file_from_workspace_path(target)
        return {
            "ok": True,
            "toolName": tool_name,
            "project_dir": str(self.project_dir(tool_name)),
            "file_path": rel_file,
            "content": target.read_text(encoding="utf-8", errors="replace"),
            "message": "child tool file opened",
        }

    def create_selected_file(self, selected_path: str, reference_type: str = "python_desktop") -> dict[str, Any]:
        target = self._safe_workspace_file(selected_path)
        tool_name, rel_file = self._tool_and_file_from_workspace_path(target)
        self.create_project(tool_name, reference_type)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self._python_template(tool_name, reference_type) if target.suffix.lower() == ".py" else ""
            target.write_text(content, encoding="utf-8")
        return self.open_selected_file(str(target))

    def save_file(self, tool_name: str, file_path: str, content: str) -> dict[str, Any]:
        tool_dir = self.project_dir(tool_name)
        tool_dir.mkdir(parents=True, exist_ok=True)
        target = self._safe_file(tool_dir, file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._touch_metadata(tool_dir)
        return {
            "ok": True,
            "toolName": sanitize_tool_name(tool_name),
            "project_dir": str(tool_dir),
            "file_path": self._relative_to_tool(tool_dir, target),
            "message": "child tool file autosaved",
        }

    def apply_ai_answer(self, tool_name: str, ai_answer: str) -> dict[str, Any]:
        tool_dir = self.project_dir(tool_name)
        tool_dir.mkdir(parents=True, exist_ok=True)
        patch_engine = PatchEngine(tool_dir)
        patches = patch_engine.parse_patch_text(ai_answer, default_path=DEFAULT_FILE)
        safe_patches: dict[str, str] = {}
        for rel_path, content in patches.items():
            target = self._safe_file(tool_dir, rel_path)
            safe_patches[self._relative_to_tool(tool_dir, target)] = content

        applied = patch_engine.apply_patches(safe_patches, allowed_files=list(safe_patches.keys()))
        check = self.check_project(tool_name)
        return {
            "ok": bool(applied) and bool(check["ok"]),
            "toolName": sanitize_tool_name(tool_name),
            "files_modified": applied,
            "check": check,
            "message": "child tool code saved" if applied and check["ok"] else "child tool code saved but check failed",
        }

    def check_project(self, tool_name: str) -> dict[str, Any]:
        tool_dir = self.project_dir(tool_name)
        if not tool_dir.exists():
            return {"ok": False, "toolName": sanitize_tool_name(tool_name), "message": "child tool project not found"}

        checked: list[str] = []
        errors: list[str] = []
        for path in sorted(tool_dir.rglob("*.py")):
            if self._is_build_path(tool_dir, path):
                continue
            rel_path = self._relative_to_tool(tool_dir, path)
            try:
                py_compile.compile(str(path), doraise=True)
                checked.append(rel_path)
            except Exception as exc:
                errors.append(f"{rel_path}: {exc}")

        return {
            "ok": not errors,
            "toolName": sanitize_tool_name(tool_name),
            "checked_files": checked,
            "errors": errors,
            "target_platform": WINDOWS_TARGET,
            "message": "child tool code check completed" if not errors else "child tool code check failed",
        }

    async def test_project(self, tool_name: str) -> dict[str, Any]:
        check = self.check_project(tool_name)
        if not check["ok"]:
            return check

        tool_dir = self.project_dir(tool_name)
        main_file = self._entry_path(tool_dir)
        if not main_file.exists():
            return {"ok": False, "toolName": sanitize_tool_name(tool_name), "message": "entry file is missing"}

        return_code, output = await self._run_process(tool_dir, sys.executable, str(main_file), "--self-test")
        return {
            "ok": return_code == 0,
            "toolName": sanitize_tool_name(tool_name),
            "exit_code": return_code,
            "output": output,
            "target_platform": WINDOWS_TARGET,
            "message": "child tool test completed" if return_code == 0 else "child tool test failed",
        }

    async def package_project(self, tool_name: str) -> dict[str, Any]:
        if not is_windows_target_host():
            return {
                "ok": False,
                "toolName": sanitize_tool_name(tool_name),
                "target_platform": WINDOWS_TARGET,
                "message": "child tool packaging requires Windows 11 host",
            }

        test_result = await self.test_project(tool_name)
        if not test_result["ok"]:
            return {
                "ok": False,
                "toolName": sanitize_tool_name(tool_name),
                "test": test_result,
                "message": "child tool package stopped because test failed",
            }

        tool_dir = self.project_dir(tool_name)
        sanitized = sanitize_tool_name(tool_name)
        main_file = self._entry_path(tool_dir)
        dist_dir = tool_dir / "dist"
        build_dir = tool_dir / "build"
        args = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onefile",
            "--clean",
            "--name",
            sanitized,
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(build_dir),
            "--specpath",
            str(build_dir),
        ]
        if self._reference_type(tool_dir) != "cli_tool":
            args.append("--noconsole")
        args.append(str(main_file))

        return_code, output = await self._run_process(tool_dir, *args)
        exe_path = dist_dir / f"{sanitized}.exe"
        exe_exists = exe_path.exists() and exe_path.is_file()
        exe_size = exe_path.stat().st_size if exe_exists else 0
        return {
            "ok": return_code == 0 and exe_exists and exe_size > 0,
            "toolName": sanitized,
            "exit_code": return_code,
            "output": output,
            "project_dir": str(tool_dir),
            "exe_path": str(exe_path),
            "exe_exists": exe_exists,
            "exe_size_bytes": exe_size,
            "target_platform": WINDOWS_TARGET,
            "message": "windows 11 child exe package completed" if return_code == 0 and exe_exists else "windows 11 child exe package failed",
        }

    def context_summary(self, tool_name: str, max_chars: int = 12000) -> dict[str, Any]:
        tool_dir = self.project_dir(tool_name)
        if not tool_dir.exists():
            return {"ok": False, "summary": "", "message": "child tool project not found"}

        chunks: list[str] = []
        for path in sorted(tool_dir.rglob("*")):
            if not path.is_file() or self._is_build_path(tool_dir, path) or path.suffix.lower() not in SAFE_FILE_SUFFIXES:
                continue
            rel_path = self._relative_to_tool(tool_dir, path)
            text = path.read_text(encoding="utf-8", errors="replace")
            chunks.append(f"===== {rel_path} =====\n{text[:3000]}")
            if sum(len(item) for item in chunks) >= max_chars:
                break

        summary = "\n\n".join(chunks)[:max_chars]
        return {
            "ok": True,
            "toolName": sanitize_tool_name(tool_name),
            "project_dir": str(tool_dir),
            "summary": summary,
            "message": "child tool context summarized",
        }

    def size_summary(self) -> dict[str, Any]:
        self.refresh()
        projects = self.list_projects()
        return {
            "root": str(self.workspace_root),
            "size_bytes": directory_size_bytes([self.workspace_root]),
            "projects": projects,
        }

    def delete_project(self, tool_name: str) -> dict[str, Any]:
        tool_dir = self.project_dir(tool_name)
        if not tool_dir.exists():
            return {"ok": False, "toolName": sanitize_tool_name(tool_name), "message": "child tool project not found"}
        shutil.rmtree(tool_dir)
        self._unregister_project(sanitize_tool_name(tool_name))
        return {"ok": True, "toolName": sanitize_tool_name(tool_name), "message": "child tool deleted"}

    def rename_project(self, tool_name: str, new_name: str) -> dict[str, Any]:
        old_name = sanitize_tool_name(tool_name)
        sanitized_new = sanitize_tool_name(new_name)
        old_dir = self.project_dir(old_name)
        new_dir = self.project_dir(sanitized_new)
        if not old_dir.exists():
            return {"ok": False, "toolName": old_name, "message": "child tool project not found"}
        if new_dir.exists():
            return {"ok": False, "toolName": old_name, "new_name": sanitized_new, "message": "target child tool name already exists"}
        old_dir.rename(new_dir)
        metadata_path = self._metadata_path(new_dir)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["name"] = sanitized_new
            metadata["updated_at"] = datetime.now().isoformat(timespec="seconds")
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
        self._unregister_project(old_name)
        self._register_project(sanitized_new, new_dir)
        return {
            "ok": True,
            "toolName": old_name,
            "new_name": sanitized_new,
            "project_dir": str(new_dir),
            "message": "child tool renamed",
        }

    def release_summary(self, tool_name: str) -> dict[str, Any]:
        tool_dir = self.project_dir(tool_name)
        dist_dir = tool_dir / "dist"
        releases = []
        if dist_dir.exists():
            for path in sorted(dist_dir.glob("*.exe")):
                releases.append({
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                })
        return {
            "ok": True,
            "toolName": sanitize_tool_name(tool_name),
            "releases": releases,
            "message": "release summary updated",
        }

    def _connect_db(self) -> "sqlite3.Connection":
        db_path = self.project_root / "runtime" / "state" / "gptbridge.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute('''
            CREATE TABLE IF NOT EXISTS child_tool_index (
                name TEXT PRIMARY KEY,
                path TEXT NOT NULL
            )
        ''')
        return conn

    def _load_index(self) -> dict[str, str]:
        try:
            with self._connect_db() as conn:
                cursor = conn.execute("SELECT name, path FROM child_tool_index")
                return {row[0]: row[1] for row in cursor.fetchall()}
        except Exception:
            return {}

    def _save_index(self, data: dict[str, str]) -> None:
        try:
            with self._connect_db() as conn:
                conn.execute("DELETE FROM child_tool_index")
                for name, path in data.items():
                    conn.execute(
                        "INSERT INTO child_tool_index (name, path) VALUES (?, ?)", 
                        (name, path)
                    )
        except Exception as e:
            print(f"Error saving child tool index: {e}")

    def _indexed_project_dir(self, tool_name: str) -> Path | None:
        index = self._load_index()
        raw_path = index.get(sanitize_tool_name(tool_name))
        if not raw_path:
            return None
        candidate = Path(raw_path).resolve()
        if not candidate.exists() or not self._read_metadata(candidate):
            index.pop(sanitize_tool_name(tool_name), None)
            self._save_index(index)
            return None
        self._validate_project_root(candidate)
        return candidate

    def _register_project(self, tool_name: str, tool_dir: Path) -> None:
        self._validate_project_root(tool_dir)
        index = self._load_index()
        index[sanitize_tool_name(tool_name)] = str(tool_dir.resolve())
        self._save_index(index)

    def _unregister_project(self, tool_name: str) -> None:
        index = self._load_index()
        index.pop(sanitize_tool_name(tool_name), None)
        self._save_index(index)

    def _validate_project_root(self, tool_dir: Path) -> None:
        resolved = tool_dir.resolve()
        platform_root = (self.project_root / "platform_tools").resolve()
        if resolved != platform_root and platform_root not in resolved.parents:
            raise ValueError("child project must live inside platform_tools")
        protected = [
            self.project_root / "src-core",
            self.project_root / "src-ui",
            self.project_root / "config",
            self.project_root / "runtime" / "profiles",
            self.project_root / ".GPTBridge_RuntimeSandbox",
            self.project_root / "backups",
            self.project_root / "node_modules",
        ]
        for root in protected:
            root_resolved = root.resolve()
            if resolved == root_resolved or root_resolved in resolved.parents:
                raise ValueError("child project cannot target protected GPTBridge paths")

    async def _run_process(self, cwd: Path, *args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=120)
        except asyncio.TimeoutError:
            await terminate_process_tree(process)
            output, _ = await process.communicate()
            return 124, (output.decode(errors="replace") if output else "") + "\nprocess timeout"
        return process.returncode, output.decode(errors="replace") if output else ""

    def _safe_file(self, tool_dir: Path, file_path: str) -> Path:
        rel_path = Path(file_path or DEFAULT_FILE)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError("invalid child tool file path")
        blocked_roots = {"src-core", "src-ui", "config", "runtime", "node_modules", "release", "backups"}
        if rel_path.parts and rel_path.parts[0] in blocked_roots:
            raise ValueError("child tool file path cannot target mother-tool directories")
        if rel_path.suffix.lower() not in SAFE_FILE_SUFFIXES:
            raise ValueError("unsupported child tool file type")
        target = (tool_dir / rel_path).resolve()
        if tool_dir.resolve() not in target.parents and target != tool_dir.resolve():
            raise ValueError("child tool file path escaped workspace")
        return target

    def _safe_workspace_file(self, file_path: str) -> Path:
        target = Path(file_path).resolve()
        workspace_root = self.workspace_root.resolve()
        if workspace_root not in target.parents:
            raise ValueError("selected file must be inside child-tool workspace")
        if target.suffix.lower() not in SAFE_FILE_SUFFIXES:
            raise ValueError("unsupported child tool file type")
        rel_parts = target.relative_to(workspace_root).parts
        if len(rel_parts) < 2:
            raise ValueError("selected file must be inside a child tool folder")
        return target

    def _tool_and_file_from_workspace_path(self, target: Path) -> tuple[str, str]:
        rel_path = target.resolve().relative_to(self.workspace_root.resolve())
        tool_name = sanitize_tool_name(rel_path.parts[0])
        file_path = Path(*rel_path.parts[1:]).as_posix()
        return tool_name, file_path

    @staticmethod
    def _relative_to_tool(tool_dir: Path, path: Path) -> str:
        return path.resolve().relative_to(tool_dir.resolve()).as_posix()

    @staticmethod
    def _write_if_missing(path: Path, content: str) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _is_build_path(tool_dir: Path, path: Path) -> bool:
        rel_parts = path.resolve().relative_to(tool_dir.resolve()).parts
        return any(part in {"build", "dist", "__pycache__"} for part in rel_parts)

    @staticmethod
    def _reference_type(tool_dir: Path) -> str:
        try:
            data = ChildToolWorkspace._read_metadata(tool_dir)
            reference = str(data.get("reference_type", "python_desktop"))
            return reference if reference in REFERENCE_OPTIONS else "python_desktop"
        except Exception:
            return "python_desktop"

    @staticmethod
    def _metadata_path(tool_dir: Path) -> Path:
        manifest_path = tool_dir / METADATA_FILE
        if manifest_path.exists():
            return manifest_path
        legacy_path = tool_dir / LEGACY_METADATA_FILE
        if legacy_path.exists():
            return legacy_path
        return manifest_path

    @staticmethod
    def _read_metadata(tool_dir: Path) -> dict[str, Any]:
        for filename in (METADATA_FILE, LEGACY_METADATA_FILE):
            try:
                data = json.loads((tool_dir / filename).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return {}

    @staticmethod
    def _entry_path(tool_dir: Path) -> Path:
        metadata = ChildToolWorkspace._read_metadata(tool_dir)
        return tool_dir / str(metadata.get("entry", DEFAULT_FILE))

    @staticmethod
    def _touch_metadata(tool_dir: Path) -> None:
        metadata_path = ChildToolWorkspace._metadata_path(tool_dir)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            metadata = {}
        metadata["updated_at"] = datetime.now().isoformat(timespec="seconds")
        metadata["size_bytes"] = directory_size_bytes([tool_dir])
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _python_template(tool_name: str, reference_type: str) -> str:
        title = REFERENCE_LABELS.get(reference_type, "Desktop tool")
        return f'''from __future__ import annotations

import argparse


def run() -> int:
    print("{tool_name} - {title} ready on Windows 11")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="{tool_name}")
    parser.add_argument("--self-test", action="store_true", help="Run a quick Windows 11 stability check.")
    args = parser.parse_args()
    if args.self_test:
        print("self-test ok")
        return 0
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
'''

    @staticmethod
    def _readme_template(tool_name: str, reference_type: str) -> str:
        title = REFERENCE_LABELS.get(reference_type, "Desktop tool")
        return f"""# {tool_name}

Target platform: Windows 11
Reference type: {title}

This child tool is managed by GPTBridge design mode.
"""

    @staticmethod
    def _file_template(tool_name: str, reference_type: str, rel_file: str) -> str:
        if rel_file.endswith(".py"):
            return ChildToolWorkspace._python_template(tool_name, reference_type)
        if rel_file == "README.md":
            return ChildToolWorkspace._readme_template(tool_name, reference_type)
        if rel_file == ".gitignore":
            return "__pycache__/\n*.pyc\nnode_modules/\ndist/\nbuild/\n.env\n"
        if rel_file == "config.json":
            return json.dumps({"target_platform": WINDOWS_TARGET, "name": tool_name}, indent=2) + "\n"
        if rel_file == "requirements.txt":
            return "\n"
        if rel_file == "package.json":
            return json.dumps(
                {
                    "name": tool_name.lower(),
                    "version": "0.1.0",
                    "private": True,
                    "scripts": {"build": "echo build"},
                },
                indent=2,
            ) + "\n"
        if rel_file.endswith(".ts"):
            return "export {};\n"
        if rel_file == "manifest.json":
            return json.dumps({"manifest_version": 3, "name": tool_name, "version": "0.1.0"}, indent=2) + "\n"
        return ""
