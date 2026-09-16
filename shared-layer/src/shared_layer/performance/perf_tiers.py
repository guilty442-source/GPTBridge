"""Performance Benchmark Tiers — PERF_SMOKE / PERF_STANDARD / PERF_FULL.

Three benchmark tiers control the cost/coverage trade-off:

    PERF_SMOKE:    Dev runs.  Fast feedback.  Small size, few samples.
    PERF_STANDARD: Integration runs.  Balanced.  Medium size, moderate samples.
    PERF_FULL:     Release runs.  Comprehensive.  All sizes, many samples.

Each tier defines:
    - sizes: which SizeClass values to benchmark
    - warmths: which WarmthClass values to benchmark
    - concurrencies: which concurrency levels to test
    - sample_count: how many samples per configuration
    - warmup_count: how many warmup runs before sampling
    - stable_runs: how many independent runs to collect for tolerance

Correctness/contract/resource safety always overrides performance.
A tier never skips correctness checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence


class BenchmarkTier(str, Enum):
    SMOKE = "PERF_SMOKE"
    STANDARD = "PERF_STANDARD"
    FULL = "PERF_FULL"


@dataclass(frozen=True)
class TierConfig:
    """Configuration for one benchmark tier."""
    tier: BenchmarkTier
    sizes: tuple[str, ...]
    warmths: tuple[str, ...]
    concurrencies: tuple[int, ...]
    sample_count: int
    warmup_count: int
    stable_runs: int  # independent runs for tolerance computation
    description: str


# Tier configurations
TIER_CONFIGS: dict[BenchmarkTier, TierConfig] = {
    BenchmarkTier.SMOKE: TierConfig(
        tier=BenchmarkTier.SMOKE,
        sizes=("small",),
        warmths=("warm",),
        concurrencies=(1,),
        sample_count=5,
        warmup_count=2,
        stable_runs=3,
        description="Dev: fast feedback, small size, few samples",
    ),
    BenchmarkTier.STANDARD: TierConfig(
        tier=BenchmarkTier.STANDARD,
        sizes=("small", "medium"),
        warmths=("cold", "warm"),
        concurrencies=(1, 4),
        sample_count=10,
        warmup_count=3,
        stable_runs=5,
        description="Integration: balanced, medium size, moderate samples",
    ),
    BenchmarkTier.FULL: TierConfig(
        tier=BenchmarkTier.FULL,
        sizes=("small", "medium", "large"),
        warmths=("cold", "warm"),
        concurrencies=(1, 4, 8),
        sample_count=20,
        warmup_count=5,
        stable_runs=7,
        description="Release: comprehensive, all sizes, many samples",
    ),
}


def get_tier_config(tier: BenchmarkTier) -> TierConfig:
    """Get the configuration for a benchmark tier."""
    return TIER_CONFIGS[tier]


def select_tier_by_env(env: str | None = None) -> BenchmarkTier:
    """Select the appropriate benchmark tier by environment.

    Environments:
        "dev" / "development" / None -> PERF_SMOKE
        "integration" / "ci"         -> PERF_STANDARD
        "release" / "production"     -> PERF_FULL
    """
    import os
    env = env or os.environ.get("PERF_TIER_ENV", "dev")
    env_lower = env.lower()

    if env_lower in ("release", "production", "prod"):
        return BenchmarkTier.FULL
    elif env_lower in ("integration", "ci", "continuous"):
        return BenchmarkTier.STANDARD
    else:
        return BenchmarkTier.SMOKE


__all__ = [
    "BenchmarkTier",
    "TierConfig",
    "TIER_CONFIGS",
    "get_tier_config",
    "select_tier_by_env",
]
