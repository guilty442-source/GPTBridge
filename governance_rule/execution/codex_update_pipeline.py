"""Automatic Codex update pipeline — isolate, change, wire, release, refresh.

Governor-directed order (2026-09-17): 先隔離 → 後執行變更 → 再執行接線 →
再解除隔離 → 前端連線刷新.

法典依據 (A382/A488 non-disruptive amendment flow; A537/A538 automatic
synchronization of updates, the five Chinese mirror parts and architecture
artifacts; A383 isolation; A446 bounded stages):
- ISOLATE copies the current generation into an isolated staging root and
  marks it non-authoritative; the live generation is never partially mutated.
- EXECUTE-CHANGE applies the prepared successor database inside the isolation,
  normalizes the version identity, validates the staged generation and records
  the mirror-quality evidence row in that same generation.
- WIRE atomically replaces the published database and mirror parts, restores
  read-only protection and produces the typed notification payload.
- RELEASE-ISOLATION verifies the published generation and releases the fence.
- FRONTEND-REFRESH emits the frontend connection refresh request so UI
  projections reconnect to the published authority generation.

The module never runs implicitly.  ``apply=False`` (default) stops after the
change was validated in isolation so the automation can be rehearsed; only the
governed executor runs ``apply=True`` and it writes only inside the explicit
codex root.  A rejected staging copy is deleted; a released one is retained
as the rollback snapshot with its evidence record.

Boundary: seal-root recomputation and revision/lineage/certification rows
stay owned by the amendment pipeline (A487/A537/A438); this module wires a
prepared successor, records the mirror-quality evidence row (so the
published generation passes the codex-integrity audit) and restores
read-only protection.  The flow uses no external signatures — the
unanimous five-sovereign audit certificate closes the seal.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Callable, Mapping


try:
    from governance_rule.execution.codex_update_pipeline_common import (
        DATABASE_NAME,
        FRONTEND_REFRESH_CHANNELS,
        ISOLATION_MARKER,
        PART_NAMES,
        RELEASED_MARKER,
        REFRESH_REQUEST,
        UPDATE_FLOW_IDENTITY,
        UPDATE_PHASES,
        AutoUpdateResult,
        CodexUpdateError,
        IsolatedStage,
        PhaseRecord,
        _atomic_replace,
        _set_read_only,
    )
    from governance_rule.execution.codex_update_pipeline_execute import (
        architecture_sync_errors,
        execute_staged_change,
    )
    from governance_rule.execution.codex_update_pipeline_isolate import (
        isolate_generation,
    )
    from governance_rule.execution.codex_update_pipeline_publish import (
        _discard_staging,
        frontend_refresh,
        release_isolation,
        wire_generation,
    )
except ImportError:  # flat-script import
    from codex_update_pipeline_common import (
        DATABASE_NAME,
        FRONTEND_REFRESH_CHANNELS,
        ISOLATION_MARKER,
        PART_NAMES,
        RELEASED_MARKER,
        REFRESH_REQUEST,
        UPDATE_FLOW_IDENTITY,
        UPDATE_PHASES,
        AutoUpdateResult,
        CodexUpdateError,
        IsolatedStage,
        PhaseRecord,
        _atomic_replace,
        _set_read_only,
    )
    from codex_update_pipeline_execute import (
        architecture_sync_errors,
        execute_staged_change,
    )
    from codex_update_pipeline_isolate import isolate_generation
    from codex_update_pipeline_publish import (
        _discard_staging,
        frontend_refresh,
        release_isolation,
        wire_generation,
    )


def run_auto_update(
    codex_root: str | Path,
    staging_root: str | Path,
    *,
    prepared_database: str | Path | None = None,
    version: str | None = None,
    apply: bool = False,
    notify: Callable[[Mapping[str, object]], Any] | None = None,
    bookkeeping: Mapping[str, str] | None = None,
) -> AutoUpdateResult:
    """Run the directed update order; stop fail-closed at the first failure."""
    phases: list[PhaseRecord] = []
    try:
        stage = isolate_generation(codex_root, staging_root)
    except CodexUpdateError as error:
        phases.append(PhaseRecord(error.phase, False, error.reason))
        return AutoUpdateResult(False, False, "", tuple(phases))
    phases.append(
        PhaseRecord(
            "isolate",
            True,
            "live generation isolated; staged copy is non-authoritative",
            {"fence_id": stage.fence_id, "source_version": stage.source_version},
        )
    )
    change = execute_staged_change(
        stage, prepared_database=prepared_database, version=version
    )
    phases.append(change)
    if not change.ok:
        _discard_staging(stage)
        return AutoUpdateResult(False, False, stage.source_version, tuple(phases))
    return _finish_update(stage, phases, version, apply, notify)


def _finish_update(
    stage: IsolatedStage,
    phases: list[PhaseRecord],
    version: str | None,
    apply: bool,
    notify: Callable[[Mapping[str, object]], Any] | None,
) -> AutoUpdateResult:
    """Dry-run stop, or wire + release + frontend refresh when applying."""
    target_version = version or stage.source_version
    if not apply:
        _discard_staging(stage)
        return AutoUpdateResult(True, False, target_version, tuple(phases))
    wire = wire_generation(stage)
    phases.append(wire)
    if not wire.ok:
        return AutoUpdateResult(False, True, target_version, tuple(phases))
    release = release_isolation(stage)
    phases.append(release)
    refresh = frontend_refresh(stage, notify)
    phases.append(refresh)
    return AutoUpdateResult(
        ok=wire.ok and release.ok,
        applied=True,
        version=target_version,
        phases=tuple(phases),
        refresh_pending=not bool(refresh.evidence.get("delivered")),
    )


__all__ = [
    "AutoUpdateResult",
    "CodexUpdateError",
    "DATABASE_NAME",
    "FRONTEND_REFRESH_CHANNELS",
    "IsolatedStage",
    "PART_NAMES",
    "PhaseRecord",
    "UPDATE_FLOW_IDENTITY",
    "UPDATE_PHASES",
    "architecture_sync_errors",
    "execute_staged_change",
    "frontend_refresh",
    "isolate_generation",
    "release_isolation",
    "run_auto_update",
    "wire_generation",
]
