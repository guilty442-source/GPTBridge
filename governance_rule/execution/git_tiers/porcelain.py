"""Single parser for ``git status --porcelain=v2 -z --branch`` (A375 HEALTH).

All programmatic status reads in git_tiers go through this parser — stable,
machine-readable, rename-aware, branch-metadata aware, and safe for special
characters in filenames (``-z`` terminates entries with NUL and does not
quote paths).

Entry grammar (v2, ``-z`` mode):

    # branch.oid <sha>            header
    # branch.head <name>          header ("(detached)" when detached)
    # branch.upstream <name>      header
    # branch.ab +<ahead> -<behind> header
    1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>\0
    2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path>\0<origPath>\0
    u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>\0
    ? <path>\0
    ! <path>\0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True)
class StatusEntry:
    """One changed-path record from porcelain v2."""

    kind: str  # "1" ordinary, "2" rename/copy, "u" unmerged, "?" untracked, "!" ignored
    xy: str
    path: str
    orig_path: str = ""

    @property
    def staged(self) -> bool:
        return self.xy[:1] not in (".", "?", "!") and self.kind != "?"

    @property
    def unstaged(self) -> bool:
        return self.xy[1:2] not in (".", "", "?", "!") or self.kind == "u"


@dataclass(frozen=True)
class WorktreeStatus:
    """Parsed porcelain v2 status."""

    branch: str = ""
    head_oid: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    detached: bool = False
    entries: tuple[StatusEntry, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.entries

    def changed_paths(self) -> tuple[str, ...]:
        """All touched paths (rename sources and targets both count)."""
        paths: list[str] = []
        for entry in self.entries:
            paths.append(entry.path)
            if entry.orig_path:
                paths.append(entry.orig_path)
        return tuple(paths)

    def legacy_map(self) -> dict[str, str]:
        """``{path: XY}`` map compatible with the old porcelain v1 callers."""
        return {entry.path: entry.xy for entry in self.entries}


def parse_porcelain_v2_z(text: str) -> WorktreeStatus:
    """Parse ``git status --porcelain=v2 -z --branch`` output."""
    branch = ""
    head_oid = ""
    upstream = ""
    ahead = 0
    behind = 0
    detached = False
    entries: list[StatusEntry] = []
    records = text.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("# "):
            header = record[2:]
            if header.startswith("branch.oid "):
                head_oid = header[len("branch.oid "):].strip()
            elif header.startswith("branch.head "):
                branch = header[len("branch.head "):].strip()
                detached = branch == "(detached)"
            elif header.startswith("branch.upstream "):
                upstream = header[len("branch.upstream "):].strip()
            elif header.startswith("branch.ab "):
                parts = header[len("branch.ab "):].split()
                for part in parts:
                    if part.startswith("+"):
                        ahead = int(part[1:] or 0)
                    elif part.startswith("-"):
                        behind = int(part[1:] or 0)
            continue
        tag = record[:1]
        if tag == "1":
            fields = record.split(" ", 8)
            if len(fields) == 9:
                entries.append(StatusEntry("1", fields[1], fields[8]))
        elif tag == "2":
            fields = record.split(" ", 9)
            if len(fields) == 10 and index < len(records):
                orig = records[index]
                index += 1
                entries.append(StatusEntry("2", fields[1], fields[9], orig))
        elif tag == "u":
            fields = record.split(" ", 10)
            if len(fields) == 11:
                entries.append(StatusEntry("u", fields[1], fields[10]))
        elif tag == "?":
            entries.append(StatusEntry("?", "??", record[2:]))
        elif tag == "!":
            entries.append(StatusEntry("!", "!!", record[2:]))
    return WorktreeStatus(
        branch=branch,
        head_oid=head_oid,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        detached=detached,
        entries=tuple(entries),
    )


def status_v2(repo, *, include_branch: bool = True) -> WorktreeStatus:
    """Run the governed status command on ``repo`` and parse it."""
    args = ["status", "--porcelain=v2", "-z"]
    if include_branch:
        args.append("--branch")
    result = repo.run(args)
    return parse_porcelain_v2_z(result.stdout or "")


__all__ = [
    "StatusEntry",
    "WorktreeStatus",
    "parse_porcelain_v2_z",
    "status_v2",
]
