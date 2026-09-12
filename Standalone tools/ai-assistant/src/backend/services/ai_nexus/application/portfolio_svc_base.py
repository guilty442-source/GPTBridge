from __future__ import annotations

from typing import Any


class PortfolioSvcBaseMixin:
    async def _get_analytics(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return self._state_response(self.repository.load_state())
