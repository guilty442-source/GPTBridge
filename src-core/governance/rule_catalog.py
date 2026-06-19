from __future__ import annotations

from typing import Final

# Central catalog for every governance rule/checker id shown in Developer Mode.
GOVERNANCE_RULE_CATALOG: Final[list[str]] = [
    # UI governance rules
    "rule_ui_001",
    "rule_ui_002",
    "rule_ui_003",
    "rule_ui_004",
    "rule_ui_005",
    "rule_ui_006",
    # Layer / architecture governance rules
    "rule_layer_001",
    "rule_layer_002",
    "rule_layer_003",
    # Security / naming / auth governance rules
    "rule_security_001",
    "rule_security_002",
    "rule_naming_001",
    "rule_auth_001",
    # Runtime governance rules
    "gemini_code_assist_lockdown",
    "no_placeholder_pollution",
    "high_risk_module_protection",
    "path_scope_guard",
    "import_governance_guard",
    "structure_modularity_guard",
    # TypeScript governance checker IDs
    "G-ALIAS-001",
    "G-MODULE-001",
    "G-I18N-001",
    "G-115",
    "G-RUNTIME-001",
    "G-BROWSER-001",
    "G-W11-001",
    "G-SEC-002",
    "G-UI-APP-001",
    "G-UI-GOV-001",
    "G-HMR-GLOBAL-001",
]

# Governance rules are global: every catalog rule is active by default.
DEFAULT_ACTIVE_GOVERNANCE_RULES: Final[list[str]] = list(GOVERNANCE_RULE_CATALOG)
