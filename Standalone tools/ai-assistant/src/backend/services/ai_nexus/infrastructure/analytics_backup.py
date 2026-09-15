from __future__ import annotations

from .analytics_backup_manage import BackupManageMixin
from .analytics_backup_restore import BackupRestoreMixin
from .analytics_backup_write import BackupWriteMixin


class BackupMixin(BackupWriteMixin, BackupManageMixin, BackupRestoreMixin):
    """Backup, restore, and database security methods."""
