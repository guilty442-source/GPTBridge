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

    Collection is batched: one ``pytest --collect-only`` process covers every
    declared file and the per-file verdict (collected count / collection
    error) is parsed from its node-id and ``ERROR`` lines.  A single pytest
    start costs ~1.5s while the previous per-file probes paid that cost ~80
    times (≈35s per audit); the batched path finishes in a few seconds.
    If the batch cannot be attributed (timeout or aborted run) the check
    falls back to the original bounded per-file probes, so the barrier stays
    fail-closed.
    """

    venv_python = root / "main-system" / ".venv" / "Scripts" / "python.exe"
    python_executable = str(venv_python) if venv_python.is_file() else sys.executable
    declared_files = sorted(_declared_self_health_test_files(root, errors))

    existing: list[str] = []
    for relative_path in declared_files:
        if (root / relative_path).is_file():
            existing.append(relative_path)
        else:
            errors.append(f"self-health test file is missing: {relative_path}")
    if not existing:
        return

    batched = _batched_collection_results(root, python_executable, existing)
    if batched is None:
        _verify_self_health_per_file(root, python_executable, existing, errors)
        return
    for relative_path in existing:
        ok, detail = batched.get(
            relative_path, (False, "collection result missing")
        )
        if ok:
            continue
        if detail == "no tests collected":
            errors.append(f"self-health test file collects no tests: {relative_path}")
        else:
            errors.append(
                f"self-health test collection failed: {relative_path}: {detail}"
            )


def _normalized_test_reference(raw: str) -> str:
    """Normalize a pytest path reference to a root-relative POSIX path."""
    return raw.strip().strip("\"'").replace("\\", "/").lstrip("./")


_SELF_HEALTH_BATCH_CHUNKS = 4


def _batched_collection_results(
    root: Path,
    python_executable: str,
    declared_files: list[str],
) -> dict[str, tuple[bool, str]] | None:
    """Collect every declared file in parallel pytest batches.

    The declared files are split across a few chunks, each collected by one
    pytest process concurrently; per-file verdicts are parsed from node-id
    and ``ERROR`` lines and merged.  Returns ``None`` when any chunk cannot
    be attributed (timeout or aborted run) so the caller can fall back to
    per-file probes.
    """
    chunk_count = max(1, min(_SELF_HEALTH_BATCH_CHUNKS, len(declared_files)))
    if chunk_count == 1 or len(declared_files) < _SELF_HEALTH_BATCH_CHUNKS * 2:
        return _collect_one_batch(root, python_executable, declared_files)

    chunks = [
        declared_files[index::chunk_count] for index in range(chunk_count)
    ]
    chunks = [chunk for chunk in chunks if chunk]
    with ThreadPoolExecutor(
        max_workers=len(chunks),
        thread_name_prefix="self-health-batch",
    ) as executor:
        outcomes = list(
            executor.map(
                lambda chunk: _collect_one_batch(
                    root, python_executable, chunk
                ),
                chunks,
            )
        )
    if any(outcome is None for outcome in outcomes):
        return None
    merged: dict[str, tuple[bool, str]] = {}
    for outcome in outcomes:
        if outcome is not None:
            merged.update(outcome)
    return merged


def _collect_one_batch(
    root: Path,
    python_executable: str,
    batch: list[str],
) -> dict[str, tuple[bool, str]] | None:
    """Run one pytest collection batch; map file -> (ok, detail).

    Returns ``None`` when the run cannot be attributed (timeout or an
    aborted interpreter).
    """
    try:
        completed = subprocess.run(
            [
                python_executable,
                "-m",
                "pytest",
                *batch,
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                "--tb=no",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return None

    output = "\n".join(
        part for part in (completed.stdout or "", completed.stderr or "") if part
    )
    counts: dict[str, int] = {}
    failures: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("ERROR "):
            _, _, remainder = line.partition(" ")
            reference = _normalized_test_reference(remainder.split(" - ", 1)[0])
            if reference:
                failures.setdefault(reference, remainder.strip()[:200])
            continue
        if "::" in line:
            reference = _normalized_test_reference(line.split("::", 1)[0])
            if reference:
                counts[reference] = counts.get(reference, 0) + 1

    results: dict[str, tuple[bool, str]] = {}
    for relative_path in batch:
        reference = _normalized_test_reference(relative_path)
        if reference in failures:
            results[relative_path] = (False, failures[reference])
        elif counts.get(reference, 0) > 0:
            results[relative_path] = (True, "")
        else:
            results[relative_path] = (False, "no tests collected")

    batch_references = {_normalized_test_reference(path) for path in batch}
    if not batch_references.intersection({*counts, *failures}):
        # Nothing in this batch was attributed: the run aborted before
        # collecting (e.g. a broken plugin).  Fall back rather than flag
        # every file.
        return None
    return results


def _verify_self_health_per_file(
    root: Path,
    python_executable: str,
    declared_files: list[str],
    errors: list[str],
) -> None:
    """Bounded per-file fallback probes (original behaviour)."""

    def collect(relative_path: str) -> list[str]:
        findings: list[str] = []
        test_path = root / relative_path
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
