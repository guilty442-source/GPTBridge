"""Search Trace — 內部搜尋執行紀錄，方便除錯與效能分析。

對應需求 42：
  Trace 包含：Request ID、Tool Route、Query Rewrite、Provider、Provider Latency、
  Fetch Latency、Parser Result、Chunk Count、Reranker Result、Selected Evidence、
  Token Usage、Cache Hit、Error
  Trace 與正常對話內容分離。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceEvent:
    """單一 trace 事件。"""

    stage: str
    timestamp: float = field(default_factory=time.time)
    duration: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class SearchTrace:
    """完整搜尋 trace。"""

    request_id: str
    events: list[TraceEvent] = field(default_factory=list)
    started_time: float = field(default_factory=time.time)
    finished_time: float | None = None

    def add_event(
        self,
        stage: str,
        *,
        duration: float = 0.0,
        detail: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            stage=stage,
            duration=duration,
            detail=detail or {},
            error=error,
        )
        self.events.append(event)
        return event

    def finish(self) -> None:
        self.finished_time = time.time()

    @property
    def total_duration(self) -> float:
        end = self.finished_time or time.time()
        return end - self.started_time

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "started_time": self.started_time,
            "finished_time": self.finished_time,
            "total_duration": round(self.total_duration, 4),
            "event_count": len(self.events),
            "events": [
                {
                    "stage": e.stage,
                    "timestamp": e.timestamp,
                    "duration": round(e.duration, 4),
                    "detail": e.detail,
                    "error": e.error,
                }
                for e in self.events
            ],
        }

    def summary(self) -> dict[str, Any]:
        """簡要摘要（不含完整事件）。"""
        stages: dict[str, float] = {}
        errors: list[str] = []
        for e in self.events:
            stages[e.stage] = stages.get(e.stage, 0.0) + e.duration
            if e.error:
                errors.append(f"{e.stage}: {e.error}")
        return {
            "request_id": self.request_id,
            "total_duration": round(self.total_duration, 4),
            "stage_durations": {k: round(v, 4) for k, v in stages.items()},
            "error_count": len(errors),
            "errors": errors,
        }


__all__ = ["SearchTrace", "TraceEvent"]
