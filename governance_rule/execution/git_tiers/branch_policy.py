"""Single branch policy source (A375 / task §17).

Every component that classifies branches — coordinator, supervisor,
pre-receive hook, prune/reconcile — resolves policy through this module.
Names live here exactly once; nothing else hardcodes branch matching.

Policy:
  * ``main`` is the sole stable integration branch — workers may never
    commit or push it directly; only the coordinator merges into it.
  * persistent pool worktrees (``git``, ``local-model``, ``rag``, ``ui``)
    are protected: never auto-deleted, may be auto fast-forwarded by the
    coordinator after integration.
  * ephemeral AI-pool branches (the registered prefixes) may be proposed
    for retirement after their lifecycle completes, but actual deletion
    always stays a Tier-3 governed operation.
"""
from __future__ import annotations

import hashlib
import json
from typing import Final

MAIN_BRANCH: Final[str] = "main"
COORDINATOR_ACTORS: Final[frozenset[str]] = frozenset(
    {
        "governance/workspace-sync",
        "governance/git-coordinator",
        "governance/automation-supervisor",
    }
)

LOCAL_MODEL_BRANCH: Final[str] = "local-model"
PROTECTED_BRANCHES: Final[frozenset[str]] = frozenset(
    {MAIN_BRANCH, "git", LOCAL_MODEL_BRANCH, "rag", "ui"}
)

EPHEMERAL_PREFIXES: Final[tuple[str, ...]] = (
    "ai/",
    "arch-",
    "bright-",
    "checker-",
    "flossy-",
    "sandy-",
    "permission-",
    "runtime-",
    "sync-",
    "xingcheng-",
)

# Tag namespaces that may never be rewritten or deleted without Tier-3
# governance approval.
PROTECTED_TAG_PREFIXES: Final[tuple[str, ...]] = (
    "release/",
    "codex-",
)


def normalize_branch(ref: str) -> str:
    """Strip ``refs/heads/`` / ``refs/remotes/<remote>/`` to a branch name."""
    value = str(ref or "").strip()
    for prefix in ("refs/heads/",):
        if value.startswith(prefix):
            return value[len(prefix):]
    if value.startswith("refs/remotes/"):
        rest = value[len("refs/remotes/"):]
        return rest.partition("/")[2] or rest
    return value


def classify_branch(branch: str) -> str:
    """Return ``protected`` | ``ephemeral`` | ``standard``."""
    name = normalize_branch(branch)
    if name in PROTECTED_BRANCHES:
        return "protected"
    if name.startswith(EPHEMERAL_PREFIXES):
        return "ephemeral"
    return "standard"


def is_protected(branch: str) -> bool:
    return classify_branch(branch) == "protected"


def is_ephemeral(branch: str) -> bool:
    return classify_branch(branch) == "ephemeral"


def is_main(branch: str) -> bool:
    return normalize_branch(branch) == MAIN_BRANCH


def is_protected_tag(ref: str) -> bool:
    """True for tags inside governed namespaces (release/*, codex-*)."""
    value = str(ref or "").strip()
    if not value.startswith("refs/tags/"):
        return False
    name = value[len("refs/tags/"):]
    return name.startswith(PROTECTED_TAG_PREFIXES)


def can_auto_retire(branch: str) -> bool:
    """Only ephemeral branches may even be *proposed* for retirement."""
    return is_ephemeral(branch)


def can_auto_fast_forward(branch: str) -> bool:
    """Persistent workers may be fast-forwarded; main never (it is the
    integration target, not a follower)."""
    name = normalize_branch(branch)
    return is_protected(name) and name != MAIN_BRANCH


def can_worker_commit(branch: str) -> bool:
    """Workers may never commit directly on main (A163)."""
    return not is_main(branch)


def can_receive_direct_push(branch: str, actor: str = "") -> bool:
    """Direct push is a coordinator-only capability.

    Workers never push (A163/A375); the coordinator may push ``main`` only
    through the governed release path.  Non-coordinator actors are denied
    for every branch.
    """
    return str(actor or "") in COORDINATOR_ACTORS


def policy_digest() -> str:
    """Stable hash of the policy surface, embedded in merge-queue entries."""
    payload = {
        "protected": sorted(PROTECTED_BRANCHES),
        "ephemeral_prefixes": list(EPHEMERAL_PREFIXES),
        "protected_tag_prefixes": list(PROTECTED_TAG_PREFIXES),
        "main": MAIN_BRANCH,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


__all__ = [
    "COORDINATOR_ACTORS",
    "EPHEMERAL_PREFIXES",
    "MAIN_BRANCH",
    "PROTECTED_BRANCHES",
    "PROTECTED_TAG_PREFIXES",
    "can_auto_fast_forward",
    "can_auto_retire",
    "can_receive_direct_push",
    "can_worker_commit",
    "classify_branch",
    "is_ephemeral",
    "is_main",
    "is_protected",
    "is_protected_tag",
    "normalize_branch",
    "policy_digest",
]
