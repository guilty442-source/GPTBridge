"""NER Type Definitions - Core data structures for Named Entity Recognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EntityType(Enum):
    """Supported entity types for NER extraction."""
    PERSON = "PERSON"
    LOCATION = "LOCATION"
    ORGANIZATION = "ORGANIZATION"
    DATETIME = "DATETIME"
    MONEY = "MONEY"
    MODEL_NAME = "MODEL_NAME"
    PRODUCT = "PRODUCT"
    EVENT = "EVENT"
    LAW = "LAW"
    LANGUAGE = "LANGUAGE"
    PERCENT = "PERCENT"
    QUANTITY = "QUANTITY"
    ORDINAL = "ORDINAL"
    CARDINAL = "CARDINAL"
    NORP = "NORP"
    FACILITY = "FACILITY"
    GPE = "GPE"
    WORK_OF_ART = "WORK_OF_ART"
    CUSTOM = "CUSTOM"


@dataclass(frozen=True)
class Entity:
    """Represents a single named entity with metadata."""
    text: str
    entity_type: EntityType
    start_char: int
    end_char: int
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    children: tuple[Entity, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        if self.start_char < 0 or self.end_char < 0:
            raise ValueError("start_char and end_char must be non-negative")
        if self.start_char > self.end_char:
            raise ValueError("start_char must be <= end_char")

    @property
    def length(self) -> int:
        return self.end_char - self.start_char

    @property
    def has_children(self) -> bool:
        return len(self.children) > 0

    def get_nested_entities(self) -> list[Entity]:
        """Get all nested entities recursively."""
        result = list(self.children)
        for child in self.children:
            result.extend(child.get_nested_entities())
        return result

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "text": self.text,
            "type": self.entity_type.value,
            "start": self.start_char,
            "end": self.end_char,
            "confidence": self.confidence,
            "metadata": self.metadata,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        """Create Entity from dictionary."""
        children = tuple(
            cls.from_dict(c) for c in data.get("children", [])
        )
        return cls(
            text=data["text"],
            entity_type=EntityType(data["type"]),
            start_char=data["start"],
            end_char=data["end"],
            confidence=data.get("confidence", 1.0),
            metadata=data.get("metadata", {}),
            children=children,
        )


@dataclass
class ExtractionResult:
    """Result of NER extraction containing all entities found."""
    text: str
    entities: tuple[Entity, ...]
    processing_time_ms: float = 0.0
    model_used: Optional[str] = None

    @property
    def entity_count(self) -> int:
        return len(self.entities)

    def get_entities_by_type(self, entity_type: EntityType) -> list[Entity]:
        """Get all entities of a specific type (including nested)."""
        result = []
        for entity in self.entities:
            if entity.entity_type == entity_type:
                result.append(entity)
            result.extend(
                e for e in entity.get_nested_entities() if e.entity_type == entity_type
            )
        return result

    def get_top_level_entities(self) -> list[Entity]:
        """Get only top-level entities (no parent)."""
        return list(self.entities)

    def get_all_entities_flat(self) -> list[Entity]:
        """Get all entities flattened (including nested)."""
        result = list(self.entities)
        for entity in self.entities:
            result.extend(entity.get_nested_entities())
        return result

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "text": self.text,
            "entities": [e.to_dict() for e in self.entities],
            "processing_time_ms": self.processing_time_ms,
            "model_used": self.model_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtractionResult:
        """Create ExtractionResult from dictionary."""
        entities = tuple(Entity.from_dict(e) for e in data.get("entities", []))
        return cls(
            text=data["text"],
            entities=entities,
            processing_time_ms=data.get("processing_time_ms", 0.0),
            model_used=data.get("model_used"),
        )