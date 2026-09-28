"""Codex pin sync driver — release contract follows the live authority.

每個 tick 讀取 PostgreSQL 權威的 ``authority_state``（codex_version +
source_sha256），與 ``shared-layer/release-dependencies.json`` 的
``governance_references`` pin 比對；漂移時對該檔做外科式兩欄位改寫
（僅 governance_references 區塊內的 codex_version/codex_sha256，
其餘內容逐位元保留）。本 driver 只改實作面契約檔，不觸碰法典權威；
檔案落盤後由既有 self-commit 機制收檔。

Kill switch：flow manifest ``enabled=false``（A10）。只經
``AutomationCore.register_flow`` 掛共享 PeriodicScheduler；被拒絕時
不回落私有迴圈。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.codex_pin_sync")

FLOW_ID = "codex-pin-sync"

_MAIN_SYSTEM_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONTRACT = _MAIN_SYSTEM_ROOT.parent / "shared-layer" / "release-dependencies.json"
_DEFAULT_STATE = (
    _MAIN_SYSTEM_ROOT / "runtime" / "state" / "codex-pin-sync.json"
)
_DEFAULT_LOG = (
    _MAIN_SYSTEM_ROOT / "runtime" / "logs" / "codex-pin-sync.jsonl"
)

_BLOCK_RE = re.compile(r'"governance_references"\s*:\s*\{(?P<body>.*?)\n  \}', re.S)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CodexPinSyncDriver:
    """Periodic release-contract codex pin reconciliation."""

    def __init__(
        self,
        app: Any,
        toolbox_service: Any = None,
        *,
        project_root: Path | None = None,
        contract_path: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.app = app
        self.toolbox = toolbox_service
        self._project_root = Path(
            project_root or getattr(app, "project_root", "")
        ).resolve()
        self._contract_path = contract_path or _DEFAULT_CONTRACT
        self._state_path = state_path or _DEFAULT_STATE
        self._registered = False
        self._last_decision = ""
        self._last_error = ""
        self._last_pinned = ""

    async def start(self) -> dict[str, Any]:
        core = getattr(self.app, "automation_core", None)
        if core is None:
            _logger.warning(
                "codex-pin-sync: no automation core — not starting "
                "(no private loop fallback)"
            )
            return {"status": "no-automation-core"}
        self._registered = core.register_flow(FLOW_ID, self.run_once)
        return {
            "status": "registered" if self._registered else "denied",
            "flow": FLOW_ID,
        }

    async def stop(self) -> None:
        core = getattr(self.app, "automation_core", None)
        if core is not None and self._registered:
            try:
                core.unregister(FLOW_ID)
            except Exception:
                pass
        self._registered = False

    async def run_once(self) -> None:
        """One scheduler tick — never raises into the shared loop."""
        try:
            decision = await self._tick_inner()
            self._last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — audit then isolate
            decision = "error"
            self._last_error = f"{type(error).__name__}: {error}"
            _logger.warning("codex-pin-sync tick failed: %s", error)
        self._last_decision = decision
        self._write_state()

    async def _tick_inner(self) -> str:
        return await asyncio.to_thread(self._sync_once)

    # ------------------------------------------------------------------
    # sync (worker thread)
    # ------------------------------------------------------------------

    def _sync_once(self) -> str:
        from governance_rule.execution.codex_postgresql import (
            authority_state,
        )

        state = authority_state()
        live_version = str(state.get("codex_version") or "")
        live_sha = str(state.get("source_sha256") or "")
        if not live_version or not live_sha:
            return "authority-empty"

        text = self._contract_path.read_text(encoding="utf-8")
        match = _BLOCK_RE.search(text)
        if match is None:
            self._last_error = "governance_references block not found"
            return "contract-block-missing"
        body = match.group("body")
        ver_m = re.search(r'"codex_version"\s*:\s*"([^"]+)"', body)
        sha_m = re.search(r'"codex_sha256"\s*:\s*"([^"]+)"', body)
        if ver_m is None or sha_m is None:
            self._last_error = "codex pin keys not found"
            return "contract-keys-missing"
        pinned_version, pinned_sha = ver_m.group(1), sha_m.group(1)
        self._last_pinned = pinned_version
        if pinned_version == live_version and pinned_sha == live_sha:
            return f"in-sync:{live_version}"

        new_body = body.replace(
            f'"codex_version": "{pinned_version}"',
            f'"codex_version": "{live_version}"',
            1,
        ).replace(
            f'"codex_sha256": "{pinned_sha}"',
            f'"codex_sha256": "{live_sha}"',
            1,
        )
        new_text = text[: match.start("body")] + new_body + text[match.end("body") :]
        tmp = self._contract_path.with_suffix(".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, self._contract_path)
        self._audit(
            {
                "op": "repin",
                "from": pinned_version,
                "to": live_version,
                "sha256": live_sha,
            }
        )
        _logger.info(
            "codex pin synced: %s -> %s", pinned_version, live_version
        )
        return f"repinned:{pinned_version}->{live_version}"

    # ------------------------------------------------------------------
    # observability
    # ------------------------------------------------------------------

    def _audit(self, record: dict[str, Any]) -> None:
        try:
            _DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _DEFAULT_LOG.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"at": _iso_now(), **record}, ensure_ascii=False
                    )
                    + "\n"
                )
        except OSError:
            pass

    def _write_state(self) -> None:
        fingerprint = json.dumps(
            {
                "flow": FLOW_ID,
                "last_decision": self._last_decision,
                "last_error": self._last_error,
                "last_pinned": self._last_pinned,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if getattr(self, "_last_state_json", None) == fingerprint:
            return
        payload = {
            "flow": FLOW_ID,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            "last_pinned": self._last_pinned,
            "written_at": _iso_now(),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)
            self._last_state_json = fingerprint
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "flow": FLOW_ID,
            "registered": self._registered,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            "last_pinned": self._last_pinned,
        }


__all__ = ["CodexPinSyncDriver", "FLOW_ID"]
