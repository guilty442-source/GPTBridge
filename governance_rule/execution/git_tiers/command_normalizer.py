"""Git command canonicalization and digest (single approval/execution source).

Tier governance binds an approval to the *exact* command it authorizes.  A
capability issued for ``git commit -m "x"`` must not be reusable for
``git commit -m "y"``, but it also must not break when the same command is
written with a different spelling (alias, short/long flag, option order,
path/ref spelling).  Both the approval path and the execution path therefore
call the one implementation here and compare ``command_digest()`` values.

Canonical form (deterministic, no user git config consulted):

    git [global options] <subcommand> [long options sorted] \\
        [positionals in order] [-- paths]

Rules:
  * bounded builtin-alias expansion (``st`` -> ``status``, ...);
  * bounded short-flag expansion to long form (``-f`` -> ``--force``);
  * value options normalize to ``--option=value`` on one token;
  * options sort; positionals and ``--`` paths keep caller order;
  * ref-ish positionals expand ``heads/x`` -> ``refs/heads/x`` and hex
    object names casefold;
  * paths after ``--`` use forward slashes with ``./`` collapsed.

Unknown flags pass through unchanged.  Classification is deliberately NOT
performed here: the tier lists in ``git_tiers`` stay authoritative and are
consulted on the raw command (normalization must never move a command
between tiers).
"""
from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "DIGEST_DOMAIN",
    "GIT_ALIASES",
    "NormalizedCommand",
    "SUBACTION_SUBCOMMANDS",
    "command_args",
    "command_digest",
    "normalize_command",
    "operation_key",
]

DIGEST_DOMAIN = b"gitbridge-git-command-v1\0"

# Old builtin aliases (git-config "old-style script aliases" plus the common
# short forms).  Deterministic, bounded, never read from user config.
GIT_ALIASES: dict[str, str] = {
    "ci": "commit",
    "co": "checkout",
    "br": "branch",
    "st": "status",
    "df": "diff",
    "dc": "diff --cached",
    "dfc": "diff --cached",
    "dl": "diff HEAD",
    "lg": "log --oneline",
    "last": "log -1 HEAD",
    "unstage": "reset HEAD --",
}

_GLOBAL_OPTS_WITH_VALUE = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)
_GLOBAL_FLAGS = frozenset(
    {
        "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
        "--icase-pathspecs", "--no-optional-locks", "--no-pager",
        "-P", "-p", "--bare", "--no-replace-objects", "--version", "--help",
    }
)

# Generic value-taking long options: ``--opt value`` -> ``--opt=value``.
_VALUE_OPTIONS: frozenset[str] = frozenset(
    {
        "--message", "--file", "--author", "--date", "--max-count",
        "--format", "--pretty", "--grep", "--strategy", "--strategy-option",
        "--sort", "--contains", "--points-at", "--exclude", "--ref",
        "--onto", "--prefix", "--source", "--mainline", "--reuse-message",
        "--reedit-message", "--git-dir", "--work-tree", "--namespace",
        "--exec-path",
    }
)

# Bounded per-subcommand short-flag expansion.  Maps short -> canonical long
# token; ``None`` value means the long token takes a following value.
_SHORT_FLAGS: dict[str, dict[str, str]] = {
    "add": {"-A": "--all", "-u": "--update", "-p": "--patch",
            "-n": "--dry-run", "-f": "--force", "-v": "--verbose",
            "-i": "--interactive"},
    "branch": {"-d": "--delete", "-D": "--delete --force", "-m": "--move",
               "-M": "--move --force", "-a": "--all", "-r": "--remotes",
               "-v": "--verbose", "-l": "--list", "-f": "--force"},
    "checkout": {"-b": "--branch", "-f": "--force", "-t": "--track",
                 "-q": "--quiet"},
    "cherry-pick": {"-n": "--no-commit", "-e": "--edit", "-x": "--record",
                    "-m": "--mainline"},
    "clean": {"-f": "--force", "-d": "--directories", "-n": "--dry-run",
              "-i": "--interactive", "-x": "--ignored", "-X": "--ignored-only"},
    "commit": {"-m": "--message", "-a": "--all", "-F": "--file",
               "-C": "--reuse-message", "-c": "--reedit-message",
               "-n": "--no-verify", "-e": "--edit", "-s": "--signoff",
               "-v": "--verbose", "--amend": "--amend"},
    "config": {"-l": "--list", "-e": "--edit", "-f": "--file"},
    "fetch": {"-f": "--force", "-t": "--tags", "-p": "--prune",
              "-a": "--all", "-v": "--verbose"},
    "log": {"-n": "--max-count", "-p": "--patch", "-i": "--regexp-ignore-case",
            "-E": "--extended-regexp"},
    "merge": {"-m": "--message", "-n": "--no-commit", "--abort": "--abort",
              "--continue": "--continue"},
    "mv": {"-f": "--force", "-k": "--skip"},
    "notes": {"-f": "--force"},
    "push": {"-f": "--force", "-u": "--set-upstream", "-n": "--dry-run",
             "-d": "--delete", "-v": "--verbose", "-q": "--quiet"},
    "rebase": {"-i": "--interactive", "-f": "--force-rebase",
               "-q": "--quiet", "-v": "--verbose", "--root": "--root"},
    "reflog": {"--expire": "--expire"},
    "replace": {"-f": "--force", "-d": "--delete", "-l": "--list",
                "-e": "--edit"},
    "reset": {"-q": "--quiet"},
    "restore": {"-p": "--patch", "-s": "--source", "-S": "--staged",
                "-W": "--worktree"},
    "revert": {"-n": "--no-commit", "-m": "--mainline", "-e": "--edit"},
    "rm": {"-r": "--recursive", "-f": "--force"},
    "stash": {"-u": "--include-untracked", "-k": "--keep-index",
              "-a": "--all", "-p": "--patch", "-q": "--quiet"},
    "switch": {"-c": "--create", "-f": "--discard-changes"},
    "tag": {"-l": "--list", "-d": "--delete", "-a": "--annotate",
            "-m": "--message", "-f": "--force", "-s": "--sign", "-n": "--format"},
    "update-ref": {"-d": "--delete", "-m": "--message"},
    "worktree": {"-f": "--force", "-b": "--branch"},
}

_REF_PREFIXES = (
    ("heads/", "refs/heads/"),
    ("tags/", "refs/tags/"),
    ("remotes/", "refs/remotes/"),
)
_HEX = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REF_LIKE = re.compile(r"^(refs/|heads/|tags/|remotes/|origin/|HEAD$|\^)")


def command_args(command: str | Sequence[str]) -> list[str]:
    """Split a command string or pass a sequence through unchanged.

    ``posix=False`` keeps Windows backslash paths intact (``posix=True``
    would eat ``\\`` as an escape); surrounding quotes are stripped after
    splitting so ``-m "two words"`` stays one value.
    """
    if isinstance(command, (list, tuple)):
        return [str(item) for item in command]
    lexer = shlex.shlex(str(command), posix=False)
    lexer.whitespace_split = True
    return [token.strip("\"'") for token in lexer]


def _is_option(token: str) -> bool:
    return token.startswith("-") and token not in ("-", "--")


def _canonical_path(token: str) -> str:
    text = token.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _canonical_ref(token: str) -> str:
    for short, full in _REF_PREFIXES:
        if token.startswith(short):
            return full + token[len(short):]
    if _HEX.match(token) or _REF_LIKE.match(token):
        return token.lower() if _HEX.match(token) else token
    return token


# Subcommands whose first positional is a sub-action keyword (``worktree
# add``, ``stash push``, ``bundle create``, ...).  The operation key keeps
# that keyword so the capability whitelist can allow ``worktree add`` while
# still refusing ``worktree remove``.
SUBACTION_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "worktree", "stash", "bundle", "remote", "commit-graph",
        "multi-pack-index", "maintenance", "sparse-checkout", "submodule",
        "notes", "reflog",
    }
)


@dataclass(frozen=True)
class NormalizedCommand:
    """Canonical git command with its approval/execution digest."""

    text: str
    subcommand: str
    global_options: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    positionals: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    alias_from: str = ""
    digest: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "subcommand": self.subcommand,
            "global_options": list(self.global_options),
            "options": list(self.options),
            "positionals": list(self.positionals),
            "paths": list(self.paths),
            "alias_from": self.alias_from,
            "digest": self.digest,
        }

    def operation(self) -> str:
        """Operation key: subcommand + sub-action + option names.

        Values are excluded so ``git commit -m a`` and ``git commit -m b``
        share an operation key while keeping distinct command digests.
        """
        names = [opt.split("=", 1)[0] for opt in self.options]
        parts = [self.subcommand]
        if (
            self.subcommand in SUBACTION_SUBCOMMANDS
            and self.positionals
            and not self.positionals[0].startswith("-")
        ):
            parts.append(self.positionals[0])
        parts.extend(names)
        return " ".join(part for part in parts if part).strip()


def _split_global(tokens: list[str]) -> tuple[list[str], list[str], str]:
    """Return (global_options, remainder, alias_source)."""
    index = 0
    globals_: list[str] = []
    if tokens and tokens[0] == "git":
        index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _GLOBAL_OPTS_WITH_VALUE and index + 1 < len(tokens):
            globals_.extend([token, _canonical_path(tokens[index + 1])])
            index += 2
            continue
        if any(token.startswith(opt + "=") for opt in _GLOBAL_OPTS_WITH_VALUE):
            globals_.append(token)
            index += 1
            continue
        if token in _GLOBAL_FLAGS:
            globals_.append(token)
            index += 1
            continue
        break
    alias_from = ""
    if index < len(tokens) and tokens[index] in GIT_ALIASES:
        alias_from = tokens[index]
        expanded = command_args(GIT_ALIASES[alias_from])
        return globals_, [*expanded, *tokens[index + 1:]], alias_from
    return globals_, tokens[index:], alias_from


def _normalize_option(
    option: str, subcommand: str, pending: str | None
) -> tuple[str, str | None]:
    """Canonicalize one option token; returns (token, pending_value_option)."""
    if pending is not None:
        return f"{pending}={option}", None
    if option.startswith("--"):
        name = option.split("=", 1)[0]
        if "=" not in option and name in _VALUE_OPTIONS:
            return option, name
        return option, None
    mapped = _SHORT_FLAGS.get(subcommand, {}).get(option)
    if mapped is None:
        return option, None
    if " " in mapped:  # e.g. -D -> "--delete --force"
        return mapped, None
    if mapped in _VALUE_OPTIONS:
        return mapped, mapped
    return mapped, None


def _partition(tokens: list[str], subcommand: str) -> tuple[list[str], list[str], list[str]]:
    options: list[str] = []
    positionals: list[str] = []
    paths: list[str] = []
    pending: str | None = None
    after_separator = False
    for token in tokens:
        if after_separator:
            paths.append(_canonical_path(token))
            continue
        if token == "--":
            after_separator = True
            continue
        if not pending and _is_option(token):
            canonical, pending = _normalize_option(token, subcommand, None)
            options.append(canonical)
            continue
        if pending is not None:
            options[-1] = f"{options[-1].split('=', 1)[0]}"
            options[-1] = _normalize_option(token, subcommand, pending)[0]
            pending = None
            continue
        positionals.append(_canonical_ref(_canonical_path(token)) if "/" in token else token)
    if pending is not None:  # flag left without value: keep bare
        options[-1] = options[-1].split("=", 1)[0]
    return options, positionals, paths


def _render(
    globals_: Iterable[str], subcommand: str, options: Iterable[str],
    positionals: Iterable[str], paths: Iterable[str],
) -> str:
    parts = ["git", *globals_, subcommand, *sorted(set(options)), *positionals]
    paths = list(paths)
    if paths:
        parts.extend(["--", *paths])
    return " ".join(part for part in parts if part != "")


def _digest_of(text: str) -> str:
    return hashlib.sha256(DIGEST_DOMAIN + text.encode("utf-8")).hexdigest()


def normalize_command(command: str | Sequence[str]) -> NormalizedCommand:
    """Canonicalize a git command for digest binding."""
    tokens = command_args(command)
    globals_, remainder, alias_from = _split_global(tokens)
    subcommand = remainder[0].casefold() if remainder else ""
    options, positionals, paths = _partition(remainder[1:], subcommand)
    text = _render(globals_, subcommand, options, positionals, paths)
    return NormalizedCommand(
        text=text,
        subcommand=subcommand,
        global_options=tuple(globals_),
        options=tuple(sorted(set(options))),
        positionals=tuple(positionals),
        paths=tuple(paths),
        alias_from=alias_from,
        digest=_digest_of(text),
    )


def command_digest(command: str | Sequence[str]) -> str:
    """SHA-256 over the canonical command — shared by issue and execute."""
    return normalize_command(command).digest


def operation_key(command: str | Sequence[str]) -> str:
    """Operation key used for capability scope/operation binding."""
    return normalize_command(command).operation()
