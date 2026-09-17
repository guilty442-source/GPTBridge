"""Git governance compatibility matrix (A318-A320).

Judges an observed runtime (supervisor / worker / registry / queue / audit /
hook versions plus the Git executable) against the versions declared in the
verified ``git_governance_manifest.json``:

    WRITE_COMPATIBLE     runtime and schema agree — reads and writes are safe
    READ_COMPATIBLE      reads are safe, writes are restricted (Git below the
                         minimum, unobserved component)
    MIGRATION_REQUIRED   runtime is newer than the schema it met — it can
                         read the old shape, but a migration must run before
                         it may write
    INCOMPATIBLE         runtime is older than the schema it met — fail
                         closed READ_ONLY (reason ``INCOMPATIBLE_SCHEMA``)

Git rules: below ``minimum_git_version`` blocks writes; above
``maximum_tested_git_version`` is reported WARN/DEGRADED but stays usable.

``version_skew()`` aggregates a version map into a single ``VERSION_SKEW``
signal (NONE / PATCH / MINOR / MAJOR / UNKNOWN), and every compatibility
report carries that aggregate so drift across the plane is one number.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

from .governance_manifest import governance_status, parse_version

VERSION_SKEW = "VERSION_SKEW"
SCHEMA_MATCH = "SCHEMA_MATCH"
RUNTIME_NEWER = "RUNTIME_NEWER"
RUNTIME_OLDER = "RUNTIME_OLDER"
INCOMPATIBLE_SCHEMA = "INCOMPATIBLE_SCHEMA"
MIGRATION_PENDING = "MIGRATION_PENDING"
GIT_BELOW_MINIMUM = "GIT_BELOW_MINIMUM"
GIT_ABOVE_TESTED = "GIT_ABOVE_TESTED"
GIT_UNKNOWN = "GIT_UNKNOWN"
GIT_OK = "GIT_OK"
VERSION_UNOBSERVED = "VERSION_UNOBSERVED"
MANIFEST_NOT_ACTIVE = "MANIFEST_NOT_ACTIVE"

#: component name -> manifest field holding the schema it must agree with
COMPONENT_SCHEMA_FIELDS: Mapping[str, str] = {
    "supervisor": "control_plane_version",
    "worker": "worker_schema_version",
    "registry": "registry_schema_version",
    "queue": "queue_schema_version",
    "audit": "audit_schema_version",
    "hook": "hook_version",
}

#: Builtin expectation used only when the manifest cannot be verified; the
#: shipped manifest is the single version source, this is the last resort.
FALLBACK_EXPECTED: Mapping[str, str] = {
    "control_plane_version": "1.0.0",
    "worker_schema_version": "1.0.0",
    "registry_schema_version": "2.0.0",
    "queue_schema_version": "1.0.0",
    "audit_schema_version": "1.0.0",
    "hook_version": "1.0.0",
    "minimum_git_version": "2.38.0",
    "maximum_tested_git_version": "2.55.0",
}

_GIT_VERSION_CMD = ("git", "--version")


class Verdict(str, Enum):
    READ_COMPATIBLE = "READ_COMPATIBLE"
    WRITE_COMPATIBLE = "WRITE_COMPATIBLE"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    INCOMPATIBLE = "INCOMPATIBLE"


#: higher = more severe; aggregation is worst-wins
_VERDICT_SEVERITY: Mapping[Verdict, int] = {
    Verdict.WRITE_COMPATIBLE: 0,
    Verdict.READ_COMPATIBLE: 1,
    Verdict.MIGRATION_REQUIRED: 2,
    Verdict.INCOMPATIBLE: 3,
}


def aggregate_verdicts(verdicts: Any) -> Verdict:
    """Worst-wins aggregation over an iterable of ``Verdict`` values."""
    result = Verdict.WRITE_COMPATIBLE
    for verdict in verdicts:
        if _VERDICT_SEVERITY[Verdict(verdict)] > _VERDICT_SEVERITY[result]:
            result = Verdict(verdict)
    return result


@dataclass(frozen=True)
class VersionSkew:
    level: str
    reason: str
    major_delta: int = 0
    minor_delta: int = 0
    patch_delta: int = 0
    minimum: Mapping[str, str] = field(default_factory=dict)
    maximum: Mapping[str, str] = field(default_factory=dict)
    components: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reason": self.reason,
            "major_delta": self.major_delta,
            "minor_delta": self.minor_delta,
            "patch_delta": self.patch_delta,
            "minimum": dict(self.minimum),
            "maximum": dict(self.maximum),
            "components": list(self.components),
        }


@dataclass(frozen=True)
class ComponentVerdict:
    component: str
    runtime_version: str
    schema_version: str
    verdict: Verdict
    reason: str
    read_allowed: bool
    write_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "runtime_version": self.runtime_version,
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "read_allowed": self.read_allowed,
            "write_allowed": self.write_allowed,
        }


@dataclass(frozen=True)
class CompatibilityReport:
    verdict: Verdict
    reason_codes: tuple[str, ...]
    read_allowed: bool
    write_allowed: bool
    degraded: bool
    git_state: str
    git_version: str
    minimum_git_version: str
    maximum_tested_git_version: str
    components: Mapping[str, ComponentVerdict]
    skew: VersionSkew

    @property
    def read_only(self) -> bool:
        return not self.write_allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason_codes": list(self.reason_codes),
            "read_allowed": self.read_allowed,
            "write_allowed": self.write_allowed,
            "degraded": self.degraded,
            "git_state": self.git_state,
            "git_version": self.git_version,
            "minimum_git_version": self.minimum_git_version,
            "maximum_tested_git_version": self.maximum_tested_git_version,
            "components": {
                name: item.to_dict() for name, item in self.components.items()
            },
            "skew": self.skew.to_dict(),
        }


def version_skew(versions: Mapping[str, str]) -> VersionSkew:
    """Aggregate a component -> version map into one VERSION_SKEW signal."""
    parsed: dict[str, tuple[int, int, int]] = {}
    for name, value in versions.items():
        text = str(value or "").strip()
        if text:
            parsed[str(name)] = parse_version(text)
    if not parsed:
        return VersionSkew(level="UNKNOWN", reason=VERSION_SKEW)
    low = min(parsed.values())
    high = max(parsed.values())
    major_delta = high[0] - low[0]
    minor_delta = high[1] - low[1]
    patch_delta = high[2] - low[2]
    if major_delta:
        level = "MAJOR"
    elif minor_delta:
        level = "MINOR"
    elif patch_delta:
        level = "PATCH"
    else:
        level = "NONE"
    minimum = {name: versions[name] for name, value in parsed.items() if value == low}
    maximum = {name: versions[name] for name, value in parsed.items() if value == high}
    return VersionSkew(
        level=level,
        reason="OK" if level == "NONE" else VERSION_SKEW,
        major_delta=major_delta,
        minor_delta=minor_delta,
        patch_delta=patch_delta,
        minimum=minimum,
        maximum=maximum,
        components=tuple(sorted(parsed)),
    )


def compare_versions(
    runtime_version: str, schema_version: str
) -> tuple[Verdict, str, bool, bool]:
    """Compare one runtime version against the schema it met.

    Returns ``(verdict, reason, read_allowed, write_allowed)``.
    """
    runtime = parse_version(runtime_version)
    schema = parse_version(schema_version)
    if (runtime[0], runtime[1]) == (schema[0], schema[1]):
        return Verdict.WRITE_COMPATIBLE, SCHEMA_MATCH, True, True
    if runtime[0] > schema[0] or (
        runtime[0] == schema[0] and runtime[1] > schema[1]
    ):
        return Verdict.MIGRATION_REQUIRED, RUNTIME_NEWER, True, False
    return Verdict.INCOMPATIBLE, INCOMPATIBLE_SCHEMA, False, False


def parse_git_version(text: str) -> tuple[int, int, int]:
    return parse_version(text)


def detected_git_version(*, timeout: float = 10.0) -> str:
    """Best-effort ``git --version`` probe; empty string when unavailable."""
    try:
        result = subprocess.run(
            list(_GIT_VERSION_CMD),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _expected_versions(
    manifest: Optional[Mapping[str, Any]],
) -> tuple[dict[str, str], bool]:
    verified = True
    payload: Mapping[str, Any] = manifest or {}
    if manifest is None:
        status = governance_status()
        verified = status.ok
        payload = status.payload or {}
    expected: dict[str, str] = {}
    for field_name, fallback in FALLBACK_EXPECTED.items():
        value = payload.get(field_name) if payload else None
        if value in (None, ""):
            verified = verified and False
            value = fallback
        expected[field_name] = str(value)
    return expected, verified


def evaluate_compatibility(
    *,
    supervisor_version: str = "",
    worker_version: str = "",
    registry_schema_version: str = "",
    queue_schema_version: str = "",
    audit_schema_version: str = "",
    hook_version: str = "",
    git_executable_version: str = "",
    expected: Optional[Mapping[str, str]] = None,
    manifest: Optional[Mapping[str, Any]] = None,
) -> CompatibilityReport:
    """Judge observed runtime/schema versions against the manifest."""
    observed = {
        "supervisor": supervisor_version,
        "worker": worker_version,
        "registry": registry_schema_version,
        "queue": queue_schema_version,
        "audit": audit_schema_version,
        "hook": hook_version,
    }
    base_expected, manifest_verified = _expected_versions(manifest)
    if expected:
        base_expected.update({str(k): str(v) for k, v in expected.items()})

    reason_codes: list[str] = []
    if manifest is None and not manifest_verified:
        reason_codes.append(MANIFEST_NOT_ACTIVE)

    components: dict[str, ComponentVerdict] = {}
    for name, field_name in COMPONENT_SCHEMA_FIELDS.items():
        schema_version = base_expected.get(field_name, "")
        runtime_version = str(observed.get(name) or "").strip()
        if not runtime_version:
            verdict, reason, read_ok, write_ok = (
                Verdict.READ_COMPATIBLE, VERSION_UNOBSERVED, True, False,
            )
        else:
            verdict, reason, read_ok, write_ok = compare_versions(
                runtime_version, schema_version
            )
            if verdict is Verdict.MIGRATION_REQUIRED:
                reason = MIGRATION_PENDING
            elif verdict is Verdict.INCOMPATIBLE:
                reason = INCOMPATIBLE_SCHEMA
        components[name] = ComponentVerdict(
            component=name,
            runtime_version=runtime_version,
            schema_version=schema_version,
            verdict=verdict,
            reason=reason,
            read_allowed=read_ok,
            write_allowed=write_ok,
        )
        if reason not in (SCHEMA_MATCH,):
            reason_codes.append(f"{reason}:{name}")

    verdict = aggregate_verdicts(item.verdict for item in components.values())
    read_allowed = all(item.read_allowed for item in components.values())
    write_allowed = all(item.write_allowed for item in components.values())

    minimum_git = base_expected.get("minimum_git_version", "") or ""
    maximum_git = base_expected.get("maximum_tested_git_version", "") or ""
    if manifest is not None:
        minimum_git = str(manifest.get("minimum_git_version", minimum_git) or minimum_git)
        maximum_git = str(
            manifest.get("maximum_tested_git_version", maximum_git) or maximum_git
        )
    git_version = str(git_executable_version or "").strip()
    git_state = GIT_UNKNOWN
    degraded = False
    if git_version:
        actual = parse_git_version(git_version)
        if minimum_git and actual < parse_version(minimum_git):
            git_state = GIT_BELOW_MINIMUM
            write_allowed = False
            reason_codes.append(GIT_BELOW_MINIMUM)
            if verdict is Verdict.WRITE_COMPATIBLE:
                verdict = Verdict.READ_COMPATIBLE
        elif maximum_git and actual > parse_version(maximum_git):
            git_state = GIT_ABOVE_TESTED
            degraded = True
            reason_codes.append(GIT_ABOVE_TESTED)
        else:
            git_state = GIT_OK
    else:
        degraded = True
        reason_codes.append(GIT_UNKNOWN)

    skew_inputs = {
        name: item.runtime_version
        for name, item in components.items()
        if item.runtime_version
    }
    skew = version_skew(skew_inputs)
    if skew.reason == VERSION_SKEW and skew.level in ("MAJOR", "MINOR"):
        reason_codes.append(f"{VERSION_SKEW}:{skew.level}")

    return CompatibilityReport(
        verdict=verdict,
        reason_codes=tuple(reason_codes),
        read_allowed=read_allowed,
        write_allowed=write_allowed,
        degraded=degraded,
        git_state=git_state,
        git_version=git_version,
        minimum_git_version=minimum_git,
        maximum_tested_git_version=maximum_git,
        components=components,
        skew=skew,
    )


def compatibility_from_manifest(
    *,
    manifest_dir: str | None = None,
    git_executable_version: str = "",
    **observed: str,
) -> CompatibilityReport:
    """Convenience: evaluate against the on-disk verified manifest."""
    status = (
        governance_status(directory=manifest_dir)
        if manifest_dir
        else governance_status()
    )
    manifest = status.payload if status.ok else None
    return evaluate_compatibility(
        manifest=manifest,
        git_executable_version=git_executable_version,
        **observed,
    )


__all__ = [
    "COMPONENT_SCHEMA_FIELDS",
    "CompatibilityReport",
    "ComponentVerdict",
    "FALLBACK_EXPECTED",
    "GIT_ABOVE_TESTED",
    "GIT_BELOW_MINIMUM",
    "GIT_OK",
    "GIT_UNKNOWN",
    "INCOMPATIBLE_SCHEMA",
    "MIGRATION_PENDING",
    "MANIFEST_NOT_ACTIVE",
    "RUNTIME_NEWER",
    "RUNTIME_OLDER",
    "SCHEMA_MATCH",
    "VERSION_SKEW",
    "VERSION_UNOBSERVED",
    "Verdict",
    "VersionSkew",
    "aggregate_verdicts",
    "compatibility_from_manifest",
    "compare_versions",
    "detected_git_version",
    "evaluate_compatibility",
    "parse_git_version",
    "version_skew",
]
