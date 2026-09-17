"""Tool spawn process mixin (A185 split).

Contains the _spawn_tool_process method extracted from StartSpawnMixin.
"""
# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Dict, IO

from .toolbox_constants import _background_subprocess_kwargs


def _open_tool_stderr_log(project_root: Path, tool_id: str) -> IO[bytes] | None:
    """Open the managed stderr log for a spawned tool process.

    Crash diagnosis reads this tail instead of a lost DEVNULL stream.
    Rotation keeps the current generation plus one previous log so the
    file stays bounded without a sweeper.
    """
    try:
        log_dir = (
            Path(project_root)
            / "main-system"
            / "runtime"
            / "logs"
            / "tools"
            / tool_id
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        current = log_dir / "stderr.log"
        previous = log_dir / "stderr.prev.log"
        if current.is_file():
            try:
                current.replace(previous)
            except OSError:
                pass
        return current.open("wb")
    except OSError:
        return None


class SpawnProcessMixin:
    """Spawn tool process."""

    _start_ctx: dict[str, Any]
    _source_runtime_environments: dict[str, Any]

    def _source_runtime_environment(self, tool_id: str, tool_dir: Path, manifest: dict) -> dict[str, str]:
        raise NotImplementedError

    def _tool_environment(self, tool_id: str, tool_dir: Path, manifest: dict, start_hidden: bool) -> dict[str, str]:
        raise NotImplementedError

    async def _release_tool_process(self, request_id: str) -> None:
        raise NotImplementedError

    async def update_status(self, tool_id: str, status: str) -> Dict[str, Any]:
        raise NotImplementedError

    def _executable_fallback_allowed(self, manifest: dict, requested_mode: str, executable_exists: bool) -> bool:
        raise NotImplementedError

    def _source_fallback_allowed(self, manifest: dict, requested_mode: str) -> bool:
        raise NotImplementedError

    async def start_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def _retry_start_after_central_repair(
        self, payload: Dict[str, Any], tool_id: str, tool_dir: Path,
        manifest: dict, failure_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        raise NotImplementedError

    async def _spawn_tool_process(
        self, tool_id: str, request_id: str, manifest: dict,
        tool_dir: Path, executable_file: Path, args: list,
        background: bool, requested_mode: str,
        repair_attempted: bool, fallback_attempted: bool,
        executable_fallback_attempted: bool, payload: Dict[str, Any],
    ) -> Any | Dict[str, Any]:
        """Spawn the tool process. Returns process or error dict."""
        ctx = self._start_ctx
        use_source_runtime = ctx["use_source_runtime"]
        source_entry = ctx["source_entry"]
        python_executable = ctx["python_executable"]

        stderr_log = _open_tool_stderr_log(self.project_root, tool_id)
        stderr_target: Any = stderr_log if stderr_log is not None else subprocess.DEVNULL
        try:
            if use_source_runtime and source_entry is not None and python_executable is not None:
                process = None
                for _spawn_attempt in range(2):
                    source_environment = self._source_runtime_environment(
                        tool_id, tool_dir, manifest,
                    )
                    process = await asyncio.create_subprocess_exec(
                        str(python_executable), "-B", "-s", "-E", "-X", "utf8",
                        str(source_entry), *args,
                        cwd=str(tool_dir),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_target,
                        close_fds=True,
                        env=source_environment,
                        **_background_subprocess_kwargs(),
                    )
                    for _ in range(3):
                        if process.returncode is not None:
                            break
                        await asyncio.sleep(0.1)
                    if process.returncode is None:
                        break
                self._source_runtime_environments[tool_id] = source_environment
            else:
                process = await asyncio.create_subprocess_exec(
                    str(executable_file), *args,
                    cwd=str(tool_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_target,
                    close_fds=True,
                    env=self._tool_environment(
                        tool_id, tool_dir, manifest, start_hidden=background,
                    ),
                    **_background_subprocess_kwargs(),
                )
        except Exception as exc:
            await self._release_tool_process(request_id)
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False, "tool_id": tool_id, "request_id": request_id,
                "error_code": "PROCESS_START_FAILED",
                "message": str(exc),
            }
            if (
                use_source_runtime
                and not executable_fallback_attempted
                and self._executable_fallback_allowed(
                    manifest, requested_mode, executable_file.exists(),
                )
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "executable"
                fallback_payload["_executable_fallback_attempted"] = True
                fallback_payload["_source_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if (
                not use_source_runtime
                and not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload, tool_id, tool_dir, manifest, failure_result,
                )
            return failure_result
        finally:
            # The child holds its own duplicated stderr handle; the parent
            # copy can be closed as soon as the spawn attempt settles.
            if stderr_log is not None:
                try:
                    stderr_log.close()
                except OSError:
                    pass
        return process


__all__ = ["SpawnProcessMixin"]
