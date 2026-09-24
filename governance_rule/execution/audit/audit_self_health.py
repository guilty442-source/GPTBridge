"""Self-health test file verification for the governance audit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
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
        "governance_rule/tests/test_global_cleaner_retired.py",
        # ── shared-layer (central SQL index + channel) ────────────────
        "shared-layer/tests/test_shared_layer.py",
        "shared-layer/tests/test_shared_layer_sub_sovereign.py",
        # ── local-model / xingcheng (native model platform) ──────────
        # A163/ef5df1a5: Ollama 耦合已切除 — transformer_runtime/model
        # registry/resource_manager/model_parameter_policy/gpt_training
        # 等 11 個測試檔隨其被測模組一併退役；屏障改由 native 路徑
        # 繼任測試檔承擔（StarNativeRuntime/native_engine/native
        # transformer/lifecycle owner/self-learning gate）。
        "Standalone tools/local-model/tests/test_xingcheng_layering.py",
        "Standalone tools/local-model/tests/test_native_engine.py",
        "Standalone tools/local-model/tests/test_native_transformer.py",
        "Standalone tools/local-model/tests/test_model_service_lifecycle_owner.py",
        "Standalone tools/local-model/tests/test_lifecycle_resource_actions.py",
        "Standalone tools/local-model/tests/test_self_learning_gates.py",
        "Standalone tools/local-model/tests/test_transformer_training_repository.py",
        "Standalone tools/local-model/tests/test_local_rag.py",
        "Standalone tools/local-model/tests/test_local_sqlite_rag_repository.py",
        "Standalone tools/local-model/tests/test_reading_expert.py",
        "Standalone tools/local-model/tests/test_google_search.py",
        "Standalone tools/local-model/tests/test_capability_composer.py",
        "Standalone tools/local-model/tests/test_capability_evaluation.py",
        "Standalone tools/local-model/tests/test_coding_expert_1000_matrix.py",
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
        lifecycle = manifest.get("lifecycle")
        if manifest.get("enabled") is False or (
            isinstance(lifecycle, dict)
            and str(lifecycle.get("status") or "").strip().casefold()
            == "retired"
        ):
            # A533/A534: a retired owner is source lineage evidence, not a
            # governed executable; it declares no collectable test barrier.
            continue
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


# §10.30/A590: batch parallelism is capped at the five-core budget; the audit
# cannot import shared_layer, so the bound is expressed as a local clamp.
_SELF_HEALTH_BATCH_CHUNKS = 5

# Relative pytest-collection weight per owning area: the native model tests
# import heavy ML dependencies (torch et al.) and dominate a chunk's runtime,
# so chunks are balanced by estimated weight rather than raw file count.
_SELF_HEALTH_AREA_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("Standalone tools/local-model/", 4),
    ("shared-layer/", 3),
    ("Standalone tools/ai-assistant/", 3),
    ("governance_rule/", 2),
    ("main-system/", 2),
)


def _test_file_weight(relative_path: str) -> int:
    for prefix, weight in _SELF_HEALTH_AREA_WEIGHTS:
        if relative_path.startswith(prefix):
            return weight
    return 1


# A533/A534: retired owners (e.g. global-cleaner) keep no executable test
# barrier; their historic suites are excluded from self-health collection.
_SELF_HEALTH_ISOLATED_AREAS: tuple[str, ...] = ()


def _balance_chunks(
    declared_files: list[str],
    chunk_count: int,
) -> list[list[str]]:
    """Greedy weight-balanced chunking (deterministic for a fixed input).

    Isolated areas get their own batches first; the remaining chunk budget
    is filled by weight-balanced greedy assignment of every other file.
    """
    isolated: list[list[str]] = []
    for area in _SELF_HEALTH_ISOLATED_AREAS:
        group = sorted(path for path in declared_files if path.startswith(area))
        if group:
            isolated.append(group)
    shared = [
        path
        for path in declared_files
        if not path.startswith(_SELF_HEALTH_ISOLATED_AREAS)
    ]
    remaining = max(1, chunk_count - len(isolated))
    chunks: list[list[str]] = [[] for _ in range(remaining)]
    loads = [0] * remaining
    for relative_path in sorted(
        shared,
        key=lambda path: (-_test_file_weight(path), path),
    ):
        target = min(range(remaining), key=lambda index: (loads[index], index))
        chunks[target].append(relative_path)
        loads[target] += _test_file_weight(relative_path)
    return [*isolated, *(sorted(chunk) for chunk in chunks if chunk)]


# ----------------------------------------------------------------------
# Collection-result cache
# ----------------------------------------------------------------------
# Pytest collection re-imports every declared test module (and its heavy
# dependency closure — torch et al.) on every audit, costing ~19s of the
# A537 30s budget even though the outcome is deterministic for a fixed
# input set.  The cache below reuses per-file verdicts only while the
# fingerprint proves nothing importable changed: a deleted, renamed or
# edited ``.py`` anywhere under the source roots alters the fingerprint
# (the P27 misdeleted-module regression class is covered by the path set
# alone), declared test-file edits are caught by their content hashes,
# and interpreter/package changes are caught by the venv stamp.

_SELF_HEALTH_CACHE_REL = Path(
    "main-system", "runtime", "cache", "self-health-collection.json"
)

# Directories whose Python surface the declared tests may import.  Anything
# outside this set cannot influence collection (data, docs, runtime state).
_SELF_HEALTH_FINGERPRINT_ROOTS: tuple[str, ...] = (
    "main-system/src-core",
    "main-system/governance",
    "main-system/tests",
    "main-system/scripts",
    "governance_rule",
    "shared-layer/src",
    "shared-layer/tests",
    "Standalone tools",
)

_SELF_HEALTH_FINGERPRINT_EXCLUDES: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        ".worktrees",
        ".kilo",
        "__pycache__",
        "node_modules",
        "runtime",
        "dist-native",
        "bin",
        "wheel-cache",
        ".cpp-build",
        ".cu-build",
        "releases",
        "data",
        "logs",
        "state",
        "temp",
        "corpus",
        "corpus-v1",
        "corpus-v2",
    }
)

_SELF_HEALTH_CACHE_SCHEMA = "self-health-collection/v1"


def _source_tree_fingerprint(root: Path) -> str:
    """Stat-only fingerprint of every importable ``.py`` under the roots.

    Hashes ``(relpath, size, mtime_ns)`` for each file — edits, deletes,
    renames and new modules all change the digest.  File contents are not
    read, so the walk stays in tens of milliseconds even on large trees.
    """
    digest = hashlib.sha256()
    for root_name in _SELF_HEALTH_FINGERPRINT_ROOTS:
        base = root / root_name
        if not base.is_dir():
            digest.update(f"MISSING:{root_name};".encode())
            continue
        entries: list[str] = []
        stack = [base]
        while stack:
            current = stack.pop()
            try:
                children = sorted(os.scandir(current), key=lambda e: e.name)
            except OSError:
                continue
            for entry in children:
                if entry.name in _SELF_HEALTH_FINGERPRINT_EXCLUDES:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.name.endswith(".py"):
                        stat = entry.stat(follow_symlinks=False)
                        entries.append(
                            f"{entry.path}:{stat.st_size}:{stat.st_mtime_ns}"
                        )
                except OSError:
                    continue
        for line in entries:
            digest.update(line.encode())
            digest.update(b"\x00")
    return digest.hexdigest()


def _collection_cache_key(
    root: Path,
    python_executable: str,
    declared_files: list[str],
) -> str:
    """Full cache key: test contents + source-tree surface + environment."""
    digest = hashlib.sha256()
    digest.update(_SELF_HEALTH_CACHE_SCHEMA.encode())
    for relative_path in declared_files:
        digest.update(relative_path.encode())
        digest.update(b"\x00")
        try:
            digest.update(
                hashlib.sha256(
                    (root / relative_path).read_bytes()
                ).hexdigest().encode()
            )
        except OSError:
            digest.update(b"UNREADABLE")
        digest.update(b"\x00")
    digest.update(_source_tree_fingerprint(root).encode())
    # Environment stamp: interpreter identity + site-packages listing stamp
    # (installs/uninstalls change the collection outcome without touching
    # any repo file).
    try:
        exe_stat = Path(python_executable).stat()
        digest.update(
            f"{python_executable}:{exe_stat.st_size}:{exe_stat.st_mtime_ns}".encode()
        )
    except OSError:
        digest.update(python_executable.encode())
    site_packages = (
        Path(python_executable).resolve().parents[1] / "Lib" / "site-packages"
    )
    try:
        stamp = site_packages.stat()
        count = sum(1 for _ in os.scandir(site_packages))
        digest.update(
            f"site-packages:{stamp.st_mtime_ns}:{count}".encode()
        )
    except OSError:
        digest.update(b"site-packages:missing")
    return digest.hexdigest()


def _collection_cache_path(root: Path) -> Path:
    return root / _SELF_HEALTH_CACHE_REL


def _collection_cache_read(
    root: Path,
    python_executable: str,
    declared_files: list[str],
) -> dict[str, tuple[bool, str]] | None:
    """Return cached per-file verdicts when the fingerprint still matches."""
    if str(os.environ.get("GPTBRIDGE_SELF_HEALTH_NO_CACHE", "")).strip():
        return None
    cache_path = _collection_cache_path(root)
    try:
        record = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("schema") != _SELF_HEALTH_CACHE_SCHEMA:
        return None
    if record.get("key") != _collection_cache_key(
        root, python_executable, declared_files
    ):
        return None
    results = record.get("results")
    if not isinstance(results, dict):
        return None
    restored: dict[str, tuple[bool, str]] = {}
    for relative_path in declared_files:
        entry = results.get(relative_path)
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not isinstance(entry[0], bool)
            or not isinstance(entry[1], str)
        ):
            return None
        restored[relative_path] = (entry[0], entry[1])
    return restored


def _collection_cache_write(
    root: Path,
    python_executable: str,
    declared_files: list[str],
    results: dict[str, tuple[bool, str]],
) -> None:
    """Persist verdicts atomically; cache corruption only costs a re-run."""
    cache_path = _collection_cache_path(root)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema": _SELF_HEALTH_CACHE_SCHEMA,
            "key": _collection_cache_key(
                root, python_executable, declared_files
            ),
            "written_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "results": {
                path: [ok, detail] for path, (ok, detail) in results.items()
            },
        }
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(temporary, cache_path)
    except OSError:
        pass


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

    A fingerprint-keyed cache short-circuits the whole collection when
    nothing importable changed since the last verified run; only a
    complete, attributable result set is ever written to the cache.
    """
    cached = _collection_cache_read(root, python_executable, declared_files)
    if cached is not None:
        return cached
    chunk_count = max(1, min(_SELF_HEALTH_BATCH_CHUNKS, len(declared_files)))
    if chunk_count == 1 or len(declared_files) < _SELF_HEALTH_BATCH_CHUNKS * 2:
        single = _collect_one_batch(root, python_executable, declared_files)
        if single is not None:
            _collection_cache_write(
                root, python_executable, declared_files, single
            )
        return single

    chunks = _balance_chunks(declared_files, chunk_count)
    with ThreadPoolExecutor(
        max_workers=min(len(chunks), _SELF_HEALTH_BATCH_CHUNKS),
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
    _collection_cache_write(root, python_executable, declared_files, merged)
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
