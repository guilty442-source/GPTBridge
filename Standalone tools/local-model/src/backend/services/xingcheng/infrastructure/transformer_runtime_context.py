from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Mapping


class TransformerRuntimeContextMixin:
    """Adaptive context-window, parameter-cache and fact helpers."""

    @staticmethod
    def _finite_number(
        value: Any, default: float, minimum: float, maximum: float
    ) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        if not math.isfinite(parsed):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _bounded_visual_inputs(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        images: list[str] = []
        total_characters = 0
        for item in value[:32]:
            encoded = str(item or "").strip()
            if ";base64," in encoded[:128]:
                encoded = encoded.split(";base64,", 1)[1]
            if not encoded or len(encoded) > 16_000_000:
                continue
            total_characters += len(encoded)
            if total_characters > 64_000_000:
                break
            images.append(encoded)
        return images

    @classmethod
    def _is_memory_pressure(cls, error: BaseException) -> bool:
        message = str(error or "").casefold()
        return any(marker in message for marker in cls._MEMORY_PRESSURE_MARKERS)

    @staticmethod
    def _estimated_token_count(text: str) -> int:
        # CJK text is commonly close to one token per character while Latin
        # text is usually denser.  Overestimating here prevents silent input
        # truncation when the context window is reduced after memory pressure.
        length = len(text)
        if length == 0:
            return 0
        cjk_or_wide = sum(1 for character in text if ord(character) > 0x7F)
        latin = max(0, length - cjk_or_wide)
        return int(math.ceil(cjk_or_wide + (latin / 3.5)))

    @classmethod
    def _bounded_context_steps(cls, maximum: int) -> tuple[int, ...]:
        bounded = tuple(step for step in cls.CONTEXT_WINDOW_STEPS if step <= maximum)
        if maximum >= cls.MIN_CONTEXT_WINDOW and maximum not in bounded:
            bounded = (*bounded, maximum)
        return bounded or (max(1, maximum),)

    def _context_state_for(self, model: str, maximum: int) -> dict[str, int]:
        with self._lock:
            state = self._context_states.get(model)
            if state is None:
                state = {
                    "context_ceiling": min(self.SAFE_CONTEXT_WINDOW, maximum),
                    "success_streak": 0,
                    "memory_pressure_count": 0,
                }
                if self._checkpoint_repository is not None:
                    stored = self._checkpoint_repository.load_context_state(model)
                    if stored is not None:
                        state.update(stored)
                self._context_states[model] = state
            state["context_ceiling"] = max(
                1, min(int(state["context_ceiling"]), maximum)
            )
            return dict(state)

    def _persist_context_state(self, model: str, state: Mapping[str, Any]) -> None:
        repository = self._checkpoint_repository
        if repository is not None:
            repository.save_context_state(model, state)

    def _cached_resolve(
        self,
        *,
        model: str,
        task_intensity: str,
        reasoning_effort: str,
        request_key: str = "",
        immutable_base: bool = False,
    ) -> dict[str, Any]:
        """Cache parameter_policy.resolve() for a few seconds to avoid
        repeated stat/JSON/deepcopy overhead within a single request."""
        cache_key = f"{model}|{task_intensity}|{reasoning_effort}|{immutable_base}"
        now = time.monotonic()
        with self._lock:
            cached = self._param_cache.get(cache_key)
            if cached and (now - cached[0]) < self._param_cache_ttl:
                return dict(cached[1])
        result = self.parameter_policy.resolve(
            model=model,
            task_intensity=task_intensity,
            reasoning_effort=reasoning_effort,
            request_key=request_key,
            immutable_base=immutable_base,
        )
        with self._lock:
            self._param_cache[cache_key] = (now, dict(result))
        return result

    def _select_context_window(
        self,
        *,
        model: str,
        maximum: int,
        system: str,
        user: str,
        num_predict: int,
    ) -> tuple[int, int]:
        steps = self._bounded_context_steps(maximum)
        estimated_required = (
            self._estimated_token_count(f"{system}\n{user}")
            + int(num_predict)
            + 256
        )
        required = next(
            (step for step in steps if step >= estimated_required), steps[-1]
        )
        desired = max(min(self.SAFE_CONTEXT_WINDOW, maximum), required)
        state = self._context_state_for(model, maximum)
        ceiling = int(state["context_ceiling"])
        # A genuinely large input may raise the window immediately; this is
        # preferable to losing content through implicit runtime truncation.
        selected = desired if required > self.SAFE_CONTEXT_WINDOW else min(desired, ceiling)
        selected = max(min(selected, maximum), min(required, maximum))
        return selected, required

    def _record_context_success(
        self, *, model: str, used_context: int, maximum: int
    ) -> dict[str, int]:
        steps = self._bounded_context_steps(maximum)
        with self._lock:
            current = dict(
                self._context_states.get(
                    model,
                    {
                        "context_ceiling": min(self.SAFE_CONTEXT_WINDOW, maximum),
                        "success_streak": 0,
                        "memory_pressure_count": 0,
                    },
                )
            )
            current["context_ceiling"] = max(
                int(current["context_ceiling"]), int(used_context)
            )
            current["success_streak"] = int(current["success_streak"]) + 1
            if current["success_streak"] >= self.CONTEXT_GROWTH_SUCCESS_THRESHOLD:
                ceiling = int(current["context_ceiling"])
                next_step = next((step for step in steps if step > ceiling), ceiling)
                current["context_ceiling"] = next_step
                current["success_streak"] = 0
            self._context_states[model] = current
        self._persist_context_state(model, current)
        return dict(current)

    def _record_context_memory_pressure(
        self,
        *,
        model: str,
        failed_context: int,
        required_context: int,
        maximum: int,
    ) -> tuple[int | None, dict[str, int]]:
        steps = self._bounded_context_steps(maximum)
        lower = [step for step in steps if step < int(failed_context)]
        next_context = lower[-1] if lower else None
        with self._lock:
            current = dict(
                self._context_states.get(
                    model,
                    {
                        "context_ceiling": min(self.SAFE_CONTEXT_WINDOW, maximum),
                        "success_streak": 0,
                        "memory_pressure_count": 0,
                    },
                )
            )
            if next_context is not None:
                current["context_ceiling"] = next_context
            current["success_streak"] = 0
            current["memory_pressure_count"] = (
                int(current["memory_pressure_count"]) + 1
            )
            self._context_states[model] = current
        self._persist_context_state(model, current)
        if next_context is None or next_context < required_context:
            return None, dict(current)
        return next_context, dict(current)

    @classmethod
    def _fact_values(cls, text: str) -> dict[str, set[str]]:
        return {
            name: {
                re.sub(r"[\s,]", "", match.casefold())
                for match in re.findall(pattern, str(text or ""), flags=re.IGNORECASE)
            }
            for name, pattern in cls._FACT_PATTERNS.items()
        }

    @staticmethod
    def _structured_context(output: Mapping[str, Any]) -> str:
        selected = {
            key: output.get(key)
            for key in (
                "response",
                "semantic_understanding",
                "analysis",
                "market_research",
                "mathematical_result",
                "coding_result",
                "self_repair",
                "fault_diagnostics",
                "rag_context",
                "evidence",
                "instruction_execution",
                "parallel_model_results",
            )
            if output.get(key) is not None
        }
        if str(output.get("intent") or "") == "self_upgrade":
            repair = output.get("self_repair")
            if isinstance(repair, Mapping):
                selected["self_repair"] = {
                    key: repair.get(key)
                    for key in (
                        "executed",
                        "status",
                        "actions",
                        "recommendations",
                        "source_write_performed",
                        "governance_rule_modified",
                        "investment_database_write_performed",
                        "version",
                    )
                    if repair.get(key) is not None
                }
            coding = output.get("coding_result")
            if isinstance(coding, Mapping):
                selected["coding_result"] = {
                    key: coding.get(key)
                    for key in ("ok", "intent", "language", "validation")
                    if coding.get(key) is not None
                }
            return json.dumps(
                selected, ensure_ascii=False, separators=(",", ":")
            )
        return json.dumps(selected, ensure_ascii=False, separators=(",", ":"))
