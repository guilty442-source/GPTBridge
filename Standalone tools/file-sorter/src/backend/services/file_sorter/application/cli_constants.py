"""Shared constants for file sorter CLI modules."""

from __future__ import annotations

import re
import threading
from pathlib import Path

import os

TOOL_ROOT = Path(__file__).resolve().parents[5]

RULES_VARIABLE_NAME = "KEYWORD_RULES"
PACKAGED_JSON_RULES_FILE_PATH = TOOL_ROOT / "src" / "keyword_rules.json"
_settings_root_override = str(
    os.environ.get("GPTBRIDGE_TOOL_SETTINGS_ROOT") or ""
).strip()
TOOL_SETTINGS_ROOT = (
    Path(_settings_root_override).resolve()
    if _settings_root_override
    else TOOL_ROOT / "runtime" / "settings"
)
try:
    TOOL_SETTINGS_ROOT.relative_to(TOOL_ROOT)
except ValueError as error:
    raise RuntimeError(
        "File Sorter settings must stay inside its own tool folder"
    ) from error
JSON_RULES_FILE_PATH = TOOL_SETTINGS_ROOT / "keyword_rules.json"
PY_RULES_FILE_PATH = TOOL_ROOT / "src" / "keyword_rules.py"
RULES_FILE_PATH = JSON_RULES_FILE_PATH
LEGACY_RULES_FILE_NAME = ".file-sorter-rules.json"
LEGACY_RULES_QUARANTINE_PREFIX = "legacy-rules-quarantine-"
LEGACY_RULES_MIGRATION_INBOX_DIR = (
    TOOL_ROOT / "runtime" / "legacy-rule-migration-inbox"
)
LEGACY_RULES_MIGRATION_INBOX_PATTERN = re.compile(
    r"^(?P<digest>[0-9a-f]{64})-"
    r"(?P<source>keyword_rules\.json|keyword_rules\.py|\.file-sorter-rules\.json)$"
)
LEGACY_RULES_MIGRATION_MAX_BYTES = 8 * 1024 * 1024
FOLDERS_JSON_PREFIX = "FILE_SORTER_FOLDERS_JSON="
SOURCE_FILES_JSON_PREFIX = "FILE_SORTER_SOURCE_FILES_JSON="
_BACKGROUND_OBSERVATIONS_LOCK = threading.Lock()
_BACKGROUND_OBSERVATIONS: dict[
    tuple[str, str],
    dict[str, tuple[int, int]],
] = {}
_BACKGROUND_DUPLICATE_OBSERVATIONS: dict[
    tuple[str, str],
    dict[str, tuple[int, int, str, str]],
] = {}
