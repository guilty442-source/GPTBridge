"""Collection generations — embedding upgrades are zero-downtime
shadow builds, never in-place vector overwrites.

    gen-001 (qwen3-embedding:4b, 2560d)  ACTIVE
    gen-002 (new-model, new-dim)         BUILDING
        -> benchmark -> consistency validation -> alias swap
        -> gen-002 ACTIVE, gen-001 RETIRED

A generation fingerprint covers *everything* that changes index
content — parser, chunk policy, embedding, vector schema — so a
chunk-policy change is a new generation too, and identical
fingerprints may safely reuse an index.
"""
from __future__ import annotations

import hashlib
import itertools
import threading
from dataclasses import dataclass, field
from enum import Enum


class GenerationState(str, Enum):
    BUILDING = "BUILDING"
    VALIDATING = "VALIDATING"
    SHADOW = "SHADOW"          # built; shadow-query comparison running
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    FAILED = "FAILED"


_TRANSITIONS: dict[GenerationState, frozenset[GenerationState]] = {
    GenerationState.BUILDING: frozenset({GenerationState.VALIDATING, GenerationState.FAILED}),
    GenerationState.VALIDATING: frozenset({GenerationState.SHADOW, GenerationState.ACTIVE, GenerationState.FAILED}),
    GenerationState.SHADOW: frozenset({GenerationState.ACTIVE, GenerationState.FAILED}),
    GenerationState.ACTIVE: frozenset({GenerationState.RETIRED}),
    GenerationState.RETIRED: frozenset(),
    GenerationState.FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class GenerationFingerprint:
    """hash(parser + chunk policy + embedding + vector schema)."""

    parser_version: str
    chunk_policy_version: str
    embedding_model: str
    embedding_dimension: int
    vector_schema_version: int

    def digest(self) -> str:
        raw = "|".join((
            self.parser_version,
            self.chunk_policy_version,
            self.embedding_model,
            str(self.embedding_dimension),
            str(self.vector_schema_version),
        ))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class CollectionGeneration:
    generation_id: str
    logical_alias: str
    physical_name: str
    fingerprint: str
    embedding_model: str
    embedding_dimension: int
    state: GenerationState = GenerationState.BUILDING
    initial_points: int = 0
    error: str = ""


class GenerationManager:
    """Manages shadow builds + alias swap; never mutates ACTIVE."""

    def __init__(self, logical_alias: str = "gptbridge_shared_knowledge") -> None:
        self._lock = threading.Lock()
        self._alias = logical_alias
        self._counter = itertools.count(1)
        self._generations: dict[str, CollectionGeneration] = {}

    def create_generation(
        self, fingerprint: GenerationFingerprint, *, initial_points: int = 0
    ) -> CollectionGeneration:
        """Start a shadow build — the ACTIVE generation keeps
        serving queries untouched."""
        with self._lock:
            n = next(self._counter)
            gid = f"gen-{n:03d}"
            gen = CollectionGeneration(
                generation_id=gid,
                logical_alias=self._alias,
                physical_name=f"{self._alias}_g{n}",
                fingerprint=fingerprint.digest(),
                embedding_model=fingerprint.embedding_model,
                embedding_dimension=fingerprint.embedding_dimension,
                initial_points=initial_points,
            )
            self._generations[gid] = gen
            return gen

    def find_reusable(self, fingerprint: GenerationFingerprint) -> CollectionGeneration | None:
        """Identical fingerprint -> existing index may be reused."""
        digest = fingerprint.digest()
        for g in self._generations.values():
            if g.fingerprint == digest and g.state in (
                GenerationState.ACTIVE, GenerationState.SHADOW,
            ):
                return g
        return None

    def _transition(self, generation_id: str, target: GenerationState) -> CollectionGeneration:
        import dataclasses
        with self._lock:
            gen = self._generations[generation_id]
            if target not in _TRANSITIONS[gen.state]:
                raise ValueError(f"illegal {gen.state}->{target} for {generation_id}")
            gen = dataclasses.replace(gen, state=target)
            self._generations[generation_id] = gen
            return gen

    def mark_validating(self, generation_id: str) -> CollectionGeneration:
        return self._transition(generation_id, GenerationState.VALIDATING)

    def mark_shadow(self, generation_id: str) -> CollectionGeneration:
        return self._transition(generation_id, GenerationState.SHADOW)

    def mark_failed(self, generation_id: str, error: str = "") -> CollectionGeneration:
        gen = self._transition(generation_id, GenerationState.FAILED)
        if error:
            import dataclasses
            with self._lock:
                gen = dataclasses.replace(gen, error=error)
                self._generations[generation_id] = gen
        return gen

    def activate(self, generation_id: str) -> CollectionGeneration:
        """Alias swap: new gen ACTIVE, every other ACTIVE retired."""
        gen = self._generations[generation_id]
        if gen.state not in (GenerationState.VALIDATING, GenerationState.SHADOW):
            raise ValueError("only VALIDATING/SHADOW generations can activate")
        with self._lock:
            for other in list(self._generations.values()):
                if other.state is GenerationState.ACTIVE and other.generation_id != generation_id:
                    import dataclasses
                    self._generations[other.generation_id] = dataclasses.replace(
                        other, state=GenerationState.RETIRED
                    )
        return self._transition(generation_id, GenerationState.ACTIVE)

    def active(self) -> CollectionGeneration | None:
        for g in self._generations.values():
            if g.state is GenerationState.ACTIVE:
                return g
        return None

    def get(self, generation_id: str) -> CollectionGeneration | None:
        return self._generations.get(generation_id)


__all__ = [
    "CollectionGeneration",
    "GenerationFingerprint",
    "GenerationManager",
    "GenerationState",
]
