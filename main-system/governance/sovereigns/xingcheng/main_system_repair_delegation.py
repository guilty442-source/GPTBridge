"""Xingcheng Main System Repair Delegation — 星澄主系統修復委派.

Governed delegation path for Xingcheng to request main system repairs.
All repairs go through CentralRepairService with proper authorization.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Final

from governance.sub_sovereigns._base import SubSovereignBase
from core_system.codex_decision import accepted_outcome, refusal_outcome

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.repair")

# Bounded repair intents Xingcheng may request for main system
_XINGCHENG_REPAIR_INTENTS: Final = frozenset({
    "repair.main-system.inspect",      # Inspect main system for issues
    "repair.main-system.targeted",     # Targeted source repair
    "repair.main-system.tool",         # Tool executable rebuild
    "repair.main-system.database",     # Database integrity check
    "repair.main-system.learn",        # Learn from repair outcome
})


class XingchengMainSystemRepairMixin:
    """Mixin for Xingcheng sovereign to delegate main system repairs."""

    # These intents are added to Xingcheng's allowlist
    _REPAIR_INTENTS = _XINGCHENG_REPAIR_INTENTS

    async def _adjudicate_repair_main_system_inspect(
        self, request: Any
    ) -> Any:
        """Inspect main system for repair opportunities."""
        from tasks.central_repair import CentralRepairService

        service = CentralRepairService(
            project_root=Path(getattr(self.app, "project_root", ".")).resolve(),
            repair_data_root=Path(
                getattr(self.app, "repair_data_root", "main-system/data/automatic-repair")
            ).resolve(),
        )

        # Run inspection
        result = await asyncio.to_thread(service.inspect_owned_databases)

        return accepted_outcome(
            {
                "action": "repair.main-system.inspect",
                "domain": "main-system",
                "result": result,
            },
            self.verified_basis("A302"),
        )

    async def _adjudicate_repair_main_system_targeted(
        self, request: Any
    ) -> Any:
        """Request targeted source repair on main system."""
        from tasks.central_repair_operations import CentralRepairOperationsMixin
        from pathlib import Path

        relative_path = str(request.payload.get("relative_path", "")).strip()
        if not relative_path:
            return refusal_outcome(
                "missing relative_path",
                self.verified_basis("A302"),
            )

        # Verify path is within governed source roots
        from tasks.source_repair import SOURCE_ROOTS
        project_root = Path(getattr(self.app, "project_root", ".")).resolve()
        target = (project_root / relative_path).resolve()

        if not any(
            target.relative_to(project_root).as_posix().startswith(str(root).rstrip("/") + "/")
            for root in SOURCE_ROOTS
        ):
            return refusal_outcome(
                f"path {relative_path} outside governed source roots",
                self.verified_basis("A302"),
            )

        # Create repair service
        service = CentralRepairService(
            project_root=project_root,
            repair_data_root=Path(
                getattr(self.app, "repair_data_root", "main-system/data/automatic-repair")
            ).resolve(),
        )

        # Execute targeted repair
        result = await asyncio.to_thread(
            service.self_repair_targeted_source,
            relative_path,
        )

        return accepted_outcome(
            {
                "action": "repair.main-system.targeted",
                "domain": "main-system",
                "target": relative_path,
                "result": result,
            },
            self.verified_basis("A302"),
        )

    async def _adjudicate_repair_main_system_tool(
        self, request: Any
    ) -> Any:
        """Request tool executable rebuild on main system."""
        from tasks.central_repair_operations import CentralRepairOperationsMixin
        from pathlib import Path

        tool_id = str(request.payload.get("tool_id", "")).strip()
        if not tool_id:
            return refusal_outcome(
                "missing tool_id",
                self.verified_basis("A302"),
            )

        # Create repair service
        service = CentralRepairService(
            project_root=Path(getattr(self.app, "project_root", ".")).resolve(),
            repair_data_root=Path(
                getattr(self.app, "repair_data_root", "main-system/data/automatic-repair")
            ).resolve(),
        )

        # Execute tool rebuild
        result = await asyncio.to_thread(
            service.rebuild_tool_executable,
            tool_id,
        )

        return accepted_outcome(
            {
                "action": "repair.main-system.tool",
                "domain": "main-system",
                "tool_id": tool_id,
                "result": result,
            },
            self.verified_basis("A302"),
        )

    async def _adjudicate_repair_main_system_database(
        self, request: Any
    ) -> Any:
        """Request database integrity check on main system."""
        from tasks.central_repair import CentralRepairService
        from pathlib import Path

        service = CentralRepairService(
            project_root=Path(getattr(self.app, "project_root", ".")).resolve(),
            repair_data_root=Path(
                getattr(self.app, "repair_data_root", "main-system/data/automatic-repair")
            ).resolve(),
        )

        result = await asyncio.to_thread(service.inspect_owned_databases)

        return accepted_outcome(
            {
                "action": "repair.main-system.database",
                "domain": "main-system",
                "result": result,
            },
            self.verified_basis("A302"),
        )

    async def _adjudicate_repair_main_system_learn(
        self, request: Any
    ) -> Any:
        """Record repair outcome for learning."""
        from tasks.repair_learning import (
            RepairLearner,
            RepairLearningStore,
            ErrorSignature,
        )
        from pathlib import Path

        payload = request.payload or {}
        error_class = str(payload.get("error_class", ""))
        message_pattern = str(payload.get("message_pattern", ""))
        failure_code = str(payload.get("failure_code", ""))
        file_context = str(payload.get("file_context", ""))
        target_tool_id = str(payload.get("target_tool_id", ""))
        remedy = str(payload.get("remedy", ""))
        success = bool(payload.get("success", False))

        if not error_class or not message_pattern:
            return refusal_outcome(
                "missing error_class or message_pattern",
                self.verified_basis("A302"),
            )

        root = Path(getattr(self.app, "repair_data_root", "main-system/data/automatic-repair")).resolve()
        learner = RepairLearner(RepairLearningStore(root))

        signature = ErrorSignature(
            error_class=error_class,
            message_pattern=message_pattern,
            failure_code=failure_code,
            file_context=file_context,
            target_tool_id=target_tool_id or None,
        )

        await asyncio.to_thread(
            learner.record_outcome,
            signature,
            remedy,
            success,
        )

        return accepted_outcome(
            {
                "action": "repair.main-system.learn",
                "domain": "main-system",
                "signature": signature.signature_hash,
                "remedy": remedy,
                "success": success,
            },
            self.verified_basis("A302"),
        )


def register_xingcheng_repair_delegation(app: Any) -> None:
    """Register repair delegation handlers on Xingcheng sovereign."""
    from governance.sovereigns.xingcheng import XingchengSovereign

    # Add repair intents to Xingcheng's allowlist
    if hasattr(app, "xingcheng_sovereign"):
        sovereign = app.xingcheng_sovereign
        sovereign._INTENT_ALLOWLIST |= _XINGCHENG_REPAIR_INTENTS
        sovereign.__class__ = type(
            "XingchengSovereignWithRepair",
            (XingchengSovereign, XingchengMainSystemRepairMixin),
            {},
        )
        _logger.info("Xingcheng main system repair delegation registered")


__all__ = [
    "XingchengMainSystemRepairMixin",
    "register_xingcheng_repair_delegation",
]