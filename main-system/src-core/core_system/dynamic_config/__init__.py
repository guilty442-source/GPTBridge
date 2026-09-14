"""Dynamic-First Configuration Package — A386/A409.

A386: DEFAULT:DYNAMIC-FIRST. Every value that may vary by machine, environment, worktree,
module, generation, release, capacity or deployment is referenced by a typed canonical
code registry or governed environment binding.

A409: ONLY-HARDCODE-ALLOWANCE: the only literal runtime/deployment value permitted to be
hardcoded is a timestamp value created for an immutable event, revision or evidence record.
"""

from .registry import (
    ConfigCategory,
    ConfigKey,
    ConfigValue,
    DynamicConfigRegistry,
    TimestampHardcodeValidator,
    create_dynamic_config_registry,
    create_timestamp_validator,
)

__all__ = [
    "ConfigCategory",
    "ConfigKey",
    "ConfigValue",
    "DynamicConfigRegistry",
    "TimestampHardcodeValidator",
    "create_dynamic_config_registry",
    "create_timestamp_validator",
]