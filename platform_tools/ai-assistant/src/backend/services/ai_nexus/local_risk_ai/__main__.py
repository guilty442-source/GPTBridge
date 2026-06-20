from __future__ import annotations

import sys
from pathlib import Path

try:
    from .engine import main
except ImportError:
    services_root = Path(__file__).resolve().parents[2]
    if str(services_root) not in sys.path:
        sys.path.insert(0, str(services_root))
    from ai_nexus.local_risk_ai.engine import main


if __name__ == "__main__":
    raise SystemExit(main())
