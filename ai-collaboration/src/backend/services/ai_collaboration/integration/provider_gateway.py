"""Governance-owned entry point for external AI provider execution.

The version-1 provider gateway is intentionally browser-only.  The concrete
implementation lives in ``provider_session`` so the application runtime and
governance-owned gateway use one execution path.
"""

from .provider_session import AiCollaborationProviderSession


BrowserProviderGateway = AiCollaborationProviderSession

__all__ = ["AiCollaborationProviderSession", "BrowserProviderGateway"]
