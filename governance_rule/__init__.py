from .governance_policy import governance_policy_snapshot
from .permission_directory import directory_authority_snapshot


def __getattr__(name: str):
    """Lazy compatibility export (A279/A435).

    ``GOVERNANCE_CODEX`` is resolved on demand through the governed
    repository interface — importing ``governance_rule`` no longer
    performs an eager codex read.  Runtime viewers must enter through
    ``governance-codex://official`` (``codex_official``/``codex_session``).
    """
    if name == "GOVERNANCE_CODEX":
        from governance_rule.execution.codex_repository import (
            load_governance_codex,
        )

        return load_governance_codex()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = (
    "GOVERNANCE_CODEX",
    "governance_policy_snapshot",
    "directory_authority_snapshot",
)
