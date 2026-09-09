from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import portfolio_file as investment_manager_core


class ImportSnapshotMixin:
    """Snapshot creation and digest helpers for the investment import service."""

    def _create_import_snapshot(self, source: Path) -> Path:
        try:
            return investment_manager_core.create_portfolio_file_snapshot(
                source,
                self.repository.runtime_root / "imports",
                keep=self.IMPORT_SNAPSHOT_KEEP,
            )
        except investment_manager_core.InvestmentManagerError as exc:
            raise investment_manager_core.InvestmentManagerError(
                f"無法建立匯入快照，原始檔不會被長時間鎖定。請確認檔案可讀取後再試：{exc}"
            ) from exc

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _portfolio_import_fingerprint(
        source_digest: str,
        parameters: dict[str, Any],
    ) -> str:
        normalized = json.dumps(
            parameters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(
            f"{source_digest}|{normalized}".encode("utf-8")
        ).hexdigest()
