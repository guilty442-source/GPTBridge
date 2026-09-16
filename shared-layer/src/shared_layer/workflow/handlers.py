"""Step handler registry: bind plan steps to injected handlers.

A step plan declares *what* must run (engine + action type) and never embeds
business logic.  This module binds every step to a handler supplied by the
caller — either a ``StepHandler`` protocol object or a plain callable — and
leaves unbound steps unbound so the executor's ``NO_HANDLER`` fail-closed
path still applies.  No handler behaviour is invented here.

Resolution order for a step is: exact ``step_id`` binding, then
``action_type`` binding, then ``engine`` binding.  A registry itself acts as
a ``StepHandlerResolver`` and can be handed to ``SagaExecutor.run``; the
legacy ``dict[Engine, StepHandler]`` form keeps working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from .operation import Operation
from .saga import StepHandler
from .steps import StepPlan, StepResult, StepSpec
from .types import Engine, StepStatus


class NoHandlerError(RuntimeError):
    """Raised when a plan step has no bound handler (fail-closed)."""

    def __init__(self, engine: Engine, step_id: str) -> None:
        super().__init__(f"NO_HANDLER:{engine.value}")
        self.engine = engine
        self.step_id = step_id


@dataclass
class CallableStepHandler:
    """Adapt plain callables to the ``StepHandler`` protocol.

    ``perform`` is required.  ``verify`` defaults to "not observed" — the
    executor must never claim an effect the caller did not confirm — and
    ``compensate`` defaults to fail-closed: an accidental compensation can
    never silently succeed.  A ``perform``/``compensate`` callable returning
    ``None`` means "completed" (perform) or "compensated" (compensate).
    """

    perform_callable: Callable[[Operation, StepSpec], StepResult | None]
    verify_callable: Callable[[Operation, StepSpec], bool] | None = None
    compensate_callable: Callable[[Operation, StepSpec, StepResult], StepResult | None] | None = None

    def perform(self, operation: Operation, step: StepSpec) -> StepResult:
        result = self.perform_callable(operation, step)
        if result is None:
            return StepResult(step.step_id, StepStatus.COMPLETED.value)
        return result

    def verify(self, operation: Operation, step: StepSpec) -> bool:
        if self.verify_callable is None:
            return False
        return bool(self.verify_callable(operation, step))

    def compensate(
        self, operation: Operation, step: StepSpec, result: StepResult
    ) -> StepResult:
        if self.compensate_callable is None:
            raise NotImplementedError("COMPENSATION_HANDLER_REQUIRED")
        compensated = self.compensate_callable(operation, step, result)
        if compensated is None:
            return StepResult(step.step_id, StepStatus.COMPENSATED.value)
        return compensated


@dataclass
class StepHandlerRegistry:
    """Deterministic step -> handler bindings for one or more plans."""

    _by_step: dict[str, StepHandler] = field(default_factory=dict)
    _by_action: dict[str, StepHandler] = field(default_factory=dict)
    _by_engine: dict[Engine, StepHandler] = field(default_factory=dict)

    # -- binding -----------------------------------------------------------

    def register_engine(
        self,
        engine: Engine,
        handler: StepHandler | Callable[..., StepResult | None],
        *,
        verify: Callable[[Operation, StepSpec], bool] | None = None,
        compensate: Callable[..., StepResult | None] | None = None,
    ) -> "StepHandlerRegistry":
        self._by_engine[engine] = _coerce(handler, verify=verify, compensate=compensate)
        return self

    def register_action(
        self,
        action_type: str,
        handler: StepHandler | Callable[..., StepResult | None],
        *,
        verify: Callable[[Operation, StepSpec], bool] | None = None,
        compensate: Callable[..., StepResult | None] | None = None,
    ) -> "StepHandlerRegistry":
        if not action_type:
            raise ValueError("HANDLER_ACTION_TYPE_REQUIRED")
        self._by_action[action_type] = _coerce(handler, verify=verify, compensate=compensate)
        return self

    def register_step(
        self,
        step_id: str,
        handler: StepHandler | Callable[..., StepResult | None],
        *,
        verify: Callable[[Operation, StepSpec], bool] | None = None,
        compensate: Callable[..., StepResult | None] | None = None,
    ) -> "StepHandlerRegistry":
        if not step_id:
            raise ValueError("HANDLER_STEP_ID_REQUIRED")
        self._by_step[step_id] = _coerce(handler, verify=verify, compensate=compensate)
        return self

    @classmethod
    def from_mapping(
        cls, handlers: Mapping[Engine, StepHandler | Callable[..., StepResult | None]]
    ) -> "StepHandlerRegistry":
        registry = cls()
        for engine, handler in handlers.items():
            registry.register_engine(engine, handler)
        return registry

    # -- resolution --------------------------------------------------------

    def resolve(self, step: StepSpec) -> StepHandler | None:
        handler = self._by_step.get(step.step_id)
        if handler is not None:
            return handler
        handler = self._by_action.get(step.action_type)
        if handler is not None:
            return handler
        return self._by_engine.get(step.engine)

    def handlers_for(self, plan: StepPlan) -> dict[Engine, StepHandler]:
        """Engine-level bindings for legacy ``dict`` callers.

        Per-step/per-action overrides are only expressible when the registry
        itself is passed to the executor as the resolver.
        """
        resolved: dict[Engine, StepHandler] = {}
        for spec in plan.ordered():
            handler = self._by_engine.get(spec.engine)
            if handler is not None:
                resolved[spec.engine] = handler
        return resolved

    def missing(self, plan: StepPlan) -> tuple[StepSpec, ...]:
        return tuple(spec for spec in plan.ordered() if self.resolve(spec) is None)

    def assert_complete(self, plan: StepPlan) -> None:
        missing = self.missing(plan)
        if missing:
            first = missing[0]
            raise NoHandlerError(first.engine, first.step_id)


def _coerce(
    handler: StepHandler | Callable[..., StepResult | None],
    *,
    verify: Callable[[Operation, StepSpec], bool] | None,
    compensate: Callable[..., StepResult | None] | None,
) -> StepHandler:
    if hasattr(handler, "perform"):
        if verify is not None or compensate is not None:
            raise ValueError("HANDLER_OVERRIDES_REQUIRE_CALLABLE")
        return handler  # type: ignore[return-value]
    if not callable(handler):
        raise TypeError("HANDLER_NOT_CALLABLE")
    return CallableStepHandler(handler, verify, compensate)


__all__ = [
    "CallableStepHandler",
    "NoHandlerError",
    "StepHandlerRegistry",
]
