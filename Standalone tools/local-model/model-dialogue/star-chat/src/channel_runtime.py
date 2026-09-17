"""Star-Chat companion runtime entry (delegates to model-dialogue).

The canonical governed runtime — the ``StarChatService`` loop that ends in
``await runtime.run()`` — lives at ``../src/channel_runtime.py`` under the
sealed ``model-dialogue`` identity.  star-chat is a companion component of
model-dialogue, not an independent tool, so this entry only forwards to the
canonical file; it keeps legacy star-chat references resolving to the same
governed, model-optional dialogue runtime.
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
