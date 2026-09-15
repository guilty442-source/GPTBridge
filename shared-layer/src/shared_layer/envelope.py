"""Governed Command Envelope — A177 information-layer transport contract.

Typed envelope for cross-owner commands through the universal gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GovernedCommandEnvelope:
    """Immutable command envelope for the A177 gate pipeline."""

    sender: str
    destination: str
    command: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.sender or not isinstance(self.sender, str):
            raise ValueError("sender must be non-empty string")
        if not self.destination or not isinstance(self.destination, str):
            raise ValueError("destination must be non-empty string")
        if not self.command or not isinstance(self.command, str):
            raise ValueError("command must be non-empty string")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be dict")


__all__ = ["GovernedCommandEnvelope"]