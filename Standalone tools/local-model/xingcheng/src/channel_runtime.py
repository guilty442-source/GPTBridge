"""Xingcheng companion runtime entry (delegates to the canonical runtime).

The canonical governed runtime — the ``xingcheng`` service loop that ends in
``await runtime.run()`` — lives at ``../../src/channel_runtime.py`` under the
``local-model`` platform root and registers as ``TOOL_ID = "xingcheng"``.
This companion manifest entry only forwards to the canonical file; it keeps
the nested ``local-model/xingcheng`` runtime entry resolving to the same
governed native-model runtime.
"""

from __future__ import annotations

import runpy
from pathlib import Path


_TARGET = (
    Path(__file__).resolve().parents[2] / "src" / "channel_runtime.py"
).resolve()
if not _TARGET.is_file():
    raise PermissionError("PERMISSION_DENIED")

runpy.run_path(str(_TARGET), run_name="__main__")
