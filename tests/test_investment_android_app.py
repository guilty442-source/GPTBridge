from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_APP_ROOT = PROJECT_ROOT / "mobile" / "investment-manager-native"


def test_android_native_app_has_installable_build_contract() -> None:
    package = json.loads(
        (ANDROID_APP_ROOT / "package.json").read_text(encoding="utf-8")
    )
    app = json.loads((ANDROID_APP_ROOT / "app.json").read_text(encoding="utf-8"))[
        "expo"
    ]
    eas = json.loads((ANDROID_APP_ROOT / "eas.json").read_text(encoding="utf-8"))

    assert package["dependencies"]["react-native"]
    assert package["dependencies"]["expo-secure-store"]
    assert package["scripts"]["android"] == "expo run:android"
    assert package["version"] == "1.0.0"
    assert app["version"] == "1.0.0"
    assert app["android"]["package"] == "com.gptbridge.investmentmanager"
    assert app["android"]["versionCode"] == 1
    assert app["platforms"] == ["android"]
    assert "ios" not in app
    assert eas["build"]["preview"]["android"]["buildType"] == "apk"


def test_android_native_app_uses_shared_api_without_browser_or_webview() -> None:
    app_source = (ANDROID_APP_ROOT / "App.tsx").read_text(encoding="utf-8")
    client_source = (
        ANDROID_APP_ROOT / "src" / "api" / "investmentClient.ts"
    ).read_text(encoding="utf-8")
    hook_source = (
        ANDROID_APP_ROOT / "src" / "hooks" / "useInvestmentPlatform.ts"
    ).read_text(encoding="utf-8")
    secure_source = (
        ANDROID_APP_ROOT / "src" / "security" / "sessionStore.ts"
    ).read_text(encoding="utf-8")

    assert "from 'react-native'" in app_source
    assert "WebView" not in app_source
    assert "window." not in app_source
    assert "/api/platform" in client_source
    assert "/api/state" in client_source
    assert "/api/local-ai-command" in client_source
    assert "android_native" in client_source
    assert "ios_native" not in client_source
    assert "setInterval" in hook_source
    assert "2_000" in hook_source
    assert "SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY" in secure_source


def test_android_native_app_keeps_high_risk_mutations_on_desktop() -> None:
    contract_source = (
        PROJECT_ROOT
        / "platform_tools"
        / "ai-assistant"
        / "src"
        / "backend"
        / "services"
        / "ai_nexus"
        / "mobile_sync.py"
    ).read_text(encoding="utf-8")

    assert '"mobile_write_scope": "queue_local_ai_command_only"' in contract_source
    assert '"foreground_sync_seconds": 2' in contract_source
    assert '"upgrade_compatibility": upgrade_compatibility_contract()' in contract_source
    assert "portfolio_overview" in contract_source
    assert "holdings_read" in contract_source
    assert "local_ai_command_queue" in contract_source
