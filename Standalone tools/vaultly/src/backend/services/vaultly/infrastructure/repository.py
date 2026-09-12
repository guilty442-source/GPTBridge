from __future__ import annotations

from ._accounts import AccountMixin
from ._downloads import DownloadMixin
from ._entity_history import EntityHistoryMixin
from ._filter_terms import FilterTermsMixin
from ._jobs import JobMixin
from ._post_scan_jobs import PostScanJobMixin
from ._posts import PostMixin
from ._removed_accounts import RemovedAccountsMixin
from ._retained_accounts import RetainedAccountsMixin
from ._schema import SchemaMixin
from ._selection import SelectionMixin
from ._settings import SettingsMixin


class VaultlyRepository(
    SchemaMixin,
    EntityHistoryMixin,
    AccountMixin,
    PostMixin,
    PostScanJobMixin,
    SelectionMixin,
    FilterTermsMixin,
    RetainedAccountsMixin,
    RemovedAccountsMixin,
    SettingsMixin,
    JobMixin,
    DownloadMixin,
):
    pass
