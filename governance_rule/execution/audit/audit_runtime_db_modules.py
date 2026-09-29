"""Runtime module-presence audit checks (query/perf helpers)."""

from __future__ import annotations

from pathlib import Path

from ._file_cache import read_text_cached

def check_query_fingerprint_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime query fingerprint module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "query_fingerprint.py"
    if not module.is_file():
        errors.append("Query fingerprint module is missing")
        return
    text = read_text_cached(module)
    for required in ("record", "get_hot"):
        if required not in text:
            errors.append(f"Query fingerprint module is missing: {required}")

def check_batch_writer_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime batch writer module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "batch_writer.py"
    if not module.is_file():
        errors.append("Batch writer module is missing")
        return
    text = read_text_cached(module)
    for required in ("BatchWriter", "add", "flush", "adjust_batch_size"):
        if required not in text:
            errors.append(f"Batch writer module is missing: {required}")

def check_locator_cache_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime locator cache module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "locator_cache.py"
    if not module.is_file():
        errors.append("Locator cache module is missing")
        return
    text = read_text_cached(module)
    for required in ("LocatorCache", "get", "put", "invalidate", "stats"):
        if required not in text:
            errors.append(f"Locator cache module is missing: {required}")

def check_prepared_query_catalog_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime prepared query catalog module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "prepared_query_catalog.py"
    if not module.is_file():
        errors.append("Prepared query catalog module is missing")
        return
    text = read_text_cached(module)
    for required in ("CATALOG", "get_query", "list_queries",
                     "lookup_resource", "claim_request", "append_audit",
                     "update_index_state", "lookup_locator", "fetch_relationships"):
        if required not in text:
            errors.append(f"Prepared query catalog module is missing: {required}")

def check_performance_baseline_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime performance baseline module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "performance_baseline.py"
    if not module.is_file():
        errors.append("Performance baseline module is missing")
        return
    text = read_text_cached(module)
    for required in ("record", "get_latest", "compare"):
        if required not in text:
            errors.append(f"Performance baseline module is missing: {required}")

def check_rebuild_certifier_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime rebuild certifier module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "rebuild_certifier.py"
    if not module.is_file():
        errors.append("Rebuild certifier module is missing")
        return
    text = read_text_cached(module)
    for required in ("certify", "is_certified"):
        if required not in text:
            errors.append(f"Rebuild certifier is missing: {required}")

def check_watchdog_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime watchdog module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "watchdog.py"
    if not module.is_file():
        errors.append("Watchdog module is missing")
        return
    text = read_text_cached(module)
    for required in ("check_long_transactions", "collect_bloat_report",
                     "get_rpo_rto_classes", "get_capacity_thresholds"):
        if required not in text:
            errors.append(f"Watchdog module is missing: {required}")

def check_startup_certifier_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime startup certifier module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "startup_certifier.py"
    if not module.is_file():
        errors.append("Startup certifier module is missing")
        return
    text = read_text_cached(module)
    for required in ("certify_startup", "is_ready"):
        if required not in text:
            errors.append(f"Startup certifier is missing: {required}")

def check_readonly_domain_module(root: Path, errors: list[str]) -> None:
    """Verify the runtime read-only domain module exists."""
    module = root / "shared-layer" / "src" / "shared_layer" / "database" / "readonly_domain.py"
    if not module.is_file():
        errors.append("Read-only domain module is missing")
        return
    text = read_text_cached(module)
    for required in ("set_readonly", "is_readonly"):
        if required not in text:
            errors.append(f"Read-only domain module is missing: {required}")
