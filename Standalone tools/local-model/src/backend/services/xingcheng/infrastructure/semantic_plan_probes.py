"""命令理解探針 runner（``star-semantic-plan-probes/v1``）。

對齊藍圖 L5 ②：意圖／動作／對象／參數／限制抽取之可量化驗收。
規格式套件（期望值依語意規格設計），fail-closed：套件檔案不合法
或讀取失敗即 raise，不產出假陽性報告。runner 只呼叫規則式
``StarNativeLanguageModel.semantic_plan``（不載入權重、無 GPU）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping

SUITE_FORMAT = "star-semantic-plan-probes/v1"
REPORT_FORMAT = "star-semantic-plan-probe-report/v1"

_EXPECT_KEYS = frozenset(
    {
        "primary_intent",
        "intents_contains",
        "intents_contains_any",
        "actions_contains",
        "actions_contains_any",
        "object_types_contains",
        "parameters_contains",
        "comp_constraint_types_contains",
        "requested_outputs_contains",
        "negations_nonempty",
        "tasks_min",
        "destructive",
        "confirmation_required",
        "operation_allowed",
        "prohibited_contains",
        "remaining_ambiguities_contains",
    }
)


def load_probe_suite(path: str | Path) -> dict[str, Any]:
    """Fail-closed suite validation (same contract as native_eval_suite)."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        suite = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"probe suite is not valid JSON: {exc}") from exc
    if not isinstance(suite, dict):
        raise ValueError("probe suite must be a JSON object")
    if suite.get("format_version") != SUITE_FORMAT:
        raise ValueError(
            f"probe suite format_version must be {SUITE_FORMAT}"
        )
    items = suite.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("probe suite requires a non-empty items list")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"probe item {index} is not an object")
        if not isinstance(item.get("input"), str) or not item["input"]:
            raise ValueError(f"probe item {index} requires input text")
        expect = item.get("expect")
        if not isinstance(expect, dict) or not expect:
            raise ValueError(f"probe item {index} requires expect object")
        unknown = set(expect) - _EXPECT_KEYS
        if unknown:
            raise ValueError(
                f"probe item {index} has unknown expect keys: "
                f"{sorted(unknown)}"
            )
    return suite


def _contains_all(actual: list[Any], expected: list[Any]) -> bool:
    return all(item in actual for item in expected)


def _params_subset(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    for key, wanted in expected.items():
        have = actual.get(key) or []
        if not isinstance(wanted, list):
            wanted = [wanted]
        for value in wanted:
            found = any(
                (isinstance(entry, (int, float)) and isinstance(value, (int, float)) and float(entry) == float(value))
                or entry == value
                for entry in have
            )
            if not found:
                return False
    return True


def _evaluate(plan: Mapping[str, Any], expect: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the list of failed expectations (empty = case passed)."""
    actions = [a.get("action") for a in plan.get("actions", [])]
    object_types = [
        o.get("object_type") for o in plan.get("operation_objects", [])
    ]
    comprehension = plan.get("comprehension") or {}
    comp_constraints = [
        c.get("type") for c in comprehension.get("constraints", [])
    ]
    req_outputs = list(comprehension.get("requested_outputs", []))
    safety = plan.get("safety") or {}
    failures: list[dict[str, Any]] = []

    def check(key: str, ok: bool, actual: Any) -> None:
        if not ok:
            failures.append(
                {"key": key, "expected": expect[key], "actual": actual}
            )

    if "primary_intent" in expect:
        check("primary_intent",
              plan.get("primary_intent") == expect["primary_intent"],
              plan.get("primary_intent"))
    if "intents_contains" in expect:
        check("intents_contains",
              _contains_all(plan.get("intents", []), expect["intents_contains"]),
              plan.get("intents"))
    if "intents_contains_any" in expect:
        check("intents_contains_any",
              any(i in plan.get("intents", []) for i in expect["intents_contains_any"]),
              plan.get("intents"))
    if "actions_contains" in expect:
        check("actions_contains",
              _contains_all(actions, expect["actions_contains"]),
              actions)
    if "actions_contains_any" in expect:
        check("actions_contains_any",
              any(a in actions for a in expect["actions_contains_any"]),
              actions)
    if "object_types_contains" in expect:
        check("object_types_contains",
              _contains_all(object_types, expect["object_types_contains"]),
              object_types)
    if "parameters_contains" in expect:
        check("parameters_contains",
              _params_subset(
                  plan.get("parameters", {}), expect["parameters_contains"]),
              {k: v for k, v in plan.get("parameters", {}).items() if v})
    if "comp_constraint_types_contains" in expect:
        check("comp_constraint_types_contains",
              _contains_all(
                  comp_constraints, expect["comp_constraint_types_contains"]),
              comp_constraints)
    if "requested_outputs_contains" in expect:
        check("requested_outputs_contains",
              _contains_all(
                  req_outputs, expect["requested_outputs_contains"]),
              req_outputs)
    if "negations_nonempty" in expect:
        non_empty = bool(comprehension.get("negations"))
        check("negations_nonempty",
              non_empty == bool(expect["negations_nonempty"]),
              comprehension.get("negations"))
    if "tasks_min" in expect:
        check("tasks_min",
              len(plan.get("tasks", [])) >= int(expect["tasks_min"]),
              len(plan.get("tasks", [])))
    if "destructive" in expect:
        check("destructive",
              bool(safety.get("destructive_operation"))
              == bool(expect["destructive"]),
              safety.get("destructive_operation"))
    if "confirmation_required" in expect:
        check("confirmation_required",
              bool(safety.get("confirmation_required"))
              == bool(expect["confirmation_required"]),
              safety.get("confirmation_required"))
    if "operation_allowed" in expect:
        check("operation_allowed",
              bool(safety.get("operation_allowed"))
              == bool(expect["operation_allowed"]),
              safety.get("operation_allowed"))
    if "prohibited_contains" in expect:
        check("prohibited_contains",
              _contains_all(
                  plan.get("prohibited_intents", []),
                  expect["prohibited_contains"]),
              plan.get("prohibited_intents"))
    if "remaining_ambiguities_contains" in expect:
        check("remaining_ambiguities_contains",
              _contains_all(
                  safety.get("remaining_ambiguities", []),
                  expect["remaining_ambiguities_contains"]),
              safety.get("remaining_ambiguities"))
    return failures


def run_probes(
    suite_path: str | Path,
    *,
    tool_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run every probe case and return the structured report."""
    suite = load_probe_suite(suite_path)
    from .native_model import StarNativeLanguageModel

    results: list[dict[str, Any]] = []
    passed = 0
    for item in suite["items"]:
        plan = StarNativeLanguageModel.semantic_plan(
            item["input"], context=str(item.get("context") or "")
        )
        failures = _evaluate(plan, item["expect"])
        results.append(
            {
                "id": item.get("id"),
                "input": item["input"],
                "passed": not failures,
                "failed_expectations": failures,
                "observed": {
                    "primary_intent": plan.get("primary_intent"),
                    "intents": plan.get("intents"),
                    "actions": [
                        a.get("action") for a in plan.get("actions", [])
                    ],
                },
            }
        )
        if not failures:
            passed += 1
    total = len(results)
    report = {
        "format_version": REPORT_FORMAT,
        "suite_id": suite["suite_id"],
        "suite_path": str(suite_path),
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate_pct": round(100.0 * passed / total, 1) if total else 0.0,
        "results": results,
    }
    if tool_root is not None:
        log_dir = (
            Path(tool_root) / "xingcheng" / "runtime" / "logs"
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        report_path = log_dir / f"semantic-plan-probes-{stamp}.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report["report_path"] = str(report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="star-semantic-plan-probes runner"
    )
    parser.add_argument(
        "--suite",
        default="xingcheng/eval/star-semantic-plan-probes-v1.json",
    )
    parser.add_argument(
        "--tool-root",
        default=".",
        help="local-model tool root (report written under "
        "xingcheng/runtime/logs; '' disables persistence)",
    )
    args = parser.parse_args(argv)
    report = run_probes(
        args.suite, tool_root=args.tool_root or None
    )
    print(
        f"{report['suite_id']}: {report['passed']}/{report['total']} "
        f"passed ({report['pass_rate_pct']}%)"
    )
    for result in report["results"]:
        if not result["passed"]:
            keys = [
                f["key"] for f in result["failed_expectations"]
            ]
            print(f"  FAIL {result['id']}: {sorted(set(keys))}")
    if report.get("report_path"):
        print(f"report: {report['report_path']}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
