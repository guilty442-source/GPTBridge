"""Governed Git hook versioning, digest verification and rolling upgrade.

Every governed hook template under ``governance_rule/git-hooks/`` declares
``HOOK_VERSION = <n>`` metadata.  This module provides:

* ``expected_hook_version`` — the governance contract version per hook.
* ``actual_hook_version`` / ``hook_version`` — parsed from a file.
* ``hook_digest`` / ``verify_hook_digest`` / ``verify_hook`` — sha256 and
  version verification against an installed hook or template.
* ``hook_generation`` / ``current_hook_generation`` — a content-addressed
  generation token a Git transaction can record (bound into audit records
  by ``audit_records.stamp_audit_record``).
* ``upgrade_hook`` — the rolling upgrade flow
  prepare -> syntax test -> stage -> critical-section wait -> atomic
  replace -> digest verify -> activate, with automatic restoration of the
  previous hook on any post-replacement failure.

The upgrade function is a governed staging/verification tool: it operates
only on explicit source/target paths and never touches the live
``.git/hooks`` directory on its own.  Live replacement remains a separate
governed action.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

HOOK_NAMES: tuple[str, ...] = (
    "pre-commit",
    "pre-merge-commit",
    "pre-push",
    "pre-receive",
)

EXPECTED_HOOK_VERSIONS: Mapping[str, int] = {
    "pre-commit": 1,
    "pre-merge-commit": 1,
    "pre-push": 1,
    "pre-receive": 1,
}

HOOK_VERSION_MARKER = "HOOK_VERSION"
HOOKS_DIR_ENV = "GPTBRIDGE_HOOKS_DIR"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "git-hooks"

_HOOK_VERSION_PATTERN = re.compile(
    r"^[ \t]*HOOK_VERSION[ \t]*=[ \t]*(\d+)[ \t]*\r?$", re.MULTILINE
)


class HookVersioningError(RuntimeError):
    """Hook metadata, digest or upgrade verification failure."""


# ---------------------------------------------------------------------------
# metadata parsing / digest
# ---------------------------------------------------------------------------


def parse_hook_version(text: str) -> int | None:
    """Return the declared ``HOOK_VERSION`` or ``None`` when absent."""
    match = _HOOK_VERSION_PATTERN.search(text)
    return int(match.group(1)) if match else None


def hook_version(path: str | Path) -> int | None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_hook_version(text)


def actual_hook_version(path: str | Path) -> int | None:
    """Version currently installed at ``path`` (alias of ``hook_version``)."""
    return hook_version(path)


def expected_hook_version(hook_name: str) -> int:
    try:
        return int(EXPECTED_HOOK_VERSIONS[hook_name])
    except KeyError as error:
        raise HookVersioningError(f"unknown governed hook: {hook_name}") from error


def hook_digest(path: str | Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as error:
        raise HookVersioningError(f"hook is unreadable: {path}: {error}") from error


def verify_hook_digest(path: str | Path, expected_digest: str) -> bool:
    if not expected_digest:
        return False
    try:
        return hook_digest(path) == expected_digest
    except HookVersioningError:
        return False


def verify_hook(
    path: str | Path,
    *,
    hook_name: str | None = None,
    expected_digest: str | None = None,
    min_version: int | None = None,
) -> dict[str, Any]:
    """Verify one hook's existence, declared version and (optionally) digest."""
    hook_path = Path(path)
    exists = hook_path.is_file()
    version = hook_version(hook_path) if exists else None
    digest = ""
    if exists:
        try:
            digest = hook_digest(hook_path)
        except HookVersioningError:
            digest = ""
    required = min_version
    expected = None
    if hook_name is not None:
        expected = expected_hook_version(hook_name)
        required = expected if required is None else max(required, expected)
    if not exists:
        version_ok = False
    elif required is None:
        version_ok = version is not None
    else:
        version_ok = version is not None and version >= required
    digest_ok = True if expected_digest is None else digest == expected_digest
    return {
        "hook": str(hook_path),
        "exists": exists,
        "version": version,
        "expected_version": expected,
        "required_version": required,
        "digest": digest,
        "expected_digest": expected_digest or "",
        "version_ok": version_ok,
        "digest_ok": digest_ok,
        "ok": exists and version_ok and digest_ok,
    }


def verify_governed_templates(
    template_dir: str | Path = TEMPLATE_DIR,
) -> dict[str, Any]:
    """Verify every governed template declares its expected HOOK_VERSION."""
    directory = Path(template_dir)
    hooks: dict[str, Any] = {}
    for name in HOOK_NAMES:
        hooks[name] = verify_hook(directory / name, hook_name=name)
    return {
        "template_dir": str(directory),
        "hooks": hooks,
        "ok": all(report["ok"] for report in hooks.values()),
    }


# ---------------------------------------------------------------------------
# hook generation
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hook_generation(
    hooks_dir: str | Path, *, names: Iterable[str] = HOOK_NAMES
) -> str:
    """Content-addressed generation token for the hooks present in a dir."""
    directory = Path(hooks_dir)
    rows: list[dict[str, Any]] = []
    for name in sorted(names):
        path = directory / name
        if not path.is_file():
            continue
        rows.append(
            {
                "hook": name,
                "version": hook_version(path) or 0,
                "sha256": _sha256_file(path),
            }
        )
    if not rows:
        return "none"
    canonical = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "hook-gen-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_hooks_dir(root: str | Path | None = None) -> Path:
    """Resolve the git hooks directory (env override -> worktree -> repo)."""
    override = os.environ.get(HOOKS_DIR_ENV, "").strip()
    if override:
        return Path(override)
    base = Path(root) if root is not None else _PROJECT_ROOT
    git_entry = base / ".git"
    if git_entry.is_file():
        try:
            line = git_entry.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            line = ""
        if line.casefold().startswith("gitdir:"):
            git_dir = Path(line.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (base / git_dir).resolve()
            return git_dir / "hooks"
    return git_entry / "hooks"


def current_hook_generation(
    *, hooks_dir: str | Path | None = None, root: str | Path | None = None
) -> str:
    """Generation token of the hook set currently in effect."""
    directory = Path(hooks_dir) if hooks_dir is not None else resolve_hooks_dir(root)
    return hook_generation(directory)


# ---------------------------------------------------------------------------
# rolling upgrade
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookUpgradeResult:
    ok: bool
    hook_name: str
    source: str
    target: str
    version: int | None
    expected_version: int | None
    digest: str
    previous_digest: str
    steps: tuple[str, ...]
    failed_step: str = ""
    restored: bool = False
    error: str = ""


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _chmod_hook(path: Path) -> None:
    try:
        current = path.stat().st_mode
        path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _restore_hook(target: Path, old_bytes: bytes | None) -> bool:
    """Restore previous hook content (or remove a newly-created hook)."""
    try:
        if old_bytes is None:
            if target.exists():
                target.unlink()
        else:
            _atomic_write(target, old_bytes)
            _chmod_hook(target)
        return True
    except OSError:
        return False


def upgrade_hook(
    source: str | Path,
    target: str | Path,
    *,
    hook_name: str | None = None,
    expected_digest: str | None = None,
    min_version: int | None = None,
    wait_for_quiescence: Callable[[], None] | None = None,
    activate: Callable[[Path], None] | None = None,
    check_syntax: bool = True,
    stage_suffix: str = ".staged",
) -> HookUpgradeResult:
    """Run the rolling upgrade flow for one hook file.

    ``source`` is the certified candidate, ``target`` the hook file to
    replace.  The current hook bytes are captured before the atomic
    replace; any failure in replace/verify/activate restores them.  The
    caller controls entry into the critical section via
    ``wait_for_quiescence`` (invoked after staging, before replacement).
    """
    source_path = Path(source)
    target_path = Path(target)
    resolved_name = hook_name or target_path.name
    steps: list[str] = []
    version: int | None = None
    expected_version: int | None = None
    digest = ""
    previous_digest = ""
    old_bytes: bytes | None = None
    staged = target_path.with_name(target_path.name + stage_suffix)

    def result(
        ok: bool,
        *,
        failed_step: str = "",
        restored: bool = False,
        error: str = "",
    ) -> HookUpgradeResult:
        return HookUpgradeResult(
            ok=ok,
            hook_name=resolved_name,
            source=str(source_path),
            target=str(target_path),
            version=version,
            expected_version=expected_version,
            digest=digest,
            previous_digest=previous_digest,
            steps=tuple(steps),
            failed_step=failed_step,
            restored=restored,
            error=error,
        )

    def discard_staged() -> None:
        try:
            staged.unlink()
        except OSError:
            pass

    if hook_name is not None:
        expected_version = expected_hook_version(hook_name)

    # prepare: read candidate bytes, enforce shebang / digest / version contract
    steps.append("prepare")
    try:
        raw = source_path.read_bytes()
    except OSError as error:
        return result(False, failed_step="prepare", error=f"candidate unreadable: {error}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        return result(False, failed_step="prepare", error=f"candidate is not UTF-8: {error}")
    if not text.startswith("#!"):
        return result(False, failed_step="prepare", error="candidate has no shebang")
    digest = hashlib.sha256(raw).hexdigest()
    version = parse_hook_version(text)
    if version is None:
        return result(False, failed_step="prepare", error="candidate declares no HOOK_VERSION")
    if expected_digest and digest != expected_digest:
        return result(
            False, failed_step="prepare", error="candidate digest mismatch"
        )
    if expected_version is not None and version < expected_version:
        return result(
            False,
            failed_step="prepare",
            error=f"candidate version {version} below expected {expected_version}",
        )
    if min_version is not None and version < min_version:
        return result(
            False,
            failed_step="prepare",
            error=f"candidate version {version} below required {min_version}",
        )

    # syntax test: the hook must be parseable before it can be staged
    if check_syntax:
        steps.append("syntax")
        try:
            ast.parse(text)
        except SyntaxError as error:
            return result(False, failed_step="syntax", error=f"candidate syntax error: {error}")

    # stage: write the candidate beside the target (target untouched)
    steps.append("stage")
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(staged, raw)
    except OSError as error:
        discard_staged()
        return result(False, failed_step="stage", error=f"staging failed: {error}")

    # critical-section wait: caller-controlled quiescence gate
    steps.append("quiescence")
    if wait_for_quiescence is not None:
        try:
            wait_for_quiescence()
        except Exception as error:  # noqa: BLE001 - quiescence must fail closed
            discard_staged()
            return result(False, failed_step="quiescence", error=f"quiescence failed: {error}")

    # backup: capture current hook bytes for restoration
    steps.append("backup")
    try:
        if target_path.is_file():
            old_bytes = target_path.read_bytes()
            previous_digest = hashlib.sha256(old_bytes).hexdigest()
    except OSError as error:
        discard_staged()
        return result(False, failed_step="backup", error=f"previous hook unreadable: {error}")

    # atomic replace
    steps.append("replace")
    try:
        os.replace(staged, target_path)
        _chmod_hook(target_path)
    except OSError as error:
        discard_staged()
        return result(False, failed_step="replace", error=f"atomic replace failed: {error}")

    # verify digest + version on the live target
    steps.append("verify")
    try:
        installed = target_path.read_bytes()
    except OSError as error:
        restored = _restore_hook(target_path, old_bytes)
        return result(
            False,
            failed_step="verify",
            restored=restored,
            error=f"installed hook unreadable: {error}",
        )
    if hashlib.sha256(installed).hexdigest() != digest:
        restored = _restore_hook(target_path, old_bytes)
        return result(
            False, failed_step="verify", restored=restored, error="installed digest mismatch"
        )
    if parse_hook_version(installed.decode("utf-8", errors="replace")) != version:
        restored = _restore_hook(target_path, old_bytes)
        return result(
            False, failed_step="verify", restored=restored, error="installed version mismatch"
        )

    # activate: optional post-replacement governance action
    steps.append("activate")
    if activate is not None:
        try:
            activate(target_path)
        except Exception as error:  # noqa: BLE001 - activation must fail closed
            restored = _restore_hook(target_path, old_bytes)
            return result(
                False,
                failed_step="activate",
                restored=restored,
                error=f"activation failed: {error}",
            )

    return result(True)


__all__ = [
    "EXPECTED_HOOK_VERSIONS",
    "HOOKS_DIR_ENV",
    "HOOK_NAMES",
    "HOOK_VERSION_MARKER",
    "HookUpgradeResult",
    "HookVersioningError",
    "TEMPLATE_DIR",
    "actual_hook_version",
    "current_hook_generation",
    "expected_hook_version",
    "hook_digest",
    "hook_generation",
    "hook_version",
    "parse_hook_version",
    "resolve_hooks_dir",
    "upgrade_hook",
    "verify_governed_templates",
    "verify_hook",
    "verify_hook_digest",
]
