"""Self-health test file verification for the governance audit."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


SELF_HEALTH_MANAGED_TEST_FILES = frozenset(
    {
        # ── main-system (central runtime + repair authority) ──────────
        "main-system/tests/test_main_system.py",
        # ── governance_rule (codex + enforcement) ─────────────────────
        "governance_rule/tests/test_governance_health.py",
        # ── shared-layer (central SQL index + channel) ────────────────
        "shared-layer/tests/test_shared_layer.py",
        # ── local-model / xingcheng (native model platform) ──────────
        "Standalone tools/local-model/tests/test_xingcheng.py",
        # ── global-cleaner (backup + cleanup infrastructure) ─────────
        "Standalone tools/global-cleaner/tests/test_global_cleaner.py",
        # ── ai-collaboration (governed browser automation) ───────────
        "Standalone tools/ai-collaboration/tests/test_ai_collaboration.py",
        # ── ai-assistant (investment + assistant UI) ─────────────────
        "Standalone tools/ai-assistant/tests/test_ai_assistant.py",
        # ── vaultly (encryption + vault) ─────────────────────────────
        "Standalone tools/vaultly/tests/test_vaultly.py",
        # ── file-sorter (governed file sorting) ──────────────────────
        "Standalone tools/file-sorter/tests/test_file_sorter.py",
        # ── system-rescue (central repair + packaging) ───────────────
        "Standalone tools/system-rescue/tests/test_system_rescue.py",
        # ── investment-mobile (mobile channel) ───────────────────────
        "Standalone tools/investment-mobile/tests/test_investment_mobile.py",
    }
)


def _declared_self_health_test_files(
    root: Path,
    errors: list[str],
) -> frozenset[str]:
    declared_files = {"main-system/tests/test_main_system.py"}
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
    """

    venv_python = root / "main-system" / ".venv" / "Scripts" / "python.exe"
    python_executable = str(venv_python) if venv_python.is_file() else sys.executable
    for relative_path in sorted(_declared_self_health_test_files(root, errors)):
        test_path = root / relative_path
        if not test_path.is_file():
            errors.append(f"self-health test file is missing: {relative_path}")
            continue
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
            errors.append(f"self-health test collection timed out: {relative_path}")
            continue
        collected = _collected_test_count(completed.stdout)
        if completed.returncode != 0:
            detail = completed.stdout.strip().splitlines()[-1:] or [
                completed.stderr.strip().splitlines()[-1:]
            ]
            errors.append(
                f"self-health test collection failed: {relative_path}: {detail}"
            )
        elif collected == 0:
            errors.append(f"self-health test file collects no tests: {relative_path}")


def _collected_test_count(output: str) -> int:
    match = re.search(r"(\d+) tests? collected", output)
    return int(match.group(1)) if match else 0
