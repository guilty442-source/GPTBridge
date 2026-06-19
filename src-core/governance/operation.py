from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal

OperationType = Literal[
    "create_file",
    "create_folder",
    "modify_file",
    "delete_file",
    "move_file",
    "modify_module",
    "modify_workflow",
    "validate",
]

@dataclass(frozen=True)
class GovernanceOperation:
    actor: str
    action: OperationType
    target: str
    reason: str
    content: str = ""
    destination: str | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "reason": self.reason,
            "content": self.content,
        }
        if self.destination is not None:
            payload["destination"] = self.destination
        payload.update(self.metadata)
        return payload
