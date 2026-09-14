"""Command Router — Fault Analysis Handler."""

from __future__ import annotations

import asyncio
from typing import Any, Dict


class FaultAnalysisHandler:
    """Handle app:get-fault-analysis command."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from core_system.fault_analysis_service import get_fault_analysis_service

        query = str(payload.get("query") or "overview").strip().lower()
        service = get_fault_analysis_service()
        try:
            if query == "overview":
                result = await asyncio.to_thread(service.system_health_overview)
            elif query == "patterns":
                faults = await asyncio.to_thread(service.collect_all_faults)
                patterns = await asyncio.to_thread(service.detect_patterns, faults)
                result = {
                    "ok": True,
                    "patterns": [p.as_dict() for p in patterns],
                    "total_faults": len(faults),
                }
            elif query == "knowledge":
                result = await asyncio.to_thread(service.repair_knowledge_summary)
            elif query == "component":
                component = str(payload.get("component") or "").strip()
                if not component:
                    return "app:get-fault-analysis_result", {
                        "ok": False,
                        "error_code": "MISSING_COMPONENT",
                        "message": "component is required for query=component",
                    }
                result = await asyncio.to_thread(service.analyze_component, component)
            elif query == "detail":
                fault_id = str(payload.get("fault_id") or "").strip()
                if not fault_id:
                    return "app:get-fault-analysis_result", {
                        "ok": False,
                        "error_code": "MISSING_Fault_ID",
                        "message": "fault_id is required for query=detail",
                    }
                detail = await asyncio.to_thread(service.fault_detail, fault_id)
                if detail is None:
                    return "app:get-fault-analysis_result", {
                        "ok": False,
                        "error_code": "FAULT_NOT_FOUND",
                        "message": f"No fault found with id={fault_id}",
                    }
                result = {"ok": True, "fault": detail}
            else:
                result = await asyncio.to_thread(service.system_health_overview)
            result.setdefault("ok", True)
            return "app:get-fault-analysis_result", result
        except Exception as error:
            return "app:get-fault-analysis_result", {
                "ok": False,
                "error_code": "FAULT_ANALYSIS_FAILED",
                "message": f"{type(error).__name__}: {error}",
            }