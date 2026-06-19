import os
import sys
import importlib.util
from pathlib import Path


sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

ROOT = Path(__file__).resolve().parents[1]
CLEANUP_SERVICE_PATH = (
    ROOT
    / "platform_tools"
    / "project-cleaner"
    / "src"
    / "backend"
    / "cleanup_service.py"
)
SPEC = importlib.util.spec_from_file_location(
    "project_cleaner_cleanup_service",
    CLEANUP_SERVICE_PATH,
)
assert SPEC and SPEC.loader
cleanup_service_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup_service_module
SPEC.loader.exec_module(cleanup_service_module)
ProjectCleanupService = cleanup_service_module.ProjectCleanupService


def test_project_cleanup_skips_dependency_and_profile_dirs(tmp_path: Path) -> None:
    dependency_cache = (
        tmp_path
        / "release"
        / "win-unpacked"
        / "resources"
        / ".venv"
        / "cache"
    )
    dependency_cache.mkdir(parents=True)
    dependency_file = dependency_cache / "keep.tmp"
    dependency_file.write_text("keep", encoding="utf-8")

    profile_cache = tmp_path / "runtime" / "profiles" / "main" / "cache"
    profile_cache.mkdir(parents=True)
    profile_file = profile_cache / "keep.tmp"
    profile_file.write_text("keep", encoding="utf-8")

    log_dir = tmp_path / "release" / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "delete.log"
    log_file.write_text("delete", encoding="utf-8")

    service = ProjectCleanupService(tmp_path)
    result = service.cleanup_garbage("project", dry_run=False)

    assert result["ok"] is True
    assert result["cleaned_dirs"] >= 1
    assert dependency_file.exists()
    assert profile_file.exists()
    assert not log_dir.exists()


def test_global_cleanup_scans_whole_project_but_skips_protected_dirs(
    tmp_path: Path,
) -> None:
    pycache_dir = tmp_path / "platform_tools" / "project-cleaner" / "src" / "__pycache__"
    pycache_dir.mkdir(parents=True)
    pycache_file = pycache_dir / "main.pyc"
    pycache_file.write_bytes(b"delete")

    src_cache = tmp_path / "src-core" / "tasks" / "cache"
    src_cache.mkdir(parents=True)
    src_cache_file = src_cache / "work.tmp"
    src_cache_file.write_text("delete", encoding="utf-8")

    source_file = tmp_path / "src-core" / "tasks" / "service.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("keep", encoding="utf-8")

    dependency_cache = tmp_path / "node_modules" / "package" / "cache"
    dependency_cache.mkdir(parents=True)
    dependency_file = dependency_cache / "keep.tmp"
    dependency_file.write_text("keep", encoding="utf-8")

    profile_cache = tmp_path / "edge-profile" / "main" / "cache"
    profile_cache.mkdir(parents=True)
    profile_file = profile_cache / "keep.tmp"
    profile_file.write_text("keep", encoding="utf-8")

    service = ProjectCleanupService(tmp_path)
    result = service.cleanup_garbage("global", dry_run=False)

    assert result["ok"] is True
    assert result["scope"] == "global"
    assert not pycache_dir.exists()
    assert not src_cache.exists()
    assert source_file.exists()
    assert dependency_file.exists()
    assert profile_file.exists()


def test_cleanup_rules_live_in_project_cleaner_backend() -> None:
    core_source = (ROOT / "src-core" / "settings" / "service.py").read_text(
        encoding="utf-8"
    )
    ipc_source = (ROOT / "src-core" / "ipc" / "server.py").read_text(
        encoding="utf-8"
    )
    cleaner_source = CLEANUP_SERVICE_PATH.read_text(encoding="utf-8")

    assert "settings_cleanup_garbage" not in core_source
    assert "settings_cleanup_garbage" not in ipc_source
    assert "ProjectCleanupService" not in core_source
    assert "removable_names" not in core_source
    assert "removable_suffixes" not in core_source
    assert "ProjectCleanupService" in cleaner_source
    assert "removable_names" in cleaner_source
