"""Baseline — versioned performance baseline storage.

Stores profile summaries as versioned JSON evidence so future changes can
answer "did this make things faster or slower?"  This is a file-based
baseline store, not a SQL gate — it does not block commits or add audit
checks (per the V1 constraint: no new governance Gate).
"""
from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import process_metrics as _metrics


BASELINE_VERSION = "1.0"


@dataclass(frozen=True)
class MachineProfile:
    """Machine profile required for reproduction (A358)."""
    python_version: str
    platform: str
    processor: str
    cpu_count: int
    memory_total_bytes: int
    machine: str
    baseline_tool_version: str = BASELINE_VERSION


def capture_machine_profile() -> MachineProfile:
    """Capture the current machine profile."""
    cpu_count = _metrics.cpu_count()
    mem = max(0, _metrics.system_memory_total_bytes())
    return MachineProfile(
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        processor=platform.processor() or "unknown",
        cpu_count=cpu_count,
        memory_total_bytes=mem,
        machine=platform.machine(),
    )


@dataclass(frozen=True)
class BaselineRecord:
    """One versioned baseline record."""
    baseline_id: str
    baseline_version: str
    capability_id: str
    recorded_at: str
    machine_profile: dict[str, Any]
    python_metrics: dict[str, Any]
    native_metrics: dict[str, Any] | None
    hotspot_class: str
    verdict: str
    notes: str = ""


class BaselineStore:
    """File-based versioned baseline store.

    Records are appended to a JSON file; each record has a monotonic
    ``baseline_id`` so regressions can be compared across versions.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8") or "[]")

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def record(self, record: BaselineRecord) -> str:
        """Append a baseline record; returns the baseline_id."""
        records = self._load()
        records.append(asdict(record))
        self._save(records)
        return record.baseline_id

    def latest(self, capability_id: str) -> BaselineRecord | None:
        """Get the most recent baseline for a capability."""
        records = self._load()
        matching = [r for r in records if r["capability_id"] == capability_id]
        if not matching:
            return None
        latest = matching[-1]
        return BaselineRecord(
            baseline_id=latest["baseline_id"],
            baseline_version=latest["baseline_version"],
            capability_id=latest["capability_id"],
            recorded_at=latest["recorded_at"],
            machine_profile=latest["machine_profile"],
            python_metrics=latest["python_metrics"],
            native_metrics=latest.get("native_metrics"),
            hotspot_class=latest["hotspot_class"],
            verdict=latest["verdict"],
            notes=latest.get("notes", ""),
        )

    def history(self, capability_id: str) -> list[BaselineRecord]:
        """Get all baselines for a capability, oldest first."""
        records = self._load()
        matching = [r for r in records if r["capability_id"] == capability_id]
        return [
            BaselineRecord(
                baseline_id=r["baseline_id"],
                baseline_version=r["baseline_version"],
                capability_id=r["capability_id"],
                recorded_at=r["recorded_at"],
                machine_profile=r["machine_profile"],
                python_metrics=r["python_metrics"],
                native_metrics=r.get("native_metrics"),
                hotspot_class=r["hotspot_class"],
                verdict=r["verdict"],
                notes=r.get("notes", ""),
            )
            for r in matching
        ]

    def next_id(self, capability_id: str) -> str:
        """Generate the next monotonic baseline_id for a capability."""
        history = self.history(capability_id)
        seq = len(history) + 1
        return f"{capability_id}-v{seq:04d}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "BASELINE_VERSION",
    "MachineProfile",
    "capture_machine_profile",
    "BaselineRecord",
    "BaselineStore",
    "now_iso",
]
