from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from governance_rule.execution.authentication import (
    GovernanceAuthenticationService,
)
from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)

from .store import SharedLayerStore


_CHANNELS: Final[frozenset[str]] = frozenset({"system", "ai"})


class SharedLayerChannel:
    """Governed request channel used by registered independent tools."""

    def __init__(
        self,
        project_root: Path | str,
        tool_id: str,
        authentication: GovernanceAuthenticationService,
        channel_id: str = "system",
    ) -> None:
        if not isinstance(authentication, GovernanceAuthenticationService):
            raise permission_denied()
        self._authentication = authentication
        self._tool_id = str(tool_id).strip()
        self._channel_id = str(channel_id or "").strip().casefold()
        if not self._tool_id or self._tool_id == "main-system":
            raise permission_denied()
        if self._channel_id not in _CHANNELS:
            raise permission_denied()
        policy = directory_authority_snapshot().shared_layer_access_policy
        self._database_path = (
            policy.ai_database_path
            if self._channel_id == "ai"
            else policy.database_path
        )
        self._store = SharedLayerStore(
            project_root,
            authentication,
            self._channel_id,
        )

    @property
    def channel_id(self) -> str:
        return self._channel_id

    def _issue(
        self,
        *,
        capability: str,
        action: str,
        target_tool_id: str | None,
    ) -> str:
        governed_target_tool_id = (
            None if target_tool_id == self._tool_id else target_tool_id
        )
        operation_tool_id = governed_target_tool_id or self._tool_id
        return self._authentication.issue_token(
            target_tool_id=governed_target_tool_id,
            capability=capability,
            action=action,
            target=(
                f"shared-layer-{self._channel_id}-request:{operation_tool_id}"
            ),
            data_scope=f"shared-layer-{self._channel_id}-request",
            resource_path=self._database_path,
        )

    def request(
        self,
        target_tool_id: str,
        request_id: str,
        payload: Any,
    ) -> None:
        token = self._issue(
            capability=f"{self._channel_id}-channel-request-submit",
            action="request",
            target_tool_id=target_tool_id,
        )
        self._store.submit_request(
            token,
            request_id,
            target_tool_id,
            payload,
        )

    def cancel(self, target_tool_id: str, request_id: str) -> bool:
        token = self._issue(
            capability=f"{self._channel_id}-channel-request-submit",
            action="cancel-request",
            target_tool_id=target_tool_id,
        )
        return self._store.cancel_request(
            token,
            request_id,
            target_tool_id,
        )

    def request_cancelled(self, request_id: str) -> bool:
        token = self._issue(
            capability=f"{self._channel_id}-channel-request-process",
            action="claim",
            target_tool_id=None,
        )
        return self._store.request_cancelled(
            token,
            request_id,
            self._tool_id,
        )

    def progress(self, request_id: str, payload: Any) -> bool:
        token = self._issue(
            capability=f"{self._channel_id}-channel-request-process",
            action="respond",
            target_tool_id=None,
        )
        return self._store.publish_progress(
            token,
            request_id,
            self._tool_id,
            payload,
        )

    def response(
        self,
        target_tool_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        token = self._issue(
            capability=f"{self._channel_id}-channel-request-submit",
            action="consume-response",
            target_tool_id=target_tool_id,
        )
        return self._store.consume_response(token, request_id, target_tool_id)

    def claim(self) -> dict[str, Any] | None:
        token = self._issue(
            capability=f"{self._channel_id}-channel-request-process",
            action="claim",
            target_tool_id=None,
        )
        return self._store.claim_request(token, self._tool_id)

    def notify_for_request(self, request_id: str) -> None:
        token = self._issue(
            capability=f"{self._channel_id}-channel-request-process",
            action="respond",
            target_tool_id=None,
        )
        try:
            self._store.notify_channel(token, self._tool_id)
        except PermissionError:
            pass

    def respond(self, request_id: str, response: Any) -> bool:
        token = self._issue(
            capability=f"{self._channel_id}-channel-request-process",
            action="respond",
            target_tool_id=None,
        )
        return self._store.respond(
            token,
            request_id,
            self._tool_id,
            response,
        )
