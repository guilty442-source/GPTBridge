"""Test ownership inventory — classify existing tests per A56/A210/A440.

Authority: language ownership is governed by the codex; this inventory is
derived and rebuildable.  Deleting the output is safe; regenerate with this
script.  No test is moved or deleted by this script — it only classifies.

CLI::

    python scripts/build-test-ownership-inventory.py [--output <path>] [--summary]
"""
from __future__ import annotations

import argparse
import json
import hashlib
import re
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "governance_rule" / "execution" / "convergence" / "test-ownership-inventory.json"

SCHEMA = "test-ownership-inventory/v1"
CLASSIFIER_VERSION = "ownership-classifier/v1"

# Ownership categories per blueprint G98
# A56 / A210 / A440 — keep the mapping explicit and audit-friendly.
CATEGORIES = (
    "native-unit",          # C/C++ in native/test_suites
    "python-unit",          # Python-only logic
    "abi-boundary",         # C ABI / pybind boundary
    "parity",               # Python vs native parity
    "csharp-xunit",         # C# xUnit
    "cross-language-integration",  # IPC / multi-language
)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def content_hash(payload: object) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def classify(path: Path) -> str:
    """Heuristic classification — single-owner, no guessing beyond path + name."""
    rel = path.as_posix().lower()
    name = path.name.lower()
    # C# xUnit
    if "native/test_suites/csharp" in rel or rel.endswith(".cs"):
        return "csharp-xunit"
    # Native C++ suites
    if "native/test_suites" in rel and name.startswith("suite_"):
        # suite_consistency is parity, others are native-unit
        if "consistency" in name or "parity" in name:
            return "parity"
        return "native-unit"
    if "native/test_suites" in rel and name.startswith("test_"):
        return "native-unit"
    # ABI boundary — explicit
    if "abi" in name or "binding" in name or "native" in name and "boundary" in name:
        return "abi-boundary"
    if "g98" in name or "ownership" in name:
        return "cross-language-integration"
    # Parity tests
    if "parity" in rel or "consistency" in rel:
        return "parity"
    # Cross-language integration — IPC, workflow, integration-04b, toolbox
    if any(k in rel for k in ("ipc", "workflow", "integration-04b", "toolbox", "soak", "orbit", "ipc-compat")):
        return "cross-language-integration"
    # Default Python unit — covers main-system, shared-layer, governance_rule Python tests
    return "python-unit"


def collect() -> list[dict]:
    patterns = [
        "native/test_suites/**/*.cpp",
        "native/test_suites/**/*.hpp",
        "native/test_suites/**/*.cs",
        "main-system/tests/**/*.py",
        "governance_rule/tests/**/*.py",
        "shared-layer/tests/**/*.py",
        "tests/**/*.py",
        "scripts/**/*.py",
    ]
    # Use project-root glob; include only test-like files
    files: list[Path] = []
    for pat in patterns:
        for p in PROJECT_ROOT.glob(pat):
            if p.is_file():
                # Only test files or suite files, or governance scripts that are test-related
                if pat.startswith("scripts/"):
                    # only include scripts that are test harnesses
                    if "test" not in p.name.lower() and "suite" not in p.name.lower():
                        continue
                files.append(p)
    # Also include any file named test_*.py anywhere (catch-all)
    for p in PROJECT_ROOT.rglob("test_*.py"):
        if p.is_file() and p not in files:
            # Skip venv, runtime, worktrees, releases
            if any(seg in p.parts for seg in (".venv", "runtime", "releases", ".git", "__pycache__", "bin", "dist", ".worktrees", ".kilo")):
                continue
            files.append(p)
    # Deduplicate and sort
    files = sorted(set(files))
    out: list[dict] = []
    for f in files:
        rel = f.relative_to(PROJECT_ROOT).as_posix()
        cat = classify(f)
        out.append({"path": rel, "category": cat, "language": "cpp" if f.suffix in (".cpp", ".hpp", ".cs") else "python"})
    return sorted(out, key=lambda x: x["path"])


def build(output: Path) -> dict:
    inventory = collect()
    by_category: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for entry in inventory:
        by_category.setdefault(entry["category"], []).append(entry["path"])
    payload = {
        "schema": SCHEMA,
        "generated_at_utc": utc_now(),
        "generator": "scripts/build-test-ownership-inventory.py",
        "classifier_version": CLASSIFIER_VERSION,
        "authority": "derived-rebuildable; language ownership per A56/A210/A440; no test moved or deleted",
        "counts": {
            "total": len(inventory),
            "by_category": {k: len(v) for k, v in sorted(by_category.items())},
            "by_language": {
                "python": sum(1 for e in inventory if e["language"] == "python"),
                "cpp": sum(1 for e in inventory if e["language"] == "cpp"),
            },
        },
        "inventory": inventory,
        "indexes": {
            "by_category": {k: sorted(v) for k, v in sorted(by_category.items())},
        },
    }
    payload["content_sha256"] = content_hash({k: v for k, v in payload.items() if k != "content_sha256"})
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    tmp.replace(output)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    payload = build(Path(args.output))
    if args.summary:
        print(json.dumps({"output": args.output, "schema": payload["schema"], "counts": payload["counts"], "content_sha256": payload["content_sha256"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
