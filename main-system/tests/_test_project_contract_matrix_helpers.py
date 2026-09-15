"""Shared test helpers (split from consolidated suite)."""
from __future__ import annotations

import _main_system_test_support as _s  # noqa: F401
from _main_system_test_support import ROOT, _read_text_cached, _parse_python_cached

import ast
import json
import re
import subprocess
from functools import cache
from pathlib import Path
from typing import Any
import pytest
from governance_rule.code_rule_directory import (  # noqa: E402
    code_rule_directory_snapshot,
)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(_read_text_cached(str(path)))
    assert isinstance(payload, dict)
    return payload
