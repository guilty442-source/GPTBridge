"""Async method mixin for PostgresSharedLayerStore (A185 split).

Assembly layer: request-channel wrappers live in
``store_async_request`` and push-channel wrappers in
``store_async_push``; the shared provenance-bound connection helper
lives in ``store_pool`` (``_governed_connection``).
"""
from __future__ import annotations

from .store_async_push import PostgresStoreAsyncPushMixin
from .store_async_request import PostgresStoreAsyncRequestMixin


class PostgresStoreAsyncMixin(
    PostgresStoreAsyncRequestMixin,
    PostgresStoreAsyncPushMixin,
):
    """Async wrappers for PostgresSharedLayerStore."""
