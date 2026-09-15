"""Self-health test file verification for the governance audit."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SELF_HEALTH_MANAGED_TEST_FILES = frozenset(
    {
        # ── main-system (central runtime + repair authority) ──────────
        "main-system/tests/test_project_contract_matrix_part1.py",
        "main-system/tests/test_project_contract_matrix_part2.py",
        "main-system/tests/test_third_party_manager.py",
        "main-system/tests/test_git_tier_governance.py",
        "main-system/tests/test_metadata_contract.py",
        "main-system/tests/test_governance_authentication.py",
        "main-system/tests/test_governance_path_guard.py",
        "main-system/tests/test_connection_watchdog_consolidated.py",
        "main-system/tests/test_repair_learning.py",
        "main-system/tests/test_special_unpacked_runtime_part1.py",
        "main-system/tests/test_special_unpacked_runtime_part2.py",
        "main-system/tests/test_project_10000_matrix.py",
        # ── governance_rule (codex + enforcement) ─────────────────────
        "governance_rule/tests/test_governance_health.py",
        # ── shared-layer (central SQL index + channel) ────────────────
        "shared-layer/tests/test_shared_layer.py",
        "shared-layer/tests/test_shared_layer_sub_sovereign.py",
        # ── local-model / xingcheng (native model platform) ──────────
        "Standalone tools/local-model/tests/test_xingcheng_layering.py",
        "Standalone tools/local-model/tests/test_model_registry_part1.py",
        "Standalone tools/local-model/tests/test_model_registry_part2.py",
        "Standalone tools/local-model/tests/test_model_registry_part3.py",
        "Standalone tools/local-model/tests/test_model_registry_part4.py",
        "Standalone tools/local-model/tests/test_transformer_runtime_part1.py",
        "Standalone tools/local-model/tests/test_transformer_runtime_part2.py",
        "Standalone tools/local-model/tests/test_transformer_runtime_part3.py",
        "Standalone tools/local-model/tests/test_transformer_runtime_part4.py",
        "Standalone tools/local-model/tests/test_transformer_training_repository.py",
        "Standalone tools/local-model/tests/test_local_rag.py",
        "Standalone tools/local-model/tests/test_local_sqlite_rag_repository.py",
        "Standalone tools/local-model/tests/test_model_parameter_policy.py",
        "Standalone tools/local-model/tests/test_reading_expert.py",
        "Standalone tools/local-model/tests/test_gpt_training.py",
        "Standalone tools/local-model/tests/test_google_search.py",
        "Standalone tools/local-model/tests/test_resource_manager.py",
        "Standalone tools/local-model/tests/test_capability_composer.py",
        "Standalone tools/local-model/tests/test_capability_evaluation.py",
        "Standalone tools/local-model/tests/test_coding_expert_1000_matrix.py",
        # ── global-cleaner (backup + cleanup infrastructure) ─────────
        "Standalone tools/global-cleaner/tests/test_global_cleaner_layering.py",
        "Standalone tools/global-cleaner/tests/test_global_cleaner_precision.py",
        "Standalone tools/global-cleaner/tests/test_governed_backup_and_repair.py",
        "Standalone tools/global-cleaner/tests/test_managed_temp_cleanup.py",
        "Standalone tools/global-cleaner/tests/test_ai_assistant_layering.py",
        "Standalone tools/global-cleaner/tests/test_ai_collaboration_layering.py",
        "Standalone tools/global-cleaner/tests/test_ai_channel_governance.py",
        "Standalone tools/global-cleaner/tests/test_file_sorter_layering.py",
        "Standalone tools/global-cleaner/tests/test_investment_mobile_governance.py",
        "Standalone tools/global-cleaner/tests/test_investment_mobile_layering.py",
        "Standalone tools/global-cleaner/tests/test_main_system_boundaries.py",
        "Standalone tools/global-cleaner/tests/test_main_system_governance_health.py",
        "Standalone tools/global-cleaner/tests/test_shared_layer_dual_channels.py",
        "Standalone tools/global-cleaner/tests/test_shared_layer_ownership.py",
        "Standalone tools/global-cleaner/tests/test_shared_layer_retention.py",
        "Standalone tools/global-cleaner/tests/test_vaultly_layering.py",
        # ── ai-collaboration (governed browser automation) ───────────
        "Standalone tools/ai-collaboration/tests/test_ai_collaboration_routing.py",
        "Standalone tools/ai-collaboration/tests/test_ai_collaboration_workflows.py",
        # ── ai-assistant (investment + assistant UI) ─────────────────
        "Standalone tools/ai-assistant/tests/test_ai_assistant_core.py",
        "Standalone tools/ai-assistant/tests/test_investment_analytics.py",
        "Standalone tools/ai-assistant/tests/test_investment_manager.py",
        "Standalone tools/ai-assistant/tests/test_investment_v3.py",
        "Standalone tools/ai-assistant/tests/test_shared_mobile_runtime.py",
        # ── vaultly (encryption + vault) ─────────────────────────────
        "Standalone tools/vaultly/tests/test_vaultly.py",
        # ── file-sorter (governed file sorting) ──────────────────────
        "Standalone tools/file-sorter/tests/test_file_sorter.py",
        "Standalone tools/file-sorter/tests/test_file_sorter_automation.py",
        "Standalone tools/file-sorter/tests/test_file_sorter_cleanup.py",
        "Standalone tools/file-sorter/tests/test_file_sorter_metadata_v2.py",
        "Standalone tools/file-sorter/tests/test_file_sorter_ui.py",
        "Standalone tools/file-sorter/tests/test_file_sorter_v2.py",
        # ── system-rescue (central repair + packaging) ───────────────
        "Standalone tools/system-rescue/tests/test_system_rescue.py",
        # ── investment-mobile (mobile channel) ───────────────────────
        "Standalone tools/investment-mobile/tests/test_investment_mobile_reexport.py",
    }
)


def _declared_self_health_test_files(
    root: Path,
    errors: list[str],
) -> frozenset[str]:
    declared_files = set(SELF_HEALTH_MANAGED_TEST_FILES)
    manifest_paths = [
        *root.glob("*/manifest.json"),
        *root.glob("Standalone tools/*/manifest.json"),
        *root.glob("Standalone tools/*/*/manifest.json"),
    ]
    for manifest_path in sorted(set(manifest_paths)):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(
                f"self-health manifest is unreadable: "
                f"{manifest_path.relative_to(root).as_posix()}: {exc}"
            )
            continue
        tool_id = str(manifest.get("id") or "").strip()
        targets = manifest.get("test_targets")
        if not tool_id or not isinstance(targets, list) or not targets:
            errors.append(
                "governed tool must declare test_targets: "
                f"{manifest_path.relative_to(root).as_posix()}"
            )
            continue
        tool_root = manifest_path.parent.resolve()
        for raw_target in targets:
            relative_target = Path(str(raw_target or "").strip())
            candidate = (tool_root / relative_target).resolve()
            try:
                candidate.relative_to(tool_root)
                relative_path = candidate.relative_to(root).as_posix()
            except ValueError:
                errors.append(f"self-health test target escaped tool root: {tool_id}")
                continue
            if (
                relative_target.is_absolute()
                or candidate.suffix.casefold() != ".py"
                or not candidate.name.startswith("test_")
            ):
                errors.append(
                    f"invalid self-health test target: {tool_id}: {raw_target}"
                )
                continue
            declared_files.add(relative_path)
    return frozenset(declared_files)


def _verify_self_health_test_files(
    root: Path,
    errors: list[str],
) -> None:
    """Verify governed test files exist and can be collected by pytest.

    Maintained by the maintenance sovereign as the self-detection health
    barrier (article A57/edict E43): every governed tool keeps a test file
    that can be collected offline so governance health checks never depend
    on a live model server.

    The per-file ``pytest --collect-only`` probes are independent read-only
    checks, so they run in a bounded thread pool.  The bounded worker count
    keeps local load low while the whole barrier finishes well inside the
    audit time budget instead of serialising one interpreter start per file.
    """

    venv_python = root / "main-system" / ".venv" / "Scripts" / "python.exe"
    python_executable = str(venv_python) if venv_python.is_file() else sys.executable
    declared_files = sorted(_declared_self_health_test_files(root, errors))

    def collect(relative_path: str) -> list[str]:
        findings: list[str] = []
        test_path = root / relative_path
        if not test_path.is_file():
            findings.append(f"self-health test file is missing: {relative_path}")
            return findings
        try:
            completed = subprocess.run(
                [
                    python_executable,
                    "-m",
                    "pytest",
                    str(test_path),
                    "--collect-only",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            findings.append(f"self-health test collection timed out: {relative_path}")
            return findings
        collected = _collected_test_count(completed.stdout)
        if completed.returncode != 0:
            detail = completed.stdout.strip().splitlines()[-1:] or [
                completed.stderr.strip().splitlines()[-1:]
            ]
            findings.append(
                f"self-health test collection failed: {relative_path}: {detail}"
            )
        elif collected == 0:
            findings.append(f"self-health test file collects no tests: {relative_path}")
        return findings

    workers = max(1, min(4, len(declared_files)))
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="self-health",
    ) as executor:
        # map preserves submission order so findings stay deterministic.
        for findings in executor.map(collect, declared_files):
            errors.extend(findings)


def _collected_test_count(output: str) -> int:
    match = re.search(r"(\d+) tests? collected", output)
    return int(match.group(1)) if match else 0
