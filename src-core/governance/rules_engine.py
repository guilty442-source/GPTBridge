from typing import Any, Dict
from .rules.no_placeholder_pollution import NoPlaceholderPollutionRule
from .rules.gemini_code_assist_lockdown import GeminiCodeAssistLockdownRule
from .rules.high_risk_module_protection import HighRiskModuleProtectionRule
from .rules.path_scope_guard import PathScopeGuardRule
from .rules.import_governance_guard import ImportGovernanceGuard
from .rules.structure_modularity_guard import StructureModularityGuard

class RulesEngine:
    """
    治理規則引擎，負責載入並執行具體規則。
    """
    def __init__(self):
        # 註冊所有真實可用的規則
        self.rules = [
            GeminiCodeAssistLockdownRule(),
            NoPlaceholderPollutionRule(),
            HighRiskModuleProtectionRule(),
            PathScopeGuardRule(),
            ImportGovernanceGuard(),
            StructureModularityGuard()
        ]

    def evaluate(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        """
        依序評估所有規則。任一規則不通過即視為 Blocked。
        """
        for rule in self.rules:
            if not rule.evaluate(operation):
                return {
                    "allowed": False,
                    "rule_id": rule.rule_id,
                    "reason": rule.reason
                }
        
        return {"allowed": True}

class BaseGovernanceRule:
    """治理規則基底類別"""
    rule_id = "base_rule"
    reason = "default_reason"

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        """
        子類別必須實作此邏輯，返回 True 代表允許，False 代表阻擋。
        """
        raise NotImplementedError("治理規則必須實作實體邏輯，禁止使用 pass")
