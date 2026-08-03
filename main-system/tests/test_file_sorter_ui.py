from __future__ import annotations

from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def test_ui_exposes_explicit_safe_automation_controls() -> None:
    source = (
        WORKSPACE_ROOT / "file-sorter" / "src" / "ui" / "FileSorterWindowApp.tsx"
    ).read_text(encoding="utf-8")
    assert "自動將完全重複檔移至 Windows 資源回收筒" in source
    assert "MiniCPM-V 4.6" in source
    assert "gptbridge.file-sorter.last-target-dir.v1" in source
    assert "useState(loadLastTargetDir)" in source
    assert "window.localStorage.setItem(LAST_TARGET_DIR_STORAGE_KEY, value)" in source
    assert "style={{ display: 'none' }}" not in source
    assert "setProfileEnabled, 'true'" not in source
    assert "'--cleanup-scan', '--json', '--progress-jsonl'" not in source
    assert "result.cancelled || cleanupStopRequestedRef.current" in source


def test_file_sorter_ui_is_in_main_typecheck_scope() -> None:
    tsconfig = (WORKSPACE_ROOT / "main-system" / "tsconfig.json").read_text(
        encoding="utf-8"
    )
    assert "../file-sorter/src/ui/**/*.tsx" in tsconfig
