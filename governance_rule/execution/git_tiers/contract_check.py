"""§10.8 跨模組契約檢查（合併成功 ≠ 整合成功）。

在 merge-queue precheck 之後、實際 merge 之前執行：
成對路徑規則（paired-path rules）——當 branch 觸及 ``trigger_glob``
時，同一 diff 必須同時觸及任一 ``require_glob``（成對變更），
例如 IPC 介面改了但對端契約檔沒改 → 攔下。

嚴重度：
- ``error``：阻擋合併（fail-closed）。
- ``warn``：記錄到 queue detail，不阻擋。

規則檔：``governance_rule/execution/git_tiers/contract_rules.json``。
"""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from .git_repository import GitRepository

_logger = logging.getLogger("gptbridge.git.contract_check")

_RULES_PATH = Path(__file__).with_name("contracts") / "contract_rules.json"


def _touched_paths(repo: GitRepository, target: str, source: str) -> list[str]:
    result = repo.run(["diff", "--name-only", f"{target}...{source}"])
    if result.returncode != 0:
        return []
    return sorted(
        line.strip() for line in result.stdout.splitlines() if line.strip()
    )


def load_rules(path: str | Path | None = None) -> list[dict[str, Any]]:
    rules_path = Path(path) if path else _RULES_PATH
    try:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _logger.warning("contract rules unreadable (%s): %s", rules_path, exc)
        return []
    return list(data.get("rules", []))


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def contract_check(
    repo: GitRepository,
    source: str,
    *,
    target: str = "main",
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """評估 branch 是否滿足成對契約規則。

    回傳 ``{"ok": bool, "violations": [...], "warnings": [...],
    "checked_rules": int, "touched": [...]}``；
    error 級違規 → ``ok=False``（fail-closed）。
    """
    touched = _touched_paths(repo, target, source)
    active_rules = rules if rules is not None else load_rules()
    violations: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    for rule in active_rules:
        triggers = rule.get("trigger_globs", [])
        requires = rule.get("require_globs", [])
        if not triggers or not requires:
            continue
        if not _matches_any_set(touched, triggers):
            continue
        if _matches_any_set(touched, requires):
            continue
        entry = {
            "rule": str(rule.get("name", "unnamed")),
            "severity": str(rule.get("severity", "warn")),
            "detail": str(rule.get("detail", "")),
        }
        if entry["severity"] == "error":
            violations.append(entry)
        else:
            warnings.append(entry)

    return {
        "ok": not violations,
        "violations": violations,
        "warnings": warnings,
        "checked_rules": len(active_rules),
        "touched": touched,
    }


def _matches_any_set(paths: Iterable[str], patterns: Iterable[str]) -> bool:
    return any(
        fnmatch.fnmatch(path, pattern)
        for path in paths
        for pattern in patterns
    )
