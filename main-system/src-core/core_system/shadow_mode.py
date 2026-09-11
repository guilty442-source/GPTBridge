"""Shadow mode runner — parallel execution for safe rollout.

Runs a new implementation alongside the old one.  The old result is
always returned to the caller (no behavior change).  The new result is
compared and discrepancies are logged.  Once confidence is established,
flip the feature flag to switch to the new implementation.

Usage::

    from core_system.shadow_mode import shadow

    result = shadow(
        flag_name="new_ipc_handler_v2",
        bucket_key=session_id,
        old_fn=old_handler,
        new_fn=new_handler,
        *args,
        **kwargs,
    )
    # result is always from old_fn until flag is fully enabled.

For async functions, use ``shadow_async``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, Callable, TypeVar

from startup_core.feature_flags import get_flags

_logger = logging.getLogger("gptbridge.shadow_mode")

T = TypeVar("T")


def _compare(old: Any, new: Any) -> bool:
    """Return True if results are considered equivalent."""
    if old is None and new is None:
        return True
    if type(old) is not type(new):
        return False
    if isinstance(old, (dict, list, tuple, set)):
        return _deep_compare(old, new)
    return old == new


def _deep_compare(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_deep_compare(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_deep_compare(x, y) for x, y in zip(a, b))
    if isinstance(a, set) and isinstance(b, set):
        return a == b
    return a == b


def shadow(
    flag_name: str,
    bucket_key: str,
    old_fn: Callable[..., T],
    new_fn: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run old_fn synchronously; optionally run new_fn in shadow.

    Returns the old_fn result.  If the flag is enabled for this bucket,
    also runs new_fn, compares results, and logs discrepancies.
    """
    flags = get_flags()

    # Always run the old path — it's the serving implementation.
    old_result = old_fn(*args, **kwargs)

    # Determine if shadow should run for this bucket.
    if not flags.is_enabled(flag_name) and not flags.rollout_check(flag_name, bucket_key):
        return old_result

    # Run the new path in shadow (never let it affect the response).
    try:
        start = time.monotonic()
        new_result = new_fn(*args, **kwargs)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if _compare(old_result, new_result):
            _logger.debug(
                "shadow_match flag=%s bucket=%s elapsed_ms=%d",
                flag_name, bucket_key, elapsed_ms,
            )
        else:
            _logger.warning(
                "shadow_mismatch flag=%s bucket=%s elapsed_ms=%d "
                "old_type=%s new_type=%s",
                flag_name, bucket_key, elapsed_ms,
                type(old_result).__name__, type(new_result).__name__,
            )
    except Exception:
        _logger.warning(
            "shadow_error flag=%s bucket=%s\n%s",
            flag_name, bucket_key, traceback.format_exc(),
        )

    # If rollout is 100%, return the new result instead.
    if flags.is_enabled(flag_name) and flags.get(flag_name, "rollout_percentage", 0) >= 100:
        try:
            return new_result  # type: ignore[return-value]
        except Exception:
            pass

    return old_result


async def shadow_async(
    flag_name: str,
    bucket_key: str,
    old_fn: Callable[..., Any],
    new_fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Async version of shadow().  Runs new_fn concurrently with old_fn."""
    flags = get_flags()

    should_shadow = flags.is_enabled(flag_name) or flags.rollout_check(flag_name, bucket_key)
    full_rollout = (
        flags.is_enabled(flag_name)
        and flags.get(flag_name, "rollout_percentage", 0) >= 100
    )

    if full_rollout:
        # New path is the serving implementation.
        return await new_fn(*args, **kwargs)

    if should_shadow:
        # Run both concurrently; return old result.
        old_task = asyncio.ensure_future(old_fn(*args, **kwargs))
        new_task = asyncio.ensure_future(new_fn(*args, **kwargs))
        old_result = await old_task
        try:
            start = time.monotonic()
            new_result = await new_task
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if _compare(old_result, new_result):
                _logger.debug(
                    "shadow_async_match flag=%s bucket=%s elapsed_ms=%d",
                    flag_name, bucket_key, elapsed_ms,
                )
            else:
                _logger.warning(
                    "shadow_async_mismatch flag=%s bucket=%s elapsed_ms=%d",
                    flag_name, bucket_key, elapsed_ms,
                )
        except Exception:
            _logger.warning(
                "shadow_async_error flag=%s bucket=%s\n%s",
                flag_name, bucket_key, traceback.format_exc(),
            )
        return old_result

    # No shadow — just run old path.
    return await old_fn(*args, **kwargs)


__all__ = ["shadow", "shadow_async"]
