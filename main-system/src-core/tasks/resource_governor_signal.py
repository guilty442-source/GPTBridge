"""§10.64 — resource governor state signals for backend consumers.

The standalone ``scripts/resource-governor.py`` writes
``runtime/state/resource-governor.json`` every cycle with the worker
aggregate ledger, the hysteresis regulation state and the
``worker_admission_hold`` flag.  These helpers let backend tasks consult
that state cheaply (one small JSON read per call, caller-chosen
cadence).

Both helpers fail open on missing/unreadable state: a dead governor
must not permanently pause periodic work or block activation — the
signals are only honoured while the governor actively publishes them.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

_STATE_FILE = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "resource-governor.json"
)
_RULES_FILE = (
    Path(__file__).resolve().parents[2]
    / "config" / "resource-governor-rules.json"
)
_MODE_AUDIT_FILE = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "resource-mode-audit.jsonl"
)
_ADVISOR_STATE_FILE = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "resource-mode-advisor.json"
)
GOVERNOR_MODES = ("low", "medium", "high")

# Night power-saving schedule — 22:00-07:00 low (省電), default auto.
# Configured in ``resource-governor-rules.json`` via ``power_saving_schedule``
# {enabled, start, end, mode}.  Missing file/key falls back to enabled-true
# with the same 22-07 low defaults so the requested behaviour is on by default.
_POWER_SAVING_DEFAULT_ENABLED = True
_POWER_SAVING_DEFAULT_START = "22:00"
_POWER_SAVING_DEFAULT_END = "07:00"
_POWER_SAVING_DEFAULT_MODE = "low"

# Auto-mode advisor control law (§10.64 demand-driven tier selection).
# Evaluated by the governed ``resource-mode-advisor`` automation flow;
# only acts while the rules file carries ``auto_mode: true`` — a manual
# UI/CLI selection flips the flag off so the advisor never fights the user.
_AUTO_STREAK: int = 3            # consecutive evals before high/medium switch
_AUTO_COOLDOWN_S: float = 600.0  # min seconds between auto mode changes
_STRAIN_CPU_PCT: float = 85.0    # machine-wide load -> low immediately
_STRAIN_MEM_PCT: float = 90.0
_HEADROOM_CPU_PCT: float = 60.0  # headroom required to allow high
_HEADROOM_MEM_PCT: float = 75.0
_DEMAND_FACTOR: float = 0.8      # worker ledger >= 80% of budget = demand


def _state() -> dict:
    try:
        payload = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def regulation_active() -> bool:
    """True while the governor's worker-budget control law is regulating
    (§10.64 ④: pausable periodic jobs defer while this holds).  The
    pre-throttle tier (>=80% of budget, 2026-09-22 strict INT-10 ruling)
    counts as regulating so periodic work yields before the budget is
    crossed rather than after."""
    regulation = _state().get("regulation")
    return isinstance(regulation, dict) and (
        regulation.get("active") is True or regulation.get("pre") is True
    )


def worker_admission_hold() -> bool:
    """True while the governor asks the backend to hold new worker
    starts (§10.64 ⑤ fail-closed load shedding)."""
    return _state().get("worker_admission_hold") is True


# ----------------------------------------------------------------------
# Performance mode control (low / medium / high)
# ----------------------------------------------------------------------
def _rules() -> dict:
    try:
        payload = json.loads(_RULES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_time_to_minutes(value: str | None, fallback: str) -> int:
    """Parse ``HH:MM`` (or ``HH``) to minutes since midnight; fallback on error."""
    text = str(value).strip() if isinstance(value, str) and value.strip() else fallback
    fallback_minutes = _parse_time_to_minutes_fallback(fallback)
    try:
        parts = text.split(":")
        hour = int(parts[0].strip())
        minute = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else 0
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return hour * 60 + minute
    except (ValueError, TypeError, IndexError, AttributeError):
        return fallback_minutes


def _parse_time_to_minutes_fallback(value: str) -> int:
    try:
        parts = str(value).split(":")
        hour = int(parts[0].strip())
        minute = int(parts[1].strip()) if len(parts) > 1 else 0
        return max(0, min(23, hour)) * 60 + max(0, min(59, minute))
    except Exception:
        return 22 * 60  # 22:00 safe default


def _resolve_power_saving_schedule(rules: dict) -> dict:
    """Resolve ``power_saving_schedule`` from rules with defaults (enabled 22-07 low)."""
    raw = rules.get("power_saving_schedule")
    if not isinstance(raw, dict):
        return {
            "enabled": _POWER_SAVING_DEFAULT_ENABLED,
            "start": _POWER_SAVING_DEFAULT_START,
            "end": _POWER_SAVING_DEFAULT_END,
            "mode": _POWER_SAVING_DEFAULT_MODE,
        }
    enabled = raw.get("enabled")
    # default enabled=True per user request (夜間省電預設開啟)
    if enabled is None:
        enabled = _POWER_SAVING_DEFAULT_ENABLED
    else:
        enabled = bool(enabled)
    start = raw.get("start") if isinstance(raw.get("start"), str) and raw.get("start").strip() else _POWER_SAVING_DEFAULT_START
    end = raw.get("end") if isinstance(raw.get("end"), str) and raw.get("end").strip() else _POWER_SAVING_DEFAULT_END
    mode_raw = raw.get("mode")
    mode = str(mode_raw).strip().lower() if isinstance(mode_raw, str) and mode_raw.strip() else _POWER_SAVING_DEFAULT_MODE
    if mode not in GOVERNOR_MODES:
        mode = _POWER_SAVING_DEFAULT_MODE
    return {"enabled": enabled, "start": start, "end": end, "mode": mode}


def _is_power_saving_time(now_minutes: int, start_minutes: int, end_minutes: int) -> bool:
    """Whether ``now`` falls inside the nightly window (wrap-around aware)."""
    if start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    # wraps midnight — e.g. 22:00-07:00
    return now_minutes >= start_minutes or now_minutes < end_minutes


def is_power_saving_hours(now: datetime | None = None) -> bool:
    """Public helper: whether local time is inside the configured nightly window."""
    rules = _rules()
    schedule = _resolve_power_saving_schedule(rules)
    if not schedule["enabled"]:
        return False
    start_min = _parse_time_to_minutes(schedule["start"], _POWER_SAVING_DEFAULT_START)
    end_min = _parse_time_to_minutes(schedule["end"], _POWER_SAVING_DEFAULT_END)
    local_now = now if isinstance(now, datetime) else datetime.now().astimezone()
    now_min = local_now.hour * 60 + local_now.minute
    return _is_power_saving_time(now_min, start_min, end_min)


def power_saving_schedule() -> dict:
    """Return the resolved power-saving schedule and current window state."""
    rules = _rules()
    schedule = _resolve_power_saving_schedule(rules)
    start_min = _parse_time_to_minutes(schedule["start"], _POWER_SAVING_DEFAULT_START)
    end_min = _parse_time_to_minutes(schedule["end"], _POWER_SAVING_DEFAULT_END)
    local_now = datetime.now().astimezone()
    now_min = local_now.hour * 60 + local_now.minute
    active = bool(schedule["enabled"] and _is_power_saving_time(now_min, start_min, end_min))
    return {**schedule, "active": active, "now": local_now.strftime("%H:%M")}


def governor_mode() -> dict:
    """Report the configured mode and the mode the live governor applied.

    ``mode`` is the value in the rules file (takes effect on the next
    governor cycle, ~20 s); ``applied`` is what the running governor
    last published in its state file.  ``None`` means no live governor
    has reported yet.
    """
    rules = _rules()
    modes = rules.get("modes")
    known = list(modes) if isinstance(modes, dict) else list(GOVERNOR_MODES)
    configured = rules.get("mode")
    state = _state()
    applied = state.get("mode")
    advisor = _advisor_state()
    schedule = _resolve_power_saving_schedule(rules)
    start_min = _parse_time_to_minutes(schedule["start"], _POWER_SAVING_DEFAULT_START)
    end_min = _parse_time_to_minutes(schedule["end"], _POWER_SAVING_DEFAULT_END)
    local_now = datetime.now().astimezone()
    now_min = local_now.hour * 60 + local_now.minute
    saving_active = bool(schedule["enabled"] and _is_power_saving_time(now_min, start_min, end_min))
    return {
        "mode": configured if isinstance(configured, str) else "medium",
        "applied": applied if isinstance(applied, str) else None,
        "modes": known,
        "auto_mode": rules.get("auto_mode") is True,
        "running": bool(state),
        "advisor": {
            "target": advisor.get("target"),
            "reason": advisor.get("reason"),
            "at": advisor.get("at"),
        } if isinstance(advisor, dict) and advisor else None,
        "rules_error": state.get("features", {}).get("rules_error")
        if isinstance(state.get("features"), dict)
        else None,
        "power_saving_schedule": {**schedule, "active": saving_active},
    }


def _advisor_state() -> dict:
    try:
        payload = json.loads(_ADVISOR_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_advisor_state(record: dict) -> None:
    try:
        _ADVISOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ADVISOR_STATE_FILE.with_suffix(
            _ADVISOR_STATE_FILE.suffix + ".tmp"
        )
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, _ADVISOR_STATE_FILE)
    except OSError:
        pass


def _commit_rules(update: dict, *, actor: str) -> dict:
    """Atomically merge ``update`` into the rules file and audit the change."""
    if not _RULES_FILE.is_file():
        raise ValueError(f"rules file missing: {_RULES_FILE}")
    rules = _rules()
    previous = rules.get("mode")
    rules.update(update)
    tmp = _RULES_FILE.with_suffix(_RULES_FILE.suffix + ".tmp")
    tmp.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, _RULES_FILE)
    audit_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "previous": previous if isinstance(previous, str) else None,
        "mode": rules.get("mode") if isinstance(rules.get("mode"), str) else None,
        "auto_mode": rules.get("auto_mode") is True,
    }
    try:
        _MODE_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _MODE_AUDIT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(audit_entry, ensure_ascii=False, sort_keys=True)
                + "\n"
            )
    except OSError:
        pass
    return governor_mode()


def set_governor_mode(mode: str, *, actor: str = "authenticated-ui") -> dict:
    """Persist a new performance mode into the rules file and audit it.

    The write is atomic (tmp + replace); the running governor hot-reloads
    the file on its next cycle, so no restart is needed.  An explicit
    selection disables ``auto_mode`` — user intent always wins over the
    demand advisor.  Raises ``ValueError`` for an unknown mode or an
    unreadable/unwritable rules file so callers can fail visibly.
    """
    mode = str(mode or "").strip().lower()
    rules = _rules()
    raw_modes = rules.get("modes")
    known = (
        {str(k) for k in raw_modes}
        if isinstance(raw_modes, dict) and raw_modes
        else set(GOVERNOR_MODES)
    )
    if mode not in known:
        raise ValueError(f"unknown governor mode: {mode!r}")
    return _commit_rules({"mode": mode, "auto_mode": False}, actor=actor)


def set_governor_auto(enabled: bool, *, actor: str = "authenticated-ui") -> dict:
    """Enable/disable demand-driven mode selection by the advisor."""
    return _commit_rules({"auto_mode": bool(enabled)}, actor=actor)


def auto_adjust_mode() -> dict:
    """One advisor evaluation: pick a mode from live demand signals.

    Control law (all signals from the governor's own state file):

    * ``low``    — night power-saving 22:00-07:00 (省電) when
      ``power_saving_schedule.enabled`` and ``auto_mode``; applied
      immediately so 22:00 switches promptly.  Highest priority.
    * ``low``    — responsiveness strained, or machine CPU >= 85 %, or RAM
      >= 90 %; applied immediately (interactivity wins).
    * ``high``   — worker demand (admission hold or ledger >= 80 % of
      budget) AND machine headroom (CPU < 60 %, RAM < 75 %); needs
      ``_AUTO_STREAK`` consecutive evaluations.
    * ``medium`` — everything else; needs ``_AUTO_STREAK`` evaluations.

    A ``_AUTO_COOLDOWN_S`` cooldown bounds oscillation.  Inert unless the
    rules file sets ``auto_mode: true`` (預設自動).  The evaluation record
    is always persisted so the control surface can show *why* a mode was
    chosen.
    """
    record: dict = {
        "at": datetime.now(timezone.utc).isoformat(),
        "enabled": False,
        "target": None,
        "streak": 0,
        "changed": False,
        "reason": "",
    }
    rules = _rules()
    if rules.get("auto_mode") is not True:
        record["reason"] = "auto-disabled"
        _write_advisor_state(record)
        return record
    record["enabled"] = True
    state = _state()
    if not state:
        record["reason"] = "governor-not-running"
        _write_advisor_state(record)
        return record
    features = state.get("features")
    if isinstance(features, dict) and features.get("rules_error"):
        record["reason"] = "rules-error"
        _write_advisor_state(record)
        return record

    regulation = state.get("regulation") or {}
    ledger = state.get("worker_ledger") or {}
    cpu_load = float(state.get("cpu_load_pct") or 0.0)
    mem_used = float(state.get("mem_used_pct") or 0.0)
    strained = regulation.get("strained") is True
    worker_cpu = float(ledger.get("cpu_pct") or 0.0)
    worker_ram = float(ledger.get("ram_pct") or 0.0)
    budget_cpu = float(ledger.get("budget_cpu_pct") or 0.0)
    budget_ram = float(ledger.get("budget_ram_pct") or 0.0)
    demand = (
        state.get("worker_admission_hold") is True
        or (budget_cpu > 0 and worker_cpu >= budget_cpu * _DEMAND_FACTOR)
        or (budget_ram > 0 and worker_ram >= budget_ram * _DEMAND_FACTOR)
    )
    headroom = cpu_load < _HEADROOM_CPU_PCT and mem_used < _HEADROOM_MEM_PCT

    # 夜間省電排程 — 22:00-07:00 預設切 low（省電），僅在 auto_mode 下生效。
    schedule = _resolve_power_saving_schedule(rules)
    start_min = _parse_time_to_minutes(schedule["start"], _POWER_SAVING_DEFAULT_START)
    end_min = _parse_time_to_minutes(schedule["end"], _POWER_SAVING_DEFAULT_END)
    local_now = datetime.now().astimezone()
    now_min = local_now.hour * 60 + local_now.minute
    in_power_saving = bool(schedule["enabled"] and _is_power_saving_time(now_min, start_min, end_min))

    urgent = False
    if in_power_saving:
        # 最高優先：夜間窗口內強制切至省電模式（預設 low），立即生效確保 22:00 準時進入
        target = schedule["mode"]
        reason = f"night-power-saving ({schedule['start']}-{schedule['end']})"
        urgent = True
    elif strained or cpu_load >= _STRAIN_CPU_PCT or mem_used >= _STRAIN_MEM_PCT:
        target, urgent = "low", True
        reason = "strained" if strained else "machine-overload"
    elif demand and headroom:
        target, reason = "high", "worker-demand"
    elif demand:
        target, reason = "medium", "demand-no-headroom"
    else:
        target, reason = "medium", "baseline"

    current = rules.get("mode")
    previous = _advisor_state()
    last_switch = float(previous.get("last_switch_at") or 0.0)
    streak = (
        int(previous.get("streak") or 0) + 1
        if previous.get("target") == target
        else 1
    )
    record.update({
        "target": target,
        "current": current if isinstance(current, str) else None,
        "streak": streak,
        "reason": reason,
        "power_saving": {
            "enabled": schedule["enabled"],
            "start": schedule["start"],
            "end": schedule["end"],
            "mode": schedule["mode"],
            "active": in_power_saving,
            "now": local_now.strftime("%H:%M"),
        },
        "signals": {
            "strained": strained,
            "cpu_load_pct": cpu_load,
            "mem_used_pct": mem_used,
            "worker_demand": demand,
            "headroom": headroom,
            "worker_cpu_pct": worker_cpu,
            "worker_ram_pct": worker_ram,
            "power_saving_active": in_power_saving,
        },
    })
    if target == current:
        record["reason"] = f"{reason} (already {target})"
        record["streak"] = 0
    elif urgent:
        pass
    elif streak < _AUTO_STREAK:
        record["reason"] = f"{reason} (streak {streak}/{_AUTO_STREAK})"
    elif time.time() - last_switch < _AUTO_COOLDOWN_S:
        record["reason"] = f"{reason} (cooldown)"
    else:
        pass
    should_apply = target != current and (
        urgent
        or (
            streak >= _AUTO_STREAK
            and time.time() - last_switch >= _AUTO_COOLDOWN_S
        )
    )
    if should_apply:
        try:
            _commit_rules({"mode": target}, actor="resource-mode-advisor")
        except (ValueError, OSError) as exc:
            record["reason"] = f"{reason} (commit-failed: {exc})"
        else:
            record["changed"] = True
            record["last_switch_at"] = time.time()
            record["reason"] = f"{reason} (applied)"
    elif previous.get("last_switch_at"):
        record["last_switch_at"] = last_switch
    _write_advisor_state(record)
    return record


def set_power_saving_schedule(
    enabled: bool | None = None,
    start: str | None = None,
    end: str | None = None,
    mode: str | None = None,
    *,
    actor: str = "authenticated-ui",
) -> dict:
    """Update ``power_saving_schedule`` in the rules file and audit it."""
    rules = _rules()
    raw = rules.get("power_saving_schedule")
    schedule = _resolve_power_saving_schedule(rules) if isinstance(raw, dict) else _resolve_power_saving_schedule(rules)
    if enabled is not None:
        schedule["enabled"] = bool(enabled)
    if start is not None:
        schedule["start"] = str(start).strip() or _POWER_SAVING_DEFAULT_START
    if end is not None:
        schedule["end"] = str(end).strip() or _POWER_SAVING_DEFAULT_END
    if mode is not None:
        candidate = str(mode).strip().lower()
        if candidate in GOVERNOR_MODES:
            schedule["mode"] = candidate
        elif candidate:
            raise ValueError(f"unknown power-saving mode: {candidate!r}")
    # validate times by parsing (will fallback on bad format, but we reject badly formatted explicitly)
    _parse_time_to_minutes(schedule["start"], _POWER_SAVING_DEFAULT_START)
    _parse_time_to_minutes(schedule["end"], _POWER_SAVING_DEFAULT_END)
    return _commit_rules({"power_saving_schedule": schedule}, actor=actor)


__all__ = [
    "GOVERNOR_MODES",
    "auto_adjust_mode",
    "governor_mode",
    "is_power_saving_hours",
    "power_saving_schedule",
    "regulation_active",
    "set_governor_auto",
    "set_governor_mode",
    "set_power_saving_schedule",
    "worker_admission_hold",
]
