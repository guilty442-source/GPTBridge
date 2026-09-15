"""file-sorter test module (A57/E43)

source: main-system/tests/test_file_sorter_ui.py
"""
from __future__ import annotations

import _sorter_test_boot  # noqa: F401  # sys.path bootstrap

from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


def test_ui_exposes_explicit_safe_automation_controls() -> None:
    source = (
        WORKSPACE_ROOT / "Standalone tools" / "file-sorter" / "src" / "ui" / "FileSorterWindowApp.tsx"
    ).read_text(encoding="utf-8")
    assert "自動將完全重複檔移至 Windows 資源回收筒" in source
    assert "MiniCPM-V 4.6" in source
    assert "gptbridge.file-sorter.last-target-dir.v1" in source
    assert "invoke?.('dialog:validate-folder', savedTarget)" in source
    assert "const [targetDir, setTargetDir] = useState('')" in source
    assert "const [targetValidated, setTargetValidated] = useState(false)" in source
    assert "localStorage.removeItem(LAST_TARGET_DIR_STORAGE_KEY)" in source
    source_host = (
        WORKSPACE_ROOT
        / "main-system"
        / "scripts"
        / "source-tool-ui-host"
        / "main.cjs"
    ).read_text(encoding="utf-8")
    source_preload = (
        WORKSPACE_ROOT
        / "main-system"
        / "scripts"
        / "source-tool-ui-host"
        / "preload.cjs"
    ).read_text(encoding="utf-8")
    assert "ipcMain.handle('dialog:validate-folder'" in source_host
    assert "'dialog:validate-folder'" in source_preload
    assert "window.localStorage.setItem(LAST_TARGET_DIR_STORAGE_KEY, value)" in source
    assert "if (!backendConnected)" in source
    assert "scanDestinationFolders(target, abortController.signal)" in source
    assert "timeoutMs: FOLDER_SCAN_TIMEOUT_MS" in source
    assert "folderScanGenerationRef.current += 1" in source
    assert "style={{ display: 'none' }}" not in source
    assert "setProfileEnabled, 'true'" not in source
    assert "'--cleanup-scan', '--json', '--progress-jsonl'" not in source
    assert "result.cancelled || cleanupStopRequestedRef.current" in source


def test_file_sorter_ui_is_in_main_typecheck_scope() -> None:
    tsconfig = (WORKSPACE_ROOT / "main-system" / "tsconfig.json").read_text(
        encoding="utf-8"
    )
    assert "../file-sorter/src/ui/**/*.tsx" in tsconfig
