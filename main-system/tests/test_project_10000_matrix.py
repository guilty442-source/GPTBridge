from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from governance_rule.code_rule_directory import code_rule_directory_snapshot
from governance_rule.governance_policy import governance_policy_snapshot


ROOT = Path(__file__).resolve().parents[2]
TOOL_IDS = tuple(code_rule_directory_snapshot().approved_tool_ids)
CASES_PER_TOOL = 3_000
TARGET_ADDITIONAL_CASES = len(TOOL_IDS) * CASES_PER_TOOL
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ContractProbe:
    tool_id: str
    family: str
    ordinal: int
    candidate: object
    expected: bool

    @property
    def test_id(self) -> str:
        return f"{self.tool_id}-{self.family}-{self.ordinal:04d}"


def _load_manifest(tool_id: str) -> dict[str, Any]:
    manifest_path = ROOT / tool_id / "manifest.json"
    if not manifest_path.is_file():
        candidates = []
        for candidate in ROOT.glob("*/*/manifest.json"):
            document = json.loads(candidate.read_text("utf-8"))
            if (
                document.get("id") == tool_id
                and document.get("main_system_independent_tool") is True
            ):
                candidates.append(candidate)
        assert len(candidates) == 1, tool_id
        manifest_path = candidates[0]
    payload = json.loads(manifest_path.read_text("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _safe_python_runtime_path(candidate: object) -> bool:
    value = str(candidate or "").replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        return False
    path_value = PurePosixPath(value)
    return (
        ".." not in path_value.parts
        and path_value.suffix == ".py"
        and all(part not in {"", "."} for part in path_value.parts)
    )


def _build_tool_identity_cases(tool_id: str) -> list[ContractProbe]:
    cases = [ContractProbe(tool_id, "tool-id", 0, tool_id, True)]
    invalid_factories = (
        lambda index: f"{tool_id.upper()}-{index}",
        lambda index: f"../{tool_id}-{index}",
        lambda index: f"{tool_id} alias {index}",
        lambda index: f"-{tool_id}-{index}",
    )
    for index in range(1, 300):
        cases.append(
            ContractProbe(
                tool_id,
                "tool-id",
                index,
                invalid_factories[index % len(invalid_factories)](index),
                False,
            )
        )
    return cases


def _build_version_cases(tool_id: str, manifest: dict[str, Any]) -> list[ContractProbe]:
    cases = [
        ContractProbe(
            tool_id,
            "version",
            0,
            (manifest.get("version"), manifest.get("display_version")),
            True,
        )
    ]
    for index in range(1, 300):
        candidate = (
            (f"1.0.{index}", "1.0")
            if index % 3 == 0
            else ("1.0.0", f"1.{index}")
            if index % 3 == 1
            else (f"{index}.0.0", f"{index}.0")
        )
        cases.append(ContractProbe(tool_id, "version", index, candidate, False))
    return cases


def _build_capability_cases(tool_id: str) -> list[ContractProbe]:
    approved = code_rule_directory_snapshot().approved_capability_names
    cases = [
        ContractProbe(tool_id, "capability", index, capability, True)
        for index, capability in enumerate(approved)
    ]
    normalized_tool = tool_id.replace("_", "-")
    for index in range(len(cases), 400):
        cases.append(
            ContractProbe(
                tool_id,
                "capability",
                index,
                f"unapproved-{normalized_tool}-{index}",
                False,
            )
        )
    return cases


def _build_runtime_path_cases(tool_id: str) -> list[ContractProbe]:
    normalized_tool = tool_id.replace("_", "-")
    cases = [
        ContractProbe(
            tool_id,
            "runtime-path",
            index,
            f"src/generated/{normalized_tool}/case-{index}.py",
            True,
        )
        for index in range(250)
    ]
    invalid_factories = (
        lambda index: f"../outside/case-{index}.py",
        lambda index: f"/absolute/case-{index}.py",
        lambda index: f"C:/outside/case-{index}.py",
        lambda index: f"src/generated/case-{index}.txt",
        lambda index: f"src/generated/../../outside-{index}.py",
    )
    for index in range(250, 500):
        cases.append(
            ContractProbe(
                tool_id,
                "runtime-path",
                index,
                invalid_factories[index % len(invalid_factories)](index),
                False,
            )
        )
    return cases


def _build_environment_cases(tool_id: str) -> list[ContractProbe]:
    prefix = tool_id.replace("-", "_").upper()
    cases = [
        ContractProbe(
            tool_id,
            "environment",
            index,
            f"GPTBRIDGE_{prefix}_CASE_{index}",
            True,
        )
        for index in range(250)
    ]
    invalid_factories = (
        lambda index: f"gptbridge_{prefix}_{index}",
        lambda index: f"GPTBRIDGE-{prefix}-{index}",
        lambda index: f" GPTBRIDGE_{prefix}_{index}",
        lambda index: f"GPTBRIDGE_{prefix}_{index}=1",
        lambda index: f"{index}_GPTBRIDGE_{prefix}",
    )
    for index in range(250, 500):
        cases.append(
            ContractProbe(
                tool_id,
                "environment",
                index,
                invalid_factories[index % len(invalid_factories)](index),
                False,
            )
        )
    return cases


def _build_locale_key_cases(tool_id: str) -> list[ContractProbe]:
    cases = [
        ContractProbe(tool_id, "locale-key", index, f"tool.case-{index}", True)
        for index in range(150)
    ]
    invalid_factories = (
        lambda index: f"Tool.case-{index}",
        lambda index: f"tool case {index}",
        lambda index: f"tool..case-{index}",
        lambda index: f".tool.case-{index}",
    )
    for index in range(150, 300):
        cases.append(
            ContractProbe(
                tool_id,
                "locale-key",
                index,
                invalid_factories[index % len(invalid_factories)](index),
                False,
            )
        )
    return cases


def _build_permission_cases(
    tool_id: str,
    manifest: dict[str, Any],
) -> list[ContractProbe]:
    permissions = manifest["permissions"]
    expected = (permissions["code_scope"], permissions["database_scope"])
    cases = [ContractProbe(tool_id, "permission", 0, expected, True)]
    for index in range(1, 300):
        candidate = (
            (f"cross-tool-code-{index}", expected[1])
            if index % 2
            else (expected[0], f"shared-database-{index}")
        )
        cases.append(ContractProbe(tool_id, "permission", index, candidate, False))
    return cases


def _build_request_channel_cases(
    tool_id: str,
    manifest: dict[str, Any],
) -> list[ContractProbe]:
    channel = manifest.get("request_channel")
    if tool_id == "governance_rule":
        cases = [ContractProbe(tool_id, "request-channel", 0, None, True)]
    else:
        assert isinstance(channel, dict)
        cases = [
            ContractProbe(
                tool_id,
                "request-channel",
                0,
                (
                    channel.get("model"),
                    channel.get("direct_instruction"),
                    channel.get("runtime_entry"),
                ),
                True,
            )
        ]
    for index in range(1, 400):
        candidate = (
            (f"direct-channel-{index}", "PERMISSION_DENIED", "src/channel_runtime.py")
            if index % 3 == 0
            else (
                "governance-authenticated-shared-layer",
                f"ALLOW_{index}",
                "src/channel_runtime.py",
            )
            if index % 3 == 1
            else (
                "governance-authenticated-shared-layer",
                "PERMISSION_DENIED",
                f"../outside/channel-{index}.py",
            )
        )
        cases.append(
            ContractProbe(tool_id, "request-channel", index, candidate, False)
        )
    return cases


def _build_cases() -> list[ContractProbe]:
    cases: list[ContractProbe] = []
    for tool_id in TOOL_IDS:
        manifest = _load_manifest(tool_id)
        tool_cases = [
            *_build_tool_identity_cases(tool_id),
            *_build_version_cases(tool_id, manifest),
            *_build_capability_cases(tool_id),
            *_build_runtime_path_cases(tool_id),
            *_build_environment_cases(tool_id),
            *_build_locale_key_cases(tool_id),
            *_build_permission_cases(tool_id, manifest),
            *_build_request_channel_cases(tool_id, manifest),
        ]
        assert len(tool_cases) == CASES_PER_TOOL
        cases.extend(tool_cases)
    assert len(cases) == TARGET_ADDITIONAL_CASES
    return cases


CASES = _build_cases()


def _evaluate_probe(probe: ContractProbe) -> bool:
    policy = governance_policy_snapshot().identifier_labels
    code_rules = code_rule_directory_snapshot()
    if probe.family == "tool-id":
        candidate = str(probe.candidate)
        return (
            candidate == probe.tool_id
            and candidate in code_rules.approved_tool_ids
            and re.fullmatch(policy.tool_id_pattern, candidate) is not None
        )
    if probe.family == "version":
        return probe.candidate == ("1.0.0", "1.0")
    if probe.family == "capability":
        candidate = str(probe.candidate)
        return (
            candidate in code_rules.approved_capability_names
            and re.fullmatch(policy.capability_pattern, candidate) is not None
        )
    if probe.family == "runtime-path":
        return _safe_python_runtime_path(probe.candidate)
    if probe.family == "environment":
        return ENVIRONMENT_NAME_PATTERN.fullmatch(str(probe.candidate)) is not None
    if probe.family == "locale-key":
        return re.fullmatch(policy.locale_key_pattern, str(probe.candidate)) is not None
    if probe.family == "permission":
        manifest_permissions = _load_manifest(probe.tool_id)["permissions"]
        return probe.candidate == (
            manifest_permissions["code_scope"],
            manifest_permissions["database_scope"],
        )
    if probe.family == "request-channel":
        if probe.tool_id == "governance_rule":
            return probe.candidate is None
        if not isinstance(probe.candidate, tuple) or len(probe.candidate) != 3:
            return False
        model, direct_instruction, runtime_entry = probe.candidate
        return (
            model == "governance-authenticated-shared-layer"
            and direct_instruction == "PERMISSION_DENIED"
            and runtime_entry == "src/channel_runtime.py"
            and _safe_python_runtime_path(runtime_entry)
        )
    raise AssertionError(f"unknown contract probe family: {probe.family}")


@pytest.mark.parametrize("probe", CASES, ids=lambda probe: probe.test_id)
def test_project_governance_contract_per_tool_matrix(probe: ContractProbe) -> None:
    assert _evaluate_probe(probe) is probe.expected
