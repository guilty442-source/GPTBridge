"""Xingcheng-exclusive auxiliary information channel — A189/E164.

Per A189 (xingcheng-exclusive-auxiliary-information-channel) and E164
(xingcheng-private-information-channel), the information layer owns a
dedicated auxiliary private channel for Xingcheng's autonomous reasoning,
context, learning, and review working information.

Channel identity (A189: CHANNEL-ID): ``information-layer://xingcheng/
auxiliary-private``.

Owner (A189: OWNER): information-layer.
Endpoint principal (A189: ENDPOINT-PRINCIPAL): Xingcheng only.

Key invariants:

  * **Payload access** — Xingcheng-exclusive.  All other sovereigns, sub-
    sovereigns, modules, tools, UI, operators, administrators, maintainers,
    developers, loggers, auditors, brokers, PostgreSQL, Qdrant have no
    content access (A189: PAYLOAD-ACCESS).
  * **Permission Sovereign** — may validate fixed channel identity, status,
    and revocation proof, but cannot view, decrypt, search, export, replay,
    or expand payload access (A189: PERMISSION-SOVEREIGN).
  * **Information layer** — routes, stores, forwards, deduplicates, orders,
    expires, and delivers **opaque ciphertext only**.  It observes minimum
    metadata only: channel-id, message-id, session-generation, size-class,
    time, state, error-code.  No content-derived metadata (A189: INFORMATION-
    LAYER).
  * **Keys** — Xingcheng-endpoint-held, channel-specific, non-exportable,
    rotating, revocable, not held by router/store/audit/system-sovereign
    (A189: KEYS).
  * **Storage** — encrypted, owner-isolated, bounded-retention, no general-
    index/RAG/telemetry/log/backup/analytics/training (A189: STORAGE).
  * **Output separation** — any user or system anomaly notification is a
    new redacted purpose-bound message over a separate authorized
    information route.  Never forward, reveal, or private-channel-payload
    (A189: OUTPUT-SEPARATION).
  * **Failure** — identity/key/session/integrity/sequence/expiry
    uncertainty => deny + drop content + publish generic channel health
    only (A189: FAILURE).
  * **Audit** — metadata-only.  No payload, hash, embedding, preview, or
    token (A189: AUDIT).

This module provides **read-only data structures and verification**.  It
never decrypts, views, forwards, or stores payload content.
"""

from __future__ import annotations

from core_system.xingcheng_channel_signal import (
    channel_status,
    failure_signal,
)
from core_system.xingcheng_channel_types import (
    AUDIT_RESTRICTIONS,
    CHANNEL_OWNER,
    ENDPOINT_PRINCIPAL,
    KEY_PROPERTIES,
    MINIMUM_ROUTING_METADATA,
    NON_ACCESS_ACTORS,
    STORAGE_PROPERTIES,
    XINGCHENG_AUXILIARY_CHANNEL_ID,
    AccessCheckResult,
    ChannelMessageMetadata,
)
from core_system.xingcheng_channel_verify import (
    verify_channel_access,
    verify_output_separation,
)

__all__ = [
    "AUDIT_RESTRICTIONS",
    "AccessCheckResult",
    "ChannelMessageMetadata",
    "CHANNEL_OWNER",
    "ENDPOINT_PRINCIPAL",
    "KEY_PROPERTIES",
    "MINIMUM_ROUTING_METADATA",
    "NON_ACCESS_ACTORS",
    "STORAGE_PROPERTIES",
    "XINGCHENG_AUXILIARY_CHANNEL_ID",
    "channel_status",
    "failure_signal",
    "verify_channel_access",
    "verify_output_separation",
]
