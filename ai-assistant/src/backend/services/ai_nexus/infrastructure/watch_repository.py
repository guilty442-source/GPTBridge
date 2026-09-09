from __future__ import annotations

from .watch_repo_helpers import (
    DATA_ROOT_ENV,
    PROFILE_ENV,
    INVESTMENT_APP_VERSION,
    INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION,
    INVESTMENT_STATE_SCHEMA_VERSION,
    InvestmentStateRecoveryRequired,
    InvestmentStateUpgradeRequired,
    _atomic_write_bytes,
    _copy_verified,
    _fsync_directory,
    _is_link_or_reparse,
    _iter_migration_files,
    _lexical_absolute,
    _normalized_profile,
    _pid_is_alive,
    _runtime_root,
    _same_path_identity,
    _try_lock_descriptor,
    _unlock_descriptor,
    _validated_storage_path,
    decode_json_document,
    encode_json_document,
    utc_now,
)
from .watch_repo_lock import _RuntimeOwnerLock
from .watch_repo_utils import WatchRepoUtilsMixin
from .watch_repo_core import WatchRepoCoreMixin
from .watch_repo_portfolio import WatchRepoPortfolioMixin
from .watch_repo_xingcheng import WatchRepoXingchengMixin
from .watch_repo_versions import WatchRepoVersionsMixin
from .watch_repo_ai_runs import WatchRepoAIRunsMixin
from .watch_repo_shared import WatchRepoSharedMixin


class InvestmentWatchRepository(
    WatchRepoCoreMixin,
    WatchRepoPortfolioMixin,
    WatchRepoXingchengMixin,
    WatchRepoVersionsMixin,
    WatchRepoAIRunsMixin,
    WatchRepoSharedMixin,
    WatchRepoUtilsMixin,
):
    """Durable investment-watch state repository.

    Composed from focused mixins that handle portfolio import, xingcheng
    analysis, state versioning, AI-run tracking, shared memory, and utility
    helpers.  See the individual ``watch_repo_*`` modules for each concern.
    """


__all__ = [
    "InvestmentWatchRepository",
    "InvestmentStateRecoveryRequired",
    "InvestmentStateUpgradeRequired",
    "_RuntimeOwnerLock",
    "_iter_migration_files",
    "_validated_storage_path",
    "utc_now",
    "DATA_ROOT_ENV",
    "PROFILE_ENV",
    "INVESTMENT_APP_VERSION",
    "INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION",
    "INVESTMENT_STATE_SCHEMA_VERSION",
    "decode_json_document",
    "encode_json_document",
]
