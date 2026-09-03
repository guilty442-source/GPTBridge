from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from governance_rule.permission_directory.execution import path_guard  # noqa: E402


@pytest.fixture
def project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(path_guard, "GPTBRIDGE_PROJECT_ROOT", root.as_posix())
    return root


@pytest.mark.parametrize(
    "relative_path",
    (
        "../outside",
        "tool//runtime",
        "tool\\runtime",
        "C:/outside",
        "tool/runtime/state.db:alternate",
        "tool/runtime/../authority",
        "\0invalid",
    ),
)
def test_resolve_project_path_rejects_noncanonical_paths(
    project_root: Path,
    relative_path: str,
) -> None:
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        path_guard.resolve_project_path(project_root, relative_path)


def test_independent_tool_root_rejects_noncanonical_project_root(
    project_root: Path,
    tmp_path: Path,
) -> None:
    unrelated_root = tmp_path / "unrelated"
    unrelated_root.mkdir()

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        path_guard.independent_tool_root(unrelated_root, "xingcheng")


def test_resolve_project_path_accepts_canonical_root_marker(
    project_root: Path,
) -> None:
    assert path_guard.resolve_project_path(project_root, ".") == project_root


def test_resolve_project_path_rejects_working_directory_relative_root(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(project_root.parent)

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        path_guard.resolve_project_path(Path(project_root.name), ".")


@pytest.mark.parametrize("invalid_root", (None, object()))
def test_resolve_project_path_rejects_invalid_root_type(
    invalid_root: object,
) -> None:
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        path_guard.resolve_project_path(invalid_root, ".")  # type: ignore[arg-type]


def test_resolve_project_path_rejects_hardlink_alias(
    project_root: Path,
) -> None:
    protected = project_root / "governance_rule" / "authority.py"
    protected.parent.mkdir()
    protected.write_text("authority", encoding="utf-8")
    alias = project_root / "xingcheng" / "runtime" / "settings" / "alias.py"
    alias.parent.mkdir(parents=True)
    os.link(protected, alias)

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        path_guard.resolve_project_path(
            project_root,
            "xingcheng/runtime/settings/alias.py",
        )


def test_grant_validation_rejects_hardlink_to_authority_file(
    project_root: Path,
) -> None:
    protected = project_root / "governance_rule" / "governance_policy.py"
    protected.parent.mkdir()
    protected.write_text("authority", encoding="utf-8")
    alias = project_root / "xingcheng" / "runtime" / "settings" / "policy.py"
    alias.parent.mkdir(parents=True)
    os.link(protected, alias)
    grant = SimpleNamespace(
        path_match="within",
        path_roots=("xingcheng/runtime/settings",),
        excluded_path_roots=("governance_rule",),
    )

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        path_guard.validate_grant_resource_path(
            project_root,
            "xingcheng/runtime/settings/policy.py",
            grant,
            "xingcheng",
        )


def test_resolve_project_path_rejects_symbolic_link(
    project_root: Path,
) -> None:
    target = project_root / "target"
    target.mkdir()
    link = project_root / "xingcheng" / "runtime-link"
    link.parent.mkdir()
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        path_guard.resolve_project_path(project_root, "xingcheng/runtime-link")
