"""O6 shared audit file cache (mtime+size keyed, process-local).

Audit checks re-read the same source files every run (135 migration files,
helper modules, contracts).  Content is cached per path and re-read only
when ``st_mtime_ns`` or ``st_size`` changes — a conservative signature per
the blueprint's cache-invalidation rule (any write changes mtime or size;
a same-size/same-mtime forged write is out of scope for a read-side audit
cache).  Never persisted; cleared on process exit.
"""

from __future__ import annotations

from pathlib import Path

_CACHE: dict[str, tuple[int, int, str]] = {}


def read_text_cached(path: Path, *, encoding: str = "utf-8") -> str:
    """``path.read_text(encoding)`` with mtime+size invalidation.

    Raises the same OSError subclasses as ``Path.read_text`` so callers
    keep identical error handling.
    """
    key = str(path)
    stat = path.stat()  # raises FileNotFoundError like read_text
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None and cached[:2] == signature:
        return cached[2]
    text = path.read_text(encoding=encoding)
    _CACHE[key] = (signature[0], signature[1], text)
    return text


def clear() -> None:
    _CACHE.clear()
