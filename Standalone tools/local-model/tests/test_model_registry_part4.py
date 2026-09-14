"""Split from consolidated test_xingcheng.py (local-model/tests/test_model_registry.py)."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401
from _xingcheng_test_support import ROOT
from _test_model_registry_helpers import _make_service, _make_transformer_service

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any
import pytest
from xingcheng.domain.model_registry import StarModelRegistry
from xingcheng.domain.module_registry import StarModuleRegistry
from xingcheng.integration.memory_broker import StarMemoryBroker
from xingcheng.infrastructure.repository import LocalAiRepository
from xingcheng.infrastructure.ollama_model_repository import OllamaModelRepository
from xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime
from xingcheng.infrastructure.model_engines import StarModelEngines
from xingcheng.infrastructure.generative_language_model import (
    StarAutoregressiveLanguageModel,
)
from xingcheng.infrastructure.native_model import StarNativeLanguageModel
from xingcheng.infrastructure import repository as repository_module
from xingcheng.application.service import LocalAiService
from xingcheng.infrastructure.market_data import (
    MarketDataSearch,
    _fund_query_terms,
    _yahoo_symbol_candidates,
    market_source_catalog,
    recognize_holding_identity,
)
from xingcheng.application.investment_accounting import coordinate_investment_accounting
from xingcheng.application.investment_analysis import ANALYSIS_MODEL_KEYS, analyze_investments
from xingcheng.application.coding_expert import StarCodingExpert

def test_capability_composition_is_owned_only_by_star_main_database(
    tmp_path: Path,
) -> None:
    main = LocalAiRepository(tmp_path, database_scope="main")
    coding = LocalAiRepository(tmp_path, database_scope="coding")
    composition = {
        "composition_id": "composition-test-1",
        "status": "approved",
        "implementation_target": "xingcheng/src/example.py",
        "model_assignments": {"implementation": "gpt-oss:20b"},
        "model_discussion": {"voters": [], "inspection_results": []},
    }

    stored = main.store_capability_composition(composition)

    assert stored["owner_model_id"] == "star-main-native-model"
    assert main.database_status()["tables"]["capability_composition"] == 1
    assert coding.database_status()["tables"]["capability_composition"] == 0
    with pytest.raises(PermissionError, match="MODEL_DATABASE_ISOLATION_DENIED"):
        coding.store_capability_composition(composition)



########################################################################
