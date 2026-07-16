from typing import Any, Dict
from .rules.no_placeholder_pollution import NoPlaceholderPollutionRule
from .rules.gemini_code_assist_lockdown import GeminiCodeAssistLockdownRule
from .rules.high_risk_module_protection import HighRiskModuleProtectionRule
from .rules.path_scope_guard import PathScopeGuardRule
from .rules.system_boundary_guard import SystemBoundaryGuardRule
from .rules.import_governance_guard import ImportGovernanceGuard
from .rules.structure_modularity_guard import StructureModularityGuard

class RulesEngine:
    """Load and execute concrete governance rules."""
    def __init__(self, project_root):
        self.rules = [
            GeminiCodeAssistLockdownRule(),
            NoPlaceholderPollutionRule(),
            SystemBoundaryGuardRule(project_root),
            HighRiskModuleProtectionRule(project_root),
            PathScopeGuardRule(project_root),
            ImportGovernanceGuard(),
            StructureModularityGuard()
        ]

    def evaluate(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate rules in order and stop at the first denied operation."""
        for rule in self.rules:
            if not rule.evaluate(operation):
                return {
                    "allowed": False,
                    "rule_id": rule.rule_id,
                    "reason": rule.reason
                }
        
        return {"allowed": True}

class BaseGovernanceRule:
    """Base contract for executable governance rules."""
    rule_id = "base_rule"
    reason = "default_reason"

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        """Return ``True`` to allow or ``False`` to block the operation."""
        raise NotImplementedError("governance rules must implement executable logic")
