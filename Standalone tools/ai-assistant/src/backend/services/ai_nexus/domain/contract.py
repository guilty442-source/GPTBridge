"""星澄 AI 投資管理與自動操盤系統 — business-layer contract.

ai-assistant owns the business layer and canonical records (signals,
orders, fills mirror, authorizations, domain state). The trading engine
cluster lives in the investment-mobile tool boundary; records reach this
store through the governed ``xingcheng_mobile_*`` channel envelopes.
"""

from __future__ import annotations

import json
from pathlib import Path


def _trading_app_version() -> str:
    try:
        from shared_layer.registry.versioning import component_version

        return component_version("ai-assistant")
    except Exception:
        pass
    manifest_path = Path(__file__).resolve().parents[5] / "manifest.json"
    try:
        version = str(
            json.loads(manifest_path.read_text("utf-8")).get("version", "")
        ).strip()
        if version:
            return version
    except Exception:
        pass
    return "1.0.0"


TRADING_APP_VERSION = _trading_app_version()
TRADING_SCHEMA_VERSION = 1

# The six core business domains of the rebuilt system.
DOMAIN_TW_STOCK = "tw-stock"
DOMAIN_US_STOCK = "us-stock"
DOMAIN_FUND = "fund"
DOMAIN_AI_ANALYSIS = "ai-analysis"
DOMAIN_AUTO_TRADING = "auto-trading"
DOMAIN_ASSET_MGMT = "asset-management"

DOMAIN_IDS = (
    DOMAIN_TW_STOCK,
    DOMAIN_US_STOCK,
    DOMAIN_FUND,
    DOMAIN_AI_ANALYSIS,
    DOMAIN_AUTO_TRADING,
    DOMAIN_ASSET_MGMT,
)

# Market → domain routing shared by domains and the store.
MARKET_DOMAIN = {
    "tw": DOMAIN_TW_STOCK,
    "us": DOMAIN_US_STOCK,
    "fund": DOMAIN_FUND,
}
