from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILE_SORTER_ROOT = ROOT / "platform_tools" / "file-sorter"
UI_PATH = FILE_SORTER_ROOT / "src" / "ui" / "FileSorterWindowApp.tsx"
RUNNER_PATH = FILE_SORTER_ROOT / "src" / "ui" / "toolWindowRunner.ts"


def test_destination_ui_only_selects_scanned_direct_child_folders() -> None:
    source = UI_PATH.read_text(encoding="utf-8")

    # The only native folder picker is the target-folder picker. Destination
    # rules must come from the backend's first-level folder scan.
    assert source.count("await selectFolder()") == 1
    assert "chooseKeywordFolder" not in source
    assert "跨磁碟絕對路徑" not in source
    assert "也可以選擇跨磁碟目的地" not in source
    assert 'aria-label="分類目的地（目標內的第一層子資料夾）"' in source
    assert ".filter(isDirectChildFolderName)" in source
    assert "destinationFolderOptions.includes(keywordFolder.trim())" in source
    assert "!hasSelectedDestinationFolder" in source


def test_destination_name_filter_rejects_path_syntax() -> None:
    source = UI_PATH.read_text(encoding="utf-8")

    assert "folderName !== '.'" in source
    assert "folderName !== '..'" in source
    assert "!folderName.includes('/')" in source
    assert "!folderName.includes('\\\\')" in source
    assert "!folderName.includes(':')" in source


def test_legacy_rule_review_is_visible_and_requires_confirmation() -> None:
    source = UI_PATH.read_text(encoding="utf-8")

    assert "migration_required_review?: boolean" in source
    assert "migration_rejected_rule_count?: number" in source
    assert "profileRequiresMigrationReview" in source
    assert "window.confirm(" in source
    assert "舊規則已完整保留" in source
    assert "自動分類維持關閉" in source


def test_automatic_classification_settings_require_backend_connection() -> None:
    source = UI_PATH.read_text(encoding="utf-8")

    assert "const backendConnected = socketStatus === 'Connected'" in source
    assert "const canChangeAutoClassification =" in source
    assert "backendConnected && !actionBusy && targetDir.trim().length > 0" in source
    assert "disabled={!canChangeAutoClassification}" in source
    assert "aria-disabled={!canChangeAutoClassification}" in source
    assert "if (!backendConnected)" in source
    assert "後端連線後才能變更自動分類設定" in source
    assert source.index("if (!backendConnected)") < source.index(
        "profileSelectionVersionRef.current = selectionVersion"
    )


def test_file_sorter_documentation_matches_folder_boundary() -> None:
    readme = (FILE_SORTER_ROOT / "README.md").read_text(encoding="utf-8")
    manifest = json.loads(
        (FILE_SORTER_ROOT / "manifest.json").read_text(encoding="utf-8")
    )

    assert "也可指定已存在資料夾作為外部目的地" not in readme
    assert "跨磁碟搬移" not in readme
    assert "--folder D:\\" not in readme
    assert "--folder E:\\" not in readme
    assert "目標資料夾內既有的第一層子資料夾" in readme
    assert "不接受絕對路徑、多層相對路徑或目標外的資料夾" in readme
    assert "目的地限定為目標內既有的第一層子資料夾" in manifest["description"]
    assert "tests/test_file_sorter_ui.py" in manifest["test_targets"]


def test_platform_tool_csp_allows_the_assigned_loopback_backend_port() -> None:
    html = (ROOT / "src-ui" / "platform-tools" / "entries" / "file-sorter.html").read_text(
        encoding="utf-8"
    )

    assert "connect-src 'self' ws://127.0.0.1:* http://127.0.0.1:*" in html
    assert "ws://127.0.0.1:8765" not in html


def test_typecheck_excludes_packaged_tool_artifacts() -> None:
    config = json.loads((ROOT / "tsconfig.json").read_text(encoding="utf-8"))

    assert "platform_tools/**/build/**" in config["exclude"]
    assert "platform_tools/**/dist/**" in config["exclude"]


def test_cancelled_tool_run_waits_for_backend_release_before_queue_continues() -> None:
    source = RUNNER_PATH.read_text(encoding="utf-8")

    assert "RUN_CANCELLATION_GRACE_MS" in source
    assert "if (queueId && removeQueuedCommand(queueId))" in source
    assert "const cancellation = sendCommand('toolbox_cancel_tool_run'" in source
    assert "cancellationGraceTimer = window.setTimeout" in source
    assert source.index("return await resultPromise") < source.index(
        "queueRef.current = queued.then"
    )
    assert "sendCommand('toolbox_cancel_tool_run'" in source
    assert (
        "sendCommand('toolbox_cancel_tool_run', {" in source
        and "abortController.abort(new Error('請求已撤銷。'))" in source
    )
