import logging
from typing import Any, Dict
from .rules_engine import RulesEngine
from pathlib import Path

class GovernanceEnforcer:
    """
    GPTBridge Runtime Governance Enforcement Layer.
    負責攔截所有系統操作並進行強制性驗證。
    """
    def __init__(self, project_root: Path, logger: Any | None = None):
        self.engine = RulesEngine()
        self.project_root = project_root
        self.logger = logger or logging.getLogger("GPTBridge.Governance")

    def validate_operation(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        """
        核心驗證入口。所有操作必須通過此方法。
        operation 範例:
        {
            "actor": "codex",
            "action": "delete_file",
            "target": "src-core/providers/chatgpt.py",
            "reason": "cleanup",
            "content": "..." # 選填，用於檢查內容污染
        }
        """
        actor = operation.get("actor", "unknown")
        action = operation.get("action", "unknown")
        target = operation.get("target", "unknown")

        # 執行規則檢查
        result = self.engine.evaluate(operation)

        if not result["allowed"]:
            self.logger.warning(
                f"治理攔截: [rule_id: {result['rule_id']}] "
                f"Actor: {actor}, Action: {action}, Target: {target}, Root Cause: {result['reason']}"
            )
            return {
                "allowed": False,
                "rule_id": result["rule_id"],
                "reason": result["reason"],
                "error_report": {
                    "root_cause": result["reason"],
                    "affected_files": [target],
                    "auto_fix_result": result.get("auto_fix_result", "Manual intervention required")
                },
                "severity": "blocked"
            }

        return {
            "allowed": True,
            "rule_id": None,
            "reason": "validation_passed",
            "severity": "info"
        }

    # Helper 方法實現
    def can_create_file(self, path: str, actor: str, reason: str, content: str = "") -> Dict[str, Any]:
        op = {"actor": actor, "action": "create_file", "target": path, "reason": reason, "content": content}
        return self.validate_operation(op)

    def can_create_folder(self, path: str, actor: str, reason: str) -> Dict[str, Any]:
        op = {"actor": actor, "action": "create_folder", "target": path, "reason": reason}
        return self.validate_operation(op)

    def can_modify_file(self, path: str, actor: str, reason: str, content: str = "") -> Dict[str, Any]:
        op = {"actor": actor, "action": "modify_file", "target": path, "reason": reason, "content": content}
        return self.validate_operation(op)

    def can_delete_file(self, path: str, actor: str, reason: str) -> Dict[str, Any]:
        op = {"actor": actor, "action": "delete_file", "target": path, "reason": reason}
        return self.validate_operation(op)

    def can_move_file(self, src: str, dst: str, actor: str, reason: str) -> Dict[str, Any]:
        op = {"actor": actor, "action": "move_file", "target": src, "destination": dst, "reason": reason}
        return self.validate_operation(op)

    def can_modify_module(self, module: str, actor: str, reason: str) -> Dict[str, Any]:
        op = {"actor": actor, "action": "modify_module", "target": module, "reason": reason}
        return self.validate_operation(op)

    def can_modify_workflow(self, workflow: str, actor: str, reason: str) -> Dict[str, Any]:
        op = {"actor": actor, "action": "modify_workflow", "target": workflow, "reason": reason}
        return self.validate_operation(op)

# 單例模式供全域使用
enforcer = GovernanceEnforcer(Path(__file__).resolve().parents[2]) # Adjust path to project root

def validate_operation(operation: Dict[str, Any]) -> Dict[str, Any]:
    """全域入口函數"""
    return enforcer.validate_operation(operation)

def check_permission(action: str, target: str, actor: str, reason: str, content: str = "") -> bool:
    """輕量化權限檢查"""
    return enforcer.validate_operation({
        "actor": actor,
        "action": action,
        "target": target,
        "reason": reason,
        "content": content
    })["allowed"]
