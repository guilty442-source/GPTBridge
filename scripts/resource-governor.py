#!/usr/bin/env python3
"""Adaptive CPU/memory governor for the local workstation.

Monitors every process owned by the current user and lowers resource
pressure automatically:

  * sustained CPU hog  -> priority lowered to BELOW_NORMAL
  * extreme CPU hog    -> CPU affinity capped to half of the logical CPUs
  * large idle process -> working set trimmed (paged out, reloaded on use)

Process Lasso-inspired tier (2026-09-22; every new *action* defaults to
OFF and is enabled through CLI flags or the rules file — the monitoring
surface is always on):

  * ProBalance           -> responsiveness probe (scheduling latency)
                            temporarily demotes foreground-external CPU
                            hogs while the system is strained
  * CPU limiter          -> Job Object CPU rate hard cap for sustained
                            offenders (kernel-enforced, releasable)
  * background mode      -> PROCESS_MODE_BACKGROUND (I/O + memory priority)
  * EcoQoS               -> PowerThrottling execution-speed efficiency mode
  * per-program rules    -> main-system/config/resource-governor-rules.json
                            (priority / affinity / cpu_limit_percent /
                            background / ecoqos / exclude)

Actions are reverted once the process stays calm, are logged as JSONL,
and every cycle writes a status snapshot.  Protected system and security
processes (including the user's antivirus) are never touched.

Usage:
  python scripts/resource-governor.py --status
  python scripts/resource-governor.py --once [--dry-run]
  python scripts/resource-governor.py --start / --stop
  python scripts/resource-governor.py --watch
  python scripts/resource-governor.py --install-logon / --uninstall-logon
  python scripts/resource-governor.py --install-task / --uninstall-task
  python scripts/resource-governor.py --watch --probalance --background-mode
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import psutil

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
STATE_DIR: Final[Path] = PROJECT_ROOT / "main-system" / "runtime" / "state"
LOG_DIR: Final[Path] = PROJECT_ROOT / "main-system" / "runtime" / "logs"
STATE_FILE: Final[Path] = STATE_DIR / "resource-governor.json"
LOG_FILE: Final[Path] = LOG_DIR / "resource-governor.jsonl"
LOCK_FILE: Final[Path] = STATE_DIR / "resource-governor.lock"

TASK_NAME: Final[str] = "GPTBridge-ResourceGovernor"
LOGO_NAME: Final[str] = "GPTBridge-ResourceGovernor"
LOGO_HIVE: Final[str] = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_INTERVAL: Final[float] = 20.0
# User policy: autonomous workloads must yield above 10% per-process CPU.
CPU_BUSY_PCT: Final[float] = 10.0
CPU_EXTREME_PCT: Final[float] = 20.0
CPU_CALM_PCT: Final[float] = 5.0
SUSTAIN_SAMPLES: Final[int] = 3
EXTREME_SAMPLES: Final[int] = 6
CALM_SAMPLES: Final[int] = 15
MEM_TRIM_MB: Final[float] = 1500.0
# 全域資源上限（使用者約束：CPU 10%／RAM 80%）。CPU limit is
# enforced by sustained priority/affinity throttling; Windows scheduling is
# not a hard wall-clock quota for arbitrary user processes.
GLOBAL_CPU_LIMIT_PCT: Final[float] = 10.0
GLOBAL_RAM_LIMIT_PCT: Final[float] = 30.0
TRIM_COOLDOWN_SECONDS: Final[float] = 300.0
AFFINITY_MIN_CPUS: Final[int] = 1

# §10.64 — worker aggregate budget (single-core-equivalent CPU / share of
# total RAM) with hysteresis, plus a fail-closed admission-hold signal the
# backend consults before starting new on-demand workers.
WORKER_CPU_BUDGET_PCT: Final[float] = 10.0
WORKER_RAM_BUDGET_PCT: Final[float] = 30.0
# Strict INT-10 semantics (2026-09-22 governor ruling): the FIRST over-budget
# sample engages regulation, and a soft pre-throttle tier engages once the
# worker aggregate reaches 80% of budget so over-budget samples trend to zero.
REGULATE_OVER_SAMPLES: Final[int] = 1
REGULATE_UNDER_SAMPLES: Final[int] = 5
REGULATE_UNDER_FACTOR: Final[float] = 0.8
# While regulating, worker-plane processes are throttled from a much lower
# per-process floor — ten individually-calm workers can still breach the
# aggregate budget.
REGULATED_WORKER_BUSY_PCT: Final[float] = 2.0
# Kill switch: observe only, no actions (§10.64 acceptance ⑤).
GOVERNOR_DISABLE_ENV: Final[str] = "GPTBRIDGE_GOVERNOR_DISABLE"
WORKER_PLANES: Final[frozenset[str]] = frozenset({"worker", "toolbox", "repo-other"})

# ---------------------------------------------------------------------------
# Process Lasso-inspired integration (2026-09-22 governor directive).
# Every new CONTROL action defaults OFF (CLI flag or rules-file defaults);
# the monitoring surface (I/O counters, action state, responsiveness
# metric) is always on.  Actions stay reversible, audited, bounded by the
# existing protections (governance plane, system/security processes, kill
# switch, A593).
# ---------------------------------------------------------------------------
RULES_FILE: Final[Path] = (
    PROJECT_ROOT / "main-system" / "config" / "resource-governor-rules.json"
)
RESP_PROBE_ITERS: Final[int] = 500_000
RESP_PROBE_RUNS: Final[int] = 3
RESP_BASELINE_ALPHA: Final[float] = 0.2
RESP_STRAIN_RATIO: Final[float] = 1.8
RESP_RELEASE_RATIO: Final[float] = 1.2
RESP_STRAIN_SAMPLES: Final[int] = 2
RESP_CALM_SAMPLES: Final[int] = 3
PROBALANCE_MAX_DEMOTIONS: Final[int] = 5
DEFAULT_LIMITER_PERCENT: Final[float] = 10.0
LIMITER_MIN_PERCENT: Final[float] = 1.0
LIMITER_MAX_PERCENT: Final[float] = 100.0
RULE_PRIORITIES: Final[dict[str, int]] = {
    "normal": psutil.NORMAL_PRIORITY_CLASS,
    "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
    "idle": psutil.IDLE_PRIORITY_CLASS,
}

PROCESS_ATTRS: Final[list[str]] = [
    "pid", "name", "exe", "username", "memory_info", "io_counters",
]

PROTECTED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "uninstallmonitor",
        "msi_tracefps",
        "processlasso",
        "processgovernor",
        "rtss",
        "afterburner",
        "system",
        "registry",
        "idle",
        "memcompression",
        "smss",
        "csrss",
        "wininit",
        "winlogon",
        "services",
        "lsass",
        "dwm",
        "fontdrvhost",
        "svchost",
        "audiodg",
        "conhost",
        "ctfmon",
        "sihost",
        "taskhostw",
        "explorer",
        "searchhost",
        "searchindexer",
        "startmenuexperiencehost",
        "shellexperiencehost",
        "textinputhost",
        "runtimebroker",
        "securityhealthservice",
        "securityhealthsystray",
        "msmpeng",
        "nissrv",
        "spoolsv",
        "wudfhost",
        "dllhost",
        "wlanext",
        "python-service",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_action(payload: dict[str, Any]) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": _now(), **payload}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _write_state(payload: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"at": _now(), **payload}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, STATE_FILE)
    except OSError:
        pass


PROCESS_SET_QUOTA: Final[int] = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000


def _trim_working_set(pid: int) -> bool:
    """Ask the OS to page out a process's working set (EmptyWorkingSet)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    handle = kernel32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    try:
        if psapi.EmptyWorkingSet(handle):
            return True
        return bool(
            kernel32.SetProcessWorkingSetSize(
                handle,
                ctypes.c_size_t(-1).value,
                ctypes.c_size_t(-1).value,
            )
        )
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Per-program rules (Process Lasso-style persistent rules).
# ---------------------------------------------------------------------------
class ProgramRule:
    __slots__ = (
        "exclude", "priority", "affinity", "cpu_limit_percent",
        "background", "ecoqos",
    )

    def __init__(self, payload: dict[str, Any]) -> None:
        self.exclude = bool(payload.get("exclude", False))
        priority = payload.get("priority")
        self.priority = (
            RULE_PRIORITIES.get(str(priority).strip().lower())
            if priority not in (None, "")
            else None
        )
        affinity = payload.get("affinity")
        if (
            isinstance(affinity, list)
            and affinity
            and all(isinstance(cpu, (int, float)) and int(cpu) >= 0 for cpu in affinity)
        ):
            self.affinity: list[int] | None = [int(cpu) for cpu in affinity]
        else:
            self.affinity = None
        limit = payload.get("cpu_limit_percent")
        self.cpu_limit_percent = (
            max(LIMITER_MIN_PERCENT, min(LIMITER_MAX_PERCENT, float(limit)))
            if isinstance(limit, (int, float)) and float(limit) > 0
            else 0.0
        )
        self.background = bool(payload.get("background", False))
        self.ecoqos = bool(payload.get("ecoqos", False))


def load_rules(path: Path) -> tuple[dict[str, Any], dict[str, ProgramRule], str | None]:
    """Load the rules file; fail-closed to monitoring-only on any error."""
    if not path.is_file():
        return {}, {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return {}, {}, "root must be an object"
    defaults_raw = payload.get("defaults")
    defaults = defaults_raw if isinstance(defaults_raw, dict) else {}
    programs: dict[str, ProgramRule] = {}
    raw_programs = payload.get("programs")
    if isinstance(raw_programs, dict):
        for key, entry in raw_programs.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            try:
                programs[key.strip().lower()] = ProgramRule(entry)
            except (TypeError, ValueError) as exc:
                return defaults, programs, f"{key}: {type(exc).__name__}: {exc}"
    return defaults, programs, None


def _feature_enabled(flag: bool | None, defaults: dict[str, Any], key: str) -> bool:
    if flag is not None:
        return bool(flag)
    value = defaults.get(key)
    return bool(value) if isinstance(value, bool) else False


def _resolve_features(config: "GovernorConfig", defaults: dict[str, Any]) -> dict[str, Any]:
    limiter = (
        config.limiter_percent_arg
        if config.limiter_percent_arg is not None
        else defaults.get("limiter_percent", DEFAULT_LIMITER_PERCENT)
    )
    ratio = (
        config.resp_ratio_arg
        if config.resp_ratio_arg is not None
        else defaults.get("resp_strain_ratio", RESP_STRAIN_RATIO)
    )
    try:
        limiter_value = float(limiter)
    except (TypeError, ValueError):
        limiter_value = DEFAULT_LIMITER_PERCENT
    try:
        ratio_value = float(ratio)
    except (TypeError, ValueError):
        ratio_value = RESP_STRAIN_RATIO
    return {
        "probalance": _feature_enabled(config.probalance_flag, defaults, "probalance"),
        "cpu_limiter": _feature_enabled(config.cpu_limiter_flag, defaults, "cpu_limiter"),
        "background_mode": _feature_enabled(
            config.background_mode_flag, defaults, "background_mode"
        ),
        "ecoqos": _feature_enabled(config.ecoqos_flag, defaults, "ecoqos"),
        "limiter_percent": max(
            LIMITER_MIN_PERCENT, min(LIMITER_MAX_PERCENT, limiter_value)
        ),
        "resp_ratio": max(1.05, ratio_value),
    }


# ---------------------------------------------------------------------------
# Responsiveness probe (ProBalance signal; monitoring surface).
# ---------------------------------------------------------------------------
def _responsiveness_sample(iters: int = RESP_PROBE_ITERS) -> float:
    """Wall-clock milliseconds for a fixed tiny workload.

    Scheduling delays, CPU contention and frequency throttling all inflate
    the result, so the median over a few runs tracks how responsive the
    machine currently is; the governor itself runs at IDLE priority.
    """
    start = time.perf_counter()
    accumulator = 0
    for value in range(iters):
        accumulator += value
    elapsed = time.perf_counter() - start
    if accumulator < 0:  # pragma: no cover - keeps the loop observable
        time.sleep(0)
    return elapsed * 1000.0


def measure_responsiveness(runs: int = RESP_PROBE_RUNS) -> float:
    samples = sorted(_responsiveness_sample() for _ in range(max(1, runs)))
    return samples[len(samples) // 2]


def _responsiveness_update(
    state: dict[str, Any], latency_ms: float, ratio: float
) -> bool:
    """Hysteretic strain detector over the responsiveness signal."""
    baseline = state.get("resp_baseline")
    if not isinstance(baseline, (int, float)) or baseline <= 0:
        state["resp_baseline"] = latency_ms
        state["strain_hits"] = 0
        state["calm_hits"] = 0
        state["strained"] = False
        state["resp_ratio"] = 1.0
        return False
    current_ratio = latency_ms / baseline if baseline > 0 else 1.0
    state["resp_ratio"] = round(current_ratio, 3)
    if current_ratio >= ratio:
        state["strain_hits"] = int(state.get("strain_hits", 0)) + 1
        state["calm_hits"] = 0
    elif current_ratio <= RESP_RELEASE_RATIO:
        state["calm_hits"] = int(state.get("calm_hits", 0)) + 1
        state["strain_hits"] = 0
        if not state.get("strained"):
            state["resp_baseline"] = (
                (1.0 - RESP_BASELINE_ALPHA) * baseline
                + RESP_BASELINE_ALPHA * latency_ms
            )
    else:
        state["strain_hits"] = 0
        state["calm_hits"] = 0
    if not state.get("strained") and int(state.get("strain_hits", 0)) >= RESP_STRAIN_SAMPLES:
        state["strained"] = True
    elif state.get("strained") and int(state.get("calm_hits", 0)) >= RESP_CALM_SAMPLES:
        state["strained"] = False
    return bool(state.get("strained"))


def _foreground_pid() -> int | None:
    """PID owning the foreground window (never demoted by ProBalance)."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Windows actions: background mode, EcoQoS, Job Object CPU hard cap.
# ---------------------------------------------------------------------------
PROCESS_TERMINATE: Final[int] = 0x0001
PROCESS_SET_INFORMATION: Final[int] = 0x0200
PROCESS_MODE_BACKGROUND_BEGIN: Final[int] = 0x00100000
PROCESS_MODE_BACKGROUND_END: Final[int] = 0x00200000
PROCESS_POWER_THROTTLING_CURRENT_VERSION: Final[int] = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED: Final[int] = 0x1
PROCESS_POWER_THROTTLING_INFORMATION: Final[int] = 4
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE: Final[int] = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP: Final[int] = 0x4
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS: Final[int] = 15


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("ControlMask", ctypes.c_ulong),
        ("StateMask", ctypes.c_ulong),
    ]


class _CpuRateControl(ctypes.Structure):
    _fields_ = [("ControlFlags", ctypes.c_uint32), ("CpuRate", ctypes.c_uint32)]


_ACTIVE_LIMITS: dict[tuple[int, float], int] = {}


def _open_process_handle(pid: int, access: int) -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(access, False, pid)
    return handle or None


def _close_process_handle(handle: int) -> None:
    try:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
    except OSError:
        pass


def _set_background_mode(pid: int, enable: bool) -> bool:
    """PROCESS_MODE_BACKGROUND_{BEGIN,END}: idle CPU + background I/O and
    memory priority; reversible through PROCESS_MODE_BACKGROUND_END."""
    handle = _open_process_handle(pid, PROCESS_SET_INFORMATION)
    if not handle:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        mode = PROCESS_MODE_BACKGROUND_BEGIN if enable else PROCESS_MODE_BACKGROUND_END
        return bool(kernel32.SetPriorityClass(handle, mode))
    except OSError:
        return False
    finally:
        _close_process_handle(handle)


def _set_ecoqos(pid: int, enable: bool) -> bool:
    """PowerThrottling execution-speed toggle (Windows efficiency mode)."""
    handle = _open_process_handle(pid, PROCESS_SET_INFORMATION)
    if not handle:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        state = _PowerThrottlingState(
            PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED if enable else 0,
        )
        return bool(
            kernel32.SetProcessInformation(
                handle,
                PROCESS_POWER_THROTTLING_INFORMATION,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
        )
    except OSError:
        return False
    finally:
        _close_process_handle(handle)


def _cpu_rate_value(percent: float) -> int:
    return max(1, min(10000, int(round(percent * 100))))


def _set_cpu_limit(key: tuple[int, float], pid: int, percent: float) -> bool:
    """Hard-cap a process's CPU share with a Job Object rate control.

    ``percent`` is a share of total machine CPU (100 = one full core);
    enforcement is kernel-side, no thread suspension.  A process already
    inside a restrictive job may refuse nesting — fail-soft.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    info = _CpuRateControl(
        JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
        _cpu_rate_value(percent),
    )
    job = _ACTIVE_LIMITS.get(key)
    if job:
        try:
            return bool(
                kernel32.SetInformationJobObject(
                    job,
                    JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
            )
        except OSError:
            return False
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    process_handle = _open_process_handle(
        pid,
        PROCESS_SET_QUOTA
        | PROCESS_SET_INFORMATION
        | PROCESS_TERMINATE
        | PROCESS_QUERY_LIMITED_INFORMATION,
    )
    if not process_handle:
        _close_process_handle(job)
        return False
    try:
        ok = bool(
            kernel32.SetInformationJobObject(
                job,
                JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        )
        ok = ok and bool(kernel32.AssignProcessToJobObject(job, process_handle))
    except OSError:
        ok = False
    finally:
        _close_process_handle(process_handle)
    if not ok:
        _close_process_handle(job)
        return False
    _ACTIVE_LIMITS[key] = job
    return True


def _clear_cpu_limit(key: tuple[int, float]) -> bool:
    job = _ACTIVE_LIMITS.pop(key, None)
    if not job:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        info = _CpuRateControl(0, 0)
        kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except OSError:
        pass
    _close_process_handle(job)
    return True



class GovernorConfig:
    def __init__(self, args: argparse.Namespace) -> None:
        self.interval: float = float(args.interval)
        self.cpu_busy: float = float(args.cpu_busy)
        self.cpu_extreme: float = float(args.cpu_extreme)
        self.calm: float = CPU_CALM_PCT
        self.sustain: int = int(args.sustain)
        self.extreme_sustain: int = max(int(args.sustain) * 2, EXTREME_SAMPLES)
        self.calm_samples: int = CALM_SAMPLES
        self.mem_trim_mb: float = float(args.mem_trim_mb)
        self.trim_cooldown: float = TRIM_COOLDOWN_SECONDS
        self.affinity: bool = not args.no_affinity
        self.dry_run: bool = bool(args.dry_run)
        self.log_samples: bool = bool(getattr(args, "log_samples", False))
        # §10.64 worker aggregate budget (single-core-equivalent).
        self.worker_cpu_budget: float = WORKER_CPU_BUDGET_PCT
        self.worker_ram_budget: float = WORKER_RAM_BUDGET_PCT
        # Process Lasso-inspired tier (all control features default OFF:
        # None = defer to the rules-file defaults, which are false).
        rules_arg = getattr(args, "rules", None)
        self.rules_path: Path = Path(rules_arg) if rules_arg else RULES_FILE
        self.probalance_flag: bool | None = getattr(args, "probalance", None)
        self.cpu_limiter_flag: bool | None = getattr(args, "cpu_limiter", None)
        self.background_mode_flag: bool | None = getattr(args, "background_mode", None)
        self.ecoqos_flag: bool | None = getattr(args, "ecoqos", None)
        self.limiter_percent_arg: float | None = getattr(args, "limiter_percent", None)
        self.resp_ratio_arg: float | None = getattr(args, "resp_ratio", None)


class ProcessRecord:
    __slots__ = (
        "busy", "calm", "prio_set", "aff_set", "reg_aff_set", "last_trim",
        "pb_set", "bg_set", "eco_set", "limit_set",
        "rule_applied", "rule_hold", "rule_priority", "rule_aff_set",
    )

    def __init__(self) -> None:
        self.busy = 0
        self.calm = 0
        self.prio_set = False
        self.aff_set = False
        self.reg_aff_set = False
        self.last_trim = 0.0
        self.pb_set = False
        self.bg_set = False
        self.eco_set = False
        self.limit_set = False
        self.rule_applied = False
        self.rule_hold: set[str] = set()
        self.rule_priority: int | None = None
        self.rule_aff_set = False


def _protected(proc: psutil.Process, name: str, exe: str | None) -> bool:
    if name.lower() in PROTECTED_NAMES:
        return True
    if exe:
        lowered = exe.lower()
        system_root = os.environ.get("SystemRoot", r"C:\Windows").lower()
        if lowered.startswith(system_root):
            return True
    return False


def _self_tree() -> set[int]:
    tree = {os.getpid()}
    try:
        for parent in psutil.Process(os.getpid()).parents():
            tree.add(parent.pid)
    except psutil.Error:
        pass
    return tree


def _is_governor_process(proc: psutil.Process) -> bool:
    try:
        cmdline = proc.cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False
    return any("resource-governor" in part for part in cmdline)


def _classify_plane(proc: psutil.Process, name: str, exe: str | None) -> str:
    """§10.64 worker-plane attribution for the aggregate budget ledger.

    Planes: ``governance`` (backend + governance processes — never
    regulated), ``toolbox`` (Standalone tools), ``worker`` (repo scripts,
    worktrees, agents, tests), ``repo-other`` (repo-attributed but
    unclassified), ``external`` (not repo-attributed — user's own work).
    """
    lowered_exe = (exe or "").lower()
    root = str(PROJECT_ROOT).lower()
    try:
        cmdline = " ".join(proc.cmdline()).lower()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        cmdline = ""
    if root not in lowered_exe and root not in cmdline and "gptbridge" not in cmdline:
        return "external"
    joined = f"{lowered_exe} {cmdline}"
    if "--serve" in cmdline or "boot_core" in joined or "governance_rule" in joined:
        return "governance"
    if "standalone tools" in joined or "standalone_tools" in joined:
        return "toolbox"
    if "scripts" in joined or ".worktrees" in joined or ".kilo" in joined or "pytest" in joined:
        return "worker"
    return "repo-other"


def govern_once(
    config: GovernorConfig,
    records: dict[tuple[int, float], ProcessRecord],
    machine: psutil.Process | None = None,
    regulation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = time.monotonic()
    me = psutil.Process() if machine is None else machine
    username = me.username()
    self_tree = _self_tree()
    logical = os.cpu_count() or 1
    if regulation is None:
        regulation = {"over": 0, "under": 0, "active": False, "pre": False}
    regulation.setdefault("pre", False)
    # §10.64 acceptance ⑤: kill switch — observe and log, never act.
    disabled = os.environ.get(GOVERNOR_DISABLE_ENV, "").strip().lower() in {
        "1", "true", "yes",
    }
    dry_run = config.dry_run or disabled
    defaults, programs, rules_error = load_rules(config.rules_path)
    features = _resolve_features(config, defaults)
    if rules_error and regulation.get("rules_error") != rules_error:
        _log_action({
            "action": "rules-invalid",
            "path": str(config.rules_path),
            "error": rules_error,
        })
    regulation["rules_error"] = rules_error
    latency_ms = measure_responsiveness()
    strained = _responsiveness_update(regulation, latency_ms, features["resp_ratio"])
    foreground_pid = _foreground_pid()
    worker_cpu_pct = 0.0
    worker_rss_mb = 0.0
    # Windows user processes do not expose a universal hard global CPU quota
    # without Job Objects.  Bound sustained offenders to approximately the
    # requested global share instead; priority throttling remains the first
    # action and protected/system processes are excluded.
    cap_count = max(
        AFFINITY_MIN_CPUS,
        int((logical * GLOBAL_CPU_LIMIT_PCT + 99.0) // 100.0),
    )
    cap_affinity = list(range(min(cap_count, logical)))
    # §10.64 control-law ②: while regulation is active the whole worker
    # plane shares one bounded affinity subset sized by the CPU budget —
    # workers timeshare a small core set instead of spreading across the
    # machine; affinity is released when regulation clears.
    worker_cap = max(
        AFFINITY_MIN_CPUS,
        int(logical * config.worker_cpu_budget // 100),
    )
    worker_affinity = list(range(min(worker_cap, logical)))

    actions: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, float]] = set()
    pb_candidates: list[tuple[float, Any, int, str, str, ProcessRecord]] = []

    for proc in psutil.process_iter(PROCESS_ATTRS):
        try:
            # W3：oneshot 讓同行程的 info/create_time/cpu_percent 共用一次快照
            # （原先每個屬性各自一次 syscall）。
            with proc.oneshot():
                info = proc.info
                pid = int(info["pid"])
                name = str(info["name"] or "")
                is_python = name.lower() in {"pythonw.exe", "python.exe"}
                if pid <= 4 or not name or pid in self_tree or (is_python and _is_governor_process(proc)):
                    continue
                if _protected(proc, name, info.get("exe")):
                    continue
                try:
                    if info.get("username") and username and info["username"] != username:
                        continue
                except (KeyError, TypeError):
                    continue

                create_time = proc.create_time()
                key = (pid, round(create_time, 3))
                seen.add(key)
                record = records.setdefault(key, ProcessRecord())

                cpu = proc.cpu_percent(None)
                rss_mb = float(info["memory_info"].rss) / (1024 * 1024) if info.get("memory_info") else 0.0
                plane = _classify_plane(proc, name, info.get("exe"))
                io_counters = info.get("io_counters")
                io_read_mb = (
                    float(io_counters.read_bytes) / (1024 * 1024)
                    if io_counters is not None else 0.0
                )
                io_write_mb = (
                    float(io_counters.write_bytes) / (1024 * 1024)
                    if io_counters is not None else 0.0
                )
                flags: list[str] = []
                if record.pb_set:
                    flags.append("probalance")
                if record.bg_set:
                    flags.append("background")
                if record.eco_set:
                    flags.append("ecoqos")
                if record.limit_set:
                    flags.append("limit")
                if record.rule_applied:
                    flags.append("rule")
                rows.append({"pid": pid, "name": name, "cpu": round(cpu, 1),
                             "mem_mb": round(rss_mb, 1), "plane": plane,
                             "io_read_mb": round(io_read_mb, 1),
                             "io_write_mb": round(io_write_mb, 1),
                             "flags": flags})
                if plane in WORKER_PLANES:
                    worker_cpu_pct += cpu
                    worker_rss_mb += rss_mb

            rule = None
            if programs:
                exe_name = ""
                if info.get("exe"):
                    exe_name = str(info["exe"]).rsplit("\\", 1)[-1].lower()
                rule = programs.get(exe_name) or programs.get(name.lower())
            if rule is not None and rule.exclude:
                continue

            # §10.64: governance / core-execution plane is never regulated.
            if plane == "governance":
                continue
            if features["probalance"] and pid != foreground_pid:
                pb_candidates.append((cpu, proc, pid, name, plane, record))

            # Process Lasso-style persistent rules: applied once per process,
            # then held (never auto-reverted) until the process exits.
            if rule is not None and not record.rule_applied:
                record.rule_applied = True
                if rule.background and features["background_mode"]:
                    ok = True if dry_run else _set_background_mode(pid, True)
                    record.bg_set = True
                    record.rule_hold.add("bg")
                    actions.append({"action": "rule-background-mode", "pid": pid,
                                    "name": name, "ok": ok})
                if rule.ecoqos and features["ecoqos"]:
                    ok = True if dry_run else _set_ecoqos(pid, True)
                    record.eco_set = True
                    record.rule_hold.add("eco")
                    actions.append({"action": "rule-ecoqos", "pid": pid,
                                    "name": name, "ok": ok})
                if rule.cpu_limit_percent > 0 and features["cpu_limiter"]:
                    ok = True if dry_run else _set_cpu_limit(
                        key, pid, rule.cpu_limit_percent
                    )
                    record.limit_set = True
                    record.rule_hold.add("limit")
                    actions.append({"action": "rule-cpu-limited", "pid": pid,
                                    "name": name,
                                    "limiter_percent": rule.cpu_limit_percent,
                                    "ok": ok})
                if rule.priority is not None and not (
                    rule.background and features["background_mode"]
                ):
                    if not dry_run:
                        try:
                            proc.nice(rule.priority)
                        except psutil.Error:
                            pass
                    record.rule_priority = rule.priority
                    actions.append({"action": "rule-priority", "pid": pid,
                                    "name": name, "priority": rule.priority})
                if rule.affinity and config.affinity:
                    if not dry_run:
                        try:
                            proc.cpu_affinity(rule.affinity)
                        except (AttributeError, psutil.Error):
                            pass
                    record.rule_aff_set = True
                    record.rule_hold.add("affinity")
                    actions.append({"action": "rule-affinity", "pid": pid,
                                    "name": name, "cpus": len(rule.affinity)})

            throttling_workers = (
                (regulation["active"] or regulation["pre"])
                and plane in WORKER_PLANES
            )
            busy_floor = (
                REGULATED_WORKER_BUSY_PCT if throttling_workers else config.cpu_busy
            )
            sustain_need = 1 if throttling_workers else config.sustain
            busy_now = cpu >= busy_floor
            extreme_now = cpu >= config.cpu_extreme
            calm_now = cpu < config.calm

            # ② aggregate containment while regulating (independent of the
            # per-process extreme-hog path below).
            if plane in WORKER_PLANES and config.affinity:
                if regulation["active"] and not record.reg_aff_set:
                    try:
                        if proc.cpu_affinity() != worker_affinity:
                            if not dry_run:
                                proc.cpu_affinity(worker_affinity)
                        record.reg_aff_set = True
                        actions.append(
                            {"action": "worker-affinity-capped", "pid": pid,
                             "name": name, "cpus": len(worker_affinity)}
                        )
                    except (AttributeError, psutil.Error):
                        pass
                elif not regulation["active"] and record.reg_aff_set:
                    try:
                        if not dry_run:
                            proc.cpu_affinity(list(range(logical)))
                        record.reg_aff_set = False
                        actions.append(
                            {"action": "worker-affinity-restored", "pid": pid,
                             "name": name}
                        )
                    except (AttributeError, psutil.Error):
                        pass

            record.busy = record.busy + 1 if busy_now else 0
            record.calm = record.calm + 1 if calm_now else 0

            if busy_now and record.busy >= sustain_need and not record.prio_set:
                if not dry_run:
                    proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                record.prio_set = True
                actions.append(
                    {"action": "priority-below-normal", "pid": pid, "name": name,
                     "cpu": round(cpu, 1), "mem_mb": round(rss_mb, 1)}
                )
            if (
                extreme_now
                and config.affinity
                and record.busy >= config.extreme_sustain
                and not record.aff_set
            ):
                try:
                    if proc.cpu_affinity() != cap_affinity:
                        if not dry_run:
                            proc.cpu_affinity(cap_affinity)
                        record.aff_set = True
                        actions.append(
                            {"action": "affinity-capped", "pid": pid, "name": name,
                             "cpu": round(cpu, 1), "cpus": len(cap_affinity)}
                        )
                except (AttributeError, psutil.Error):
                    pass
            # Process Lasso-inspired dynamic tiers (all default OFF; worker
            # planes only for the intrusive ones, unlike the priority path).
            if (
                extreme_now
                and plane in WORKER_PLANES
                and record.busy >= config.extreme_sustain
            ):
                if features["background_mode"] and not record.bg_set:
                    ok = True if dry_run else _set_background_mode(pid, True)
                    record.bg_set = True
                    actions.append({"action": "background-mode", "pid": pid,
                                    "name": name, "cpu": round(cpu, 1), "ok": ok})
                if features["ecoqos"] and not record.eco_set:
                    ok = True if dry_run else _set_ecoqos(pid, True)
                    record.eco_set = True
                    actions.append({"action": "ecoqos", "pid": pid, "name": name,
                                    "cpu": round(cpu, 1), "ok": ok})
                if features["cpu_limiter"] and not record.limit_set:
                    ok = True if dry_run else _set_cpu_limit(
                        key, pid, features["limiter_percent"]
                    )
                    record.limit_set = True
                    actions.append({"action": "cpu-limited", "pid": pid,
                                    "name": name, "cpu": round(cpu, 1),
                                    "limiter_percent": features["limiter_percent"],
                                    "ok": ok})
                and calm_now
                and now - record.last_trim >= config.trim_cooldown
            ):
                trimmed = True
                if not dry_run:
                    trimmed = _trim_working_set(pid)
                record.last_trim = now
                actions.append(
                    {"action": "working-set-trimmed", "pid": pid, "name": name,
                     "cpu": round(cpu, 1), "mem_mb": round(rss_mb, 1), "ok": trimmed}
                )
            if (
                calm_now
                and record.calm >= config.calm_samples
                and (record.prio_set or record.aff_set)
            ):
                if not dry_run:
                    try:
                        proc.nice(psutil.NORMAL_PRIORITY_CLASS)
                    except psutil.Error:
                        pass
                    if record.aff_set:
                        try:
                            proc.cpu_affinity(list(range(logical)))
                        except (AttributeError, psutil.Error):
                            pass
                record.prio_set = False
                record.aff_set = False
                actions.append(
                    {"action": "restored", "pid": pid, "name": name, "cpu": round(cpu, 1)}
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue

    for key in list(records):
        if key not in seen:
            records.pop(key, None)

    # §10.64 aggregate worker budget + hysteresis control law (strict
    # INT-10 semantics): first over-budget sample -> regulate; 5 consecutive
    # samples under 80% of budget -> release (no flapping).
    total_mem = psutil.virtual_memory()
    total_ram_mb = total_mem.total / (1024 * 1024)
    worker_ram_pct = (worker_rss_mb / total_ram_mb * 100.0) if total_ram_mb else 0.0
    over_budget = (
        worker_cpu_pct > config.worker_cpu_budget
        or worker_ram_pct > config.worker_ram_budget
    )
    under_budget = (
        worker_cpu_pct <= config.worker_cpu_budget * REGULATE_UNDER_FACTOR
        and worker_ram_pct <= config.worker_ram_budget * REGULATE_UNDER_FACTOR
    )
    if over_budget:
        regulation["over"] += 1
        regulation["under"] = 0
    elif under_budget:
        regulation["under"] += 1
        regulation["over"] = 0
    else:
        regulation["over"] = 0
        regulation["under"] = 0
    # Pre-throttle (2026-09-22 governor ruling, strict INT-10 semantics): the
    # 80%-of-budget band engages a soft worker throttle on a single sample so
    # the budget is rarely crossed; release still requires the same 5-sample
    # under-80% streak as full regulation (anti-flap).
    if not regulation["pre"] and not under_budget:
        regulation["pre"] = True
        _log_action({
            "action": "prethrottle-entered",
            "worker_cpu_pct": round(worker_cpu_pct, 1),
            "worker_ram_pct": round(worker_ram_pct, 2),
        })
    if not regulation["active"] and regulation["over"] >= REGULATE_OVER_SAMPLES:
        regulation["active"] = True
        _log_action({
            "action": "regulation-entered",
            "worker_cpu_pct": round(worker_cpu_pct, 1),
            "worker_ram_pct": round(worker_ram_pct, 2),
        })
    elif regulation["active"] and regulation["under"] >= REGULATE_UNDER_SAMPLES:
        regulation["active"] = False
        _log_action({
            "action": "regulation-released",
            "worker_cpu_pct": round(worker_cpu_pct, 1),
            "worker_ram_pct": round(worker_ram_pct, 2),
        })
    if regulation["pre"] and regulation["under"] >= REGULATE_UNDER_SAMPLES:
        regulation["pre"] = False
        _log_action({
            "action": "prethrottle-released",
            "worker_cpu_pct": round(worker_cpu_pct, 1),
            "worker_ram_pct": round(worker_ram_pct, 2),
        })

    for entry in actions:
        _log_action(entry)

    top_cpu = sorted(rows, key=lambda item: item["cpu"], reverse=True)[:5]
    top_mem = sorted(rows, key=lambda item: item["mem_mb"], reverse=True)[:5]
    snapshot = {
        "interval": config.interval,
        "processes": len(rows),
        "tracked": len(records),
        "cpu_load_pct": psutil.cpu_percent(None),
        "mem_used_pct": total_mem.percent,
        "mem_available_mb": round(total_mem.available / (1024 * 1024), 1),
        "resource_limits": {
            "cpu_pct": GLOBAL_CPU_LIMIT_PCT,
            "ram_pct": GLOBAL_RAM_LIMIT_PCT,
            "cpu_over_limit": psutil.cpu_percent(None) > GLOBAL_CPU_LIMIT_PCT,
            "ram_over_limit": total_mem.percent > GLOBAL_RAM_LIMIT_PCT,
        },
        # §10.64 worker ledger — the backend reads worker_admission_hold
        # before starting new on-demand workers (control-law step ⑤).
        "worker_ledger": {
            "cpu_pct": round(worker_cpu_pct, 1),
            "ram_mb": round(worker_rss_mb, 1),
            "ram_pct": round(worker_ram_pct, 2),
            "budget_cpu_pct": config.worker_cpu_budget,
            "budget_ram_pct": config.worker_ram_budget,
            "over_budget": over_budget,
            "planes": dict(
                sorted(
                    (
                        plane,
                        sum(1 for r in rows if r["plane"] == plane),
                    )
                    for plane in {r["plane"] for r in rows}
                )
            ),
        },
        "regulation": {
            "active": regulation["active"],
            "pre": regulation["pre"],
            "over_samples": regulation["over"],
            "under_samples": regulation["under"],
        },
        "worker_admission_hold": regulation["active"] or regulation["pre"],
        "actions": actions,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "dry_run": config.dry_run,
        "disabled": disabled,
    }
    _write_state(snapshot)
    # §10.64 acceptance ①: per-cycle ledger samples are what a 30 min
    # p95 audit is computed from — opt-in via --log-samples.
    if config.log_samples:
        _log_action({
            "action": "sample",
            "worker_ledger": snapshot["worker_ledger"],
            "regulation": snapshot["regulation"],
        })
    return snapshot


def _print_snapshot(snapshot: dict[str, Any]) -> None:
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


def _lock_is_busy() -> bool:
    sys.path.insert(0, str(PROJECT_ROOT))
    from governance_rule.execution.git_tiers.process_lock import lock_is_active

    return lock_is_active(LOCK_FILE)


def _acquire_lock():
    sys.path.insert(0, str(PROJECT_ROOT))
    from governance_rule.execution.git_tiers.process_lock import ProcessFileLock

    return ProcessFileLock(LOCK_FILE)


def run_watch(config: GovernorConfig) -> int:
    lock = _acquire_lock()
    try:
        with lock:
            print(f"resource-governor watching (interval={config.interval}s); Ctrl+C to stop")
            records: dict[tuple[int, float], ProcessRecord] = {}
            regulation: dict[str, Any] = {"over": 0, "under": 0, "active": False, "pre": False}
            psutil.Process().nice(psutil.IDLE_PRIORITY_CLASS)
            running = True

            def _stop(*_: object) -> None:
                nonlocal running
                running = False

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    signal.signal(sig, _stop)
                except (ValueError, OSError):
                    pass
            psutil.cpu_percent(None)
            while running:
                try:
                    govern_once(config, records, regulation=regulation)
                except Exception as error:  # noqa: BLE001 - keep the loop alive
                    _log_action({"action": "cycle-error", "error": f"{type(error).__name__}: {error}"})
                deadline = time.monotonic() + config.interval
                while running and time.monotonic() < deadline:
                    time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
            _write_state({"stopped": True, "reason": "signal"})
            return 0
    except Exception as error:  # LockBusyError
        print(f"governor already running ({error})")
        return 0


def run_start(config: GovernorConfig) -> int:
    if _lock_is_busy():
        print("resource-governor already running")
        return 0
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = str(pythonw) if pythonw.is_file() else sys.executable
    command = [
        target,
        str(Path(__file__).resolve()),
        "--watch",
        "--interval",
        str(config.interval),
    ]
    if config.log_samples:
        command.append("--log-samples")
    creationflags = 0x00000008 | 0x00000200
    subprocess.Popen(  # noqa: S603
        command,
        cwd=str(PROJECT_ROOT),
        creationflags=creationflags,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(2.0)
    return run_status()


def run_stop() -> int:
    if not LOCK_FILE.is_file():
        print("no lock file; nothing to stop")
        return 0
    try:
        payload = json.loads(LOCK_FILE.read_text(encoding="ascii"))
        pid = int(payload["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        print("lock file unreadable; remove it manually if no governor is running")
        return 1
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except psutil.TimeoutExpired:
            proc.kill()
        print(f"resource-governor stopped (pid {pid})")
        return 0
    except psutil.NoSuchProcess:
        LOCK_FILE.unlink(missing_ok=True)
        print("stale lock removed (process already gone)")
        return 0


def run_status() -> int:
    payload: dict[str, Any] = {
        "running": _lock_is_busy(),
        "lock_file": str(LOCK_FILE),
    }
    if STATE_FILE.is_file():
        try:
            payload["last_cycle"] = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload["last_cycle"] = None
    if LOG_FILE.is_file():
        try:
            lines = LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
            payload["recent_actions"] = [json.loads(line) for line in lines[-10:]]
        except (OSError, json.JSONDecodeError):
            payload["recent_actions"] = []
    _print_snapshot(payload)
    return 0


def _installed_pythonw() -> str:
    target = PROJECT_ROOT / "main-system" / ".venv" / "Scripts" / "pythonw.exe"
    return str(target) if target.is_file() else str(sys.executable)


def _launch_arguments(config: GovernorConfig) -> str:
    return f'"{PROJECT_ROOT / "scripts" / "resource-governor.py"}" --watch --interval {config.interval}'


def _task_xml(arguments: str, target: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="InteractiveUser">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="InteractiveUser">
    <Exec>
      <Command>{target}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{PROJECT_ROOT}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def install_task(config: GovernorConfig) -> int:
    import tempfile

    target = _installed_pythonw()
    arguments = _launch_arguments(config)
    xml = _task_xml(arguments, target)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", delete=False, encoding="utf-16"
    ) as handle:
        handle.write(xml)
        task_xml = handle.name
    try:
        result = subprocess.run(  # noqa: S603
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", task_xml, "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        try:
            os.unlink(task_xml)
        except OSError:
            pass
    if result.returncode != 0:
        print(f"task registration failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"task registered: {TASK_NAME}")
    return 0


def uninstall_task() -> int:
    result = subprocess.run(  # noqa: S603
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 and "does not exist" not in result.stderr:
        print(f"task removal failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"task removed: {TASK_NAME}")
    return 0


def install_logon(config: GovernorConfig) -> int:
    import winreg

    value = f'"{_installed_pythonw()}" {_launch_arguments(config)}'
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, LOGO_HIVE, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, LOGO_NAME, 0, winreg.REG_SZ, value)
    except OSError as exc:
        print(f"logon registration failed: {exc}", file=sys.stderr)
        return 1
    print(f"logon registration active: {LOGO_NAME}")
    return 0


def uninstall_logon() -> int:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, LOGO_HIVE, 0, winreg.KEY_SET_VALUE
        ) as key:
            try:
                winreg.DeleteValue(key, LOGO_NAME)
            except FileNotFoundError:
                pass
    except OSError as exc:
        print(f"logon removal failed: {exc}", file=sys.stderr)
        return 1
    print(f"logon registration removed: {LOGO_NAME}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Adaptive CPU/memory governor with automatic throttling."
    )
    parser.add_argument("--once", action="store_true", help="run a single cycle")
    parser.add_argument("--watch", action="store_true", help="run continuously")
    parser.add_argument("--dry-run", action="store_true", help="log without applying")
    parser.add_argument("--start", action="store_true", help="start detached background service")
    parser.add_argument("--stop", action="store_true", help="stop the background service")
    parser.add_argument("--status", action="store_true", help="report service status")
    parser.add_argument("--install-task", action="store_true", help="register a logon Task Scheduler job")
    parser.add_argument("--uninstall-task", action="store_true", help="remove the Task Scheduler job")
    parser.add_argument("--install-logon", action="store_true", help="register a per-user logon Run key")
    parser.add_argument("--uninstall-logon", action="store_true", help="remove the per-user logon Run key")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="cycle seconds (default 20)")
    parser.add_argument("--cpu-busy", type=float, default=CPU_BUSY_PCT, help="busy CPU %% of one core (default 10)")
    parser.add_argument("--cpu-extreme", type=float, default=CPU_EXTREME_PCT, help="extreme CPU %% of one core (default 150)")
    parser.add_argument("--mem-trim-mb", type=float, default=MEM_TRIM_MB, help="working-set trim threshold MB (default 1500)")
    parser.add_argument("--sustain", type=int, default=SUSTAIN_SAMPLES, help="busy samples before priority drop (default 3)")
    parser.add_argument("--no-affinity", action="store_true", help="never cap CPU affinity")
    parser.add_argument("--log-samples", action="store_true", help="append per-cycle worker-ledger samples to the action log (30 min p95 audits)")
    return parser


def cli_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = GovernorConfig(args)
    if args.uninstall_task:
        return uninstall_task()
    if args.uninstall_logon:
        return uninstall_logon()
    if args.install_task:
        return install_task(config)
    if args.install_logon:
        return install_logon(config)
    if args.stop:
        return run_stop()
    if args.start:
        return run_start(config)
    if args.watch:
        return run_watch(config)
    if args.once:
        records: dict[tuple[int, float], ProcessRecord] = {}
        psutil.cpu_percent(None)
        for proc in psutil.process_iter(PROCESS_ATTRS):
            try:
                proc.cpu_percent(None)
            except psutil.Error:
                continue
        time.sleep(max(1.0, min(config.interval, 5.0)))
        snapshot = govern_once(config, records)
        _print_snapshot(snapshot)
        return 0
    return run_status()


if __name__ == "__main__":
    raise SystemExit(cli_main())
