"""Task -> Git domain routing (task §46/§47/§57).

Routing maps touched paths to an integration domain — it decides
*base branch / integration domain only*, never product or program
content.  ``preferred_domain_owner`` is an advisory hint for
allocation, not an exclusive lock: real write authority still comes
from claims + governance.

A task spanning multiple domains must not become one giant branch:
``split_cross_domain`` produces a parent task with one child per
domain, each child getting its own worker/branch/queue (§47).  The
parent completes only when every child is MERGED and audit-passed.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Optional


#: path glob -> integration domain (base branch).  Order matters:
#: first match wins, most specific patterns first.
DOMAIN_ROUTES: tuple[tuple[str, str], ...] = (
    ("governance_rule/execution/git_tiers/**", "git"),
    ("governance_rule/git-hooks/**", "git"),
    ("scripts/git-*", "git"),
    ("local-model/**", "local-model"),
    ("rag/**", "rag"),
    ("main-system/src-ui/**", "ui"),
    ("main-system/ui/**", "ui"),
)

#: Advisory ownership hints (§57) — routing metadata, not a lock.
PREFERRED_DOMAIN_OWNER: dict[str, str] = dict(DOMAIN_ROUTES)

CROSS_DOMAIN = "cross-domain"


def route_path(path: str) -> Optional[str]:
    """Domain for one repo-relative path, or None if unrouted."""
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    for pattern, domain in DOMAIN_ROUTES:
        if fnmatch.fnmatch(normalized, pattern) or normalized.startswith(
            pattern.rstrip("*")
        ):
            return domain
    return None


def route_task(paths: list[str]) -> str:
    """Single domain for a task's path set.

    Unrouted tasks land on ``cross-domain`` — they must be split or
    explicitly assigned; they never fall back to ``main``.
    """
    domains = {route_path(p) for p in paths}
    domains.discard(None)
    if len(domains) == 1:
        return domains.pop()
    return CROSS_DOMAIN


@dataclass(frozen=True)
class ChildTask:
    task_id: str
    domain: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class TaskPlan:
    """Parent task with one child per domain (§47)."""

    parent_task_id: str
    children: tuple[ChildTask, ...]

    @property
    def is_cross_domain(self) -> bool:
        return len(self.children) > 1


def split_cross_domain(
    task_id: str, paths: list[str]
) -> TaskPlan:
    """Split a task's paths into per-domain child tasks.

    A single-domain task still returns one child — the caller can
    treat both cases uniformly.  Unrouted paths form their own
    ``cross-domain`` child so nothing is silently dropped.
    """
    grouped: dict[str, list[str]] = {}
    for path in paths:
        domain = route_path(path) or CROSS_DOMAIN
        grouped.setdefault(domain, []).append(path)
    children = tuple(
        ChildTask(
            task_id=f"{task_id}/child/{domain}",
            domain=domain,
            paths=tuple(sorted(group)),
        )
        for domain, group in sorted(grouped.items())
    )
    return TaskPlan(parent_task_id=task_id, children=children)


def preferred_owner(path: str) -> Optional[str]:
    """Advisory domain owner hint (§57) — never an exclusive lock."""
    return route_path(path)


__all__ = [
    "CROSS_DOMAIN",
    "DOMAIN_ROUTES",
    "PREFERRED_DOMAIN_OWNER",
    "ChildTask",
    "TaskPlan",
    "preferred_owner",
    "route_path",
    "route_task",
    "split_cross_domain",
]
