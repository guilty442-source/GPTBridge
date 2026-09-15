from __future__ import annotations

from .import_excel import ImportExcelMixin
from .import_service_files import ImportFilesMixin
from .import_service_operations import ImportOperationsMixin
from .import_service_reports import ImportReportsMixin
from .import_snapshot import ImportSnapshotMixin


class InvestmentImportServiceMixin(
    ImportOperationsMixin,
    ImportFilesMixin,
    ImportReportsMixin,
    ImportExcelMixin,
    ImportSnapshotMixin,
):
    """Lifecycle management for investment portfolio import operations."""


__all__ = ["InvestmentImportServiceMixin"]
