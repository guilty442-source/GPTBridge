"""High-performance inference for StarAutoregressiveLanguageModel.

Optimizations:
- Pre-computed vocabulary index mapping (token -> int)
- Pre-computed context distributions as dense arrays
- Cached transient contexts per (intent, prompt_signature)
- Integer arithmetic in hot path
- Batched inference with shared transient contexts
"""

from __future__ import annotations

import hashlib
import math
import random
from array import array
from collections import Counter, defaultdict, OrderedDict
from functools import lru_cache
from typing import Any, Sequence

from .generative_language_model import (
    StarAutoregressiveLanguageModel,
    _BOS,
    _EOS,
)


class FastInferenceEngine:
    """Optimized inference engine for StarAutoregressiveLanguageModel."""

    def __init__(
        self,
        model: StarAutoregressiveLanguageModel,
        *,
        enable_result_cache: bool = True,
        result_cache_size: int = 128,
    ) -> None:
        self.model = model
        self.order = model.order
        self._enable_result_cache = enable_result_cache
        self._result_cache_size = result_cache_size

        # Build vocabulary index (token -> int) for array operations
        self._vocab_list = list(model._vocabulary)
        self._vocab_to_idx = {token: i for i, token in enumerate(self._vocab_list)}
        self._vocab_size = len(self._vocab_list)

        # Pre-compute context distributions as dense arrays
        # Format: {context_tuple: array('I', [weight_0, weight_1, ...])}
        self._context_distributions: dict[tuple[str, ...], array] = {}
        self._build_distributions()

        # Pre-compute intent tokens
        self._intent_tokens = {}
        for intent in set(ex["intent"] for ex in model._examples):
            self._intent_tokens[intent] = model._intent_token(intent)

        # Pre-compute token indices for special tokens
        self._bos_idx = self._vocab_to_idx.get(_BOS, -1)
        self._eos_idx = self._vocab_to_idx.get(_EOS, -1)

        # Cache for transient contexts keyed by (intent, prompt_hash)
        self._transient_cache: dict[tuple[str, int], dict] = {}

        # LRU cache for full generation results (identical prompts)
        self._result_cache: OrderedDict[tuple, dict] = OrderedDict()

    def _build_distributions(self) -> None:
        """Pre-compute all context distributions as dense integer arrays."""
        for context, counter in self.model._counts.items():
            arr = array("I", [0] * self._vocab_size)
            for token, weight in counter.items():
                idx = self._vocab_to_idx.get(token)
                if idx is not None:
                    arr[idx] = weight
            self._context_distributions[context] = arr

    def _get_prompt_signature(self, prompt: str, grounding: str) -> int:
        """Compute stable hash for prompt+grounding to cache transient."""
        return int(hashlib.md5(f"{prompt}|{grounding}".encode()).hexdigest()[:16], 16)

    def _get_or_build_transient(
        self,
        intent: str,
        prompt: str,
        grounding: str,
    ) -> dict[tuple[str, ...], array]:
        """Get or build transient context, with caching."""
        cache_key = (intent, self._get_prompt_signature(prompt, grounding))

        if cache_key in self._transient_cache:
            return self._transient_cache[cache_key]

        transient = self._build_transient(intent, prompt, grounding)
        self._transient_cache[cache_key] = transient
        return transient

    def _get_distribution(self, history: Sequence[str], transient: dict) -> array:
        """Get distribution for history using pre-computed arrays."""
        max_width = min(self.order - 1, len(history))
        for width in range(max_width, -1, -1):
            context = tuple(history[-width:]) if width else ()

            # Try transient first (higher weight)
            transient_arr = transient.get(context)
            if transient_arr is not None:
                return transient_arr

            # Try persistent
            persistent_arr = self._context_distributions.get(context)
            if persistent_arr is not None:
                return persistent_arr

        return None

    def _sample_from_array(
        self,
        dist_array: array,
        randomizer: random.Random,
        temperature: float,
        top_k: int,
    ) -> int:
        """Sample from pre-computed distribution array using fast path."""
        if dist_array is None:
            return self._eos_idx

        # Fast path: find top-k in single pass
        best_idx = -1
        best_weight = 0
        candidates = []

        for idx, weight in enumerate(dist_array):
            if weight > 0:
                candidates.append((idx, weight))
                if weight > best_weight:
                    best_weight = weight
                    best_idx = idx

        if not candidates:
            return self._eos_idx

        if temperature <= 0 or len(candidates) == 1:
            return best_idx

        # Partial sort for top-k (faster than full sort for small k)
        candidates.sort(key=lambda x: -x[1])
        top_candidates = candidates[:max(1, top_k)]

        exponent = 1.0 / max(0.1, min(2.0, temperature))
        weighted = [(idx, math.pow(max(1, weight), exponent)) for idx, weight in top_candidates]
        threshold = randomizer.random() * sum(w for _, w in weighted)
        cumulative = 0.0
        for idx, weight in weighted:
            cumulative += weight
            if cumulative >= threshold:
                return idx
        return top_candidates[-1][0]

    def generate_fast(
        self,
        *,
        intent: str,
        prompt: str,
        grounding: str = "",
        max_tokens: int = 180,
        temperature: float = 0.55,
        top_k: int = 4,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Fast single-prompt generation with optional transient and result caching."""
        # Check result cache for identical prompt
        cache_key = (intent, prompt, grounding, max_tokens, temperature, top_k)
        if self._enable_result_cache and use_cache and cache_key in self._result_cache:
            self._result_cache.move_to_end(cache_key)
            return self._result_cache[cache_key]

        intent_token = self._intent_tokens.get(intent, self.model._intent_token(intent))
        prompt_tokens = self.model.tokenize(prompt)
        grounding_tokens = self.model.tokenize(grounding) if grounding else []

        if use_cache:
            transient = self._get_or_build_transient(intent, prompt, grounding)
        else:
            transient = self._build_transient(intent, prompt, grounding)

        # Initialize history with BOS and intent
        history = [*([_BOS] * (self.order - 1)), intent_token, *prompt_tokens, *grounding_tokens]

        seed = self.model._seed(prompt, intent)
        randomizer = random.Random(seed)

        output_tokens = []
        repeated: Counter[tuple[str, str, str]] = Counter()

        max_toks = max(16, min(360, int(max_tokens)))

        for _ in range(max_toks):
            dist_array = self._get_distribution(history, transient)
            token_idx = self._sample_from_array(dist_array, randomizer, temperature, top_k)

            if token_idx == self._eos_idx or token_idx < 0:
                break

            token = self._vocab_list[token_idx]

            # Repetition penalty
            if len(output_tokens) >= 2:
                trigram = (output_tokens[-2], output_tokens[-1], token)
                repeated[trigram] += 1
                if repeated[trigram] > 2:
                    break

            output_tokens.append(token)
            history.append(token)

        text = self.model.detokenize(output_tokens)
        if text and text[-1] not in "。！？.!?":
            text += "。"

        result = {
            "text": text,
            "tokens": output_tokens,
            "intent": intent,
        }

        # Store in result cache
        if self._enable_result_cache and use_cache:
            self._result_cache[cache_key] = result
            if len(self._result_cache) > self._result_cache_size:
                self._result_cache.popitem(last=False)

        return result

    def _build_transient(
        self,
        intent: str,
        prompt: str,
        grounding: str,
    ) -> dict[tuple[str, ...], array]:
        """Build transient context from prompt-conditioned examples."""
        transient: dict[tuple[str, ...], array] = {}

        prompt_tokens = set(self.model.tokenize(prompt))
        conditioned_examples = []

        for example in self.model._examples:
            if example["intent"] != intent:
                continue
            example_tokens = set(self.model.tokenize(example["input_text"]))
            similarity = (
                2 * len(prompt_tokens & example_tokens)
                / (len(prompt_tokens) + len(example_tokens))
                if prompt_tokens and example_tokens
                else 0.0
            )
            if similarity > 0:
                conditioned_examples.append((similarity, example))

        conditioned_examples.sort(key=lambda item: -item[0])

        for similarity, example in conditioned_examples[:6]:
            weight = max(1, round(2 + similarity * 10))
            sequence = self.model._sequence(intent, example["target_text"])
            self._accumulate_to_transient(transient, sequence, weight)

        if grounding:
            sequence = self.model._sequence(intent, grounding.strip())
            self._accumulate_to_transient(transient, sequence, 12)

        return transient

    def _accumulate_to_transient(
        self,
        transient: dict,
        sequence: Sequence[str],
        weight: int,
    ) -> None:
        """Accumulate sequence into transient distributions."""
        for index in range(self.order, len(sequence)):
            next_token = sequence[index]
            next_idx = self._vocab_to_idx.get(next_token)
            if next_idx is None:
                continue
            for width in range(self.order):
                context = tuple(sequence[index - width:index]) if width else ()
                arr = transient.get(context)
                if arr is None:
                    arr = array("I", [0] * self._vocab_size)
                    transient[context] = arr
                arr[next_idx] += weight

    def generate_batch(
        self,
        prompts: list[dict[str, Any]],
        *,
        max_tokens: int = 180,
        temperature: float = 0.55,
        top_k: int = 4,
    ) -> list[dict[str, Any]]:
        """Generate for multiple prompts efficiently.

        Each prompt dict should have: intent, prompt, grounding (optional)
        """
        results = []
        for p in prompts:
            result = self.generate_fast(
                intent=p["intent"],
                prompt=p["prompt"],
                grounding=p.get("grounding", ""),
                max_tokens=max_tokens,
                temperature=temperature,
                top_k=top_k,
            )
            results.append(result)
        return results


def create_fast_engine(
    model: StarAutoregressiveLanguageModel,
    *,
    enable_result_cache: bool = True,
    result_cache_size: int = 128,
) -> FastInferenceEngine:
    """Factory function to create optimized inference engine."""
    return FastInferenceEngine(
        model,
        enable_result_cache=enable_result_cache,
        result_cache_size=result_cache_size,
    )


__all__ = ["FastInferenceEngine", "create_fast_engine"]