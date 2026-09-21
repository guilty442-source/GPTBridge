"""§10.9 統一 Contract 與相容性驗證（`star-contract-registry/v1`）。

載入 ``main-system/config/*-contract.json``，驗證結構（contract_version
為正整數、必要欄位存在），並提供版本軸分離的相容性判定——

**Contract Version ≠ Backend Release ≠ Frontend Release ≠ Codex Version
≠ Database Schema Version**——``check_compatibility`` 只比對 contract
軸（``contract_version`` vs ``minimum_supported_contract_version``），
不混淆其他版本軸。

任何 Manager 變更前可先 ``validate_all`` 判定是否破壞其他模組。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.contract")

REGISTRY_VERSION = "star-contract-registry/v1"


@dataclass
class ContractValidation:
    name: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    contract_version: Optional[int] = None
    minimum_supported: Optional[int] = None


@dataclass
class CompatibilityResult:
    compatible: bool
    reason: str
    contract_name: str


def _schema_version(data: dict[str, Any]) -> Optional[int]:
    """部分契約以 ``schema: ".../vN"`` 宣告版本而非 contract_version。"""
    schema = data.get("schema")
    if isinstance(schema, str) and "/v" in schema:
        try:
            return int(schema.rsplit("/v", 1)[1])
        except (ValueError, IndexError):
            return None
    return None


def _validate_contract(name: str, data: Any) -> ContractValidation:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ContractValidation(name, False, ["contract-not-an-object"])
    version = data.get("contract_version")
    if version is None:
        version = _schema_version(data)
    if not isinstance(version, int) or version < 1:
        errors.append("contract-version-invalid")
    minimum = data.get("minimum_supported_contract_version")
    if minimum is not None and (not isinstance(minimum, int) or minimum < 0):
        errors.append("minimum-supported-invalid")
    if minimum is not None and isinstance(version, int) and minimum > version:
        errors.append("minimum-exceeds-version")
    return ContractValidation(
        name,
        not errors,
        errors,
        contract_version=version if isinstance(version, int) else None,
        minimum_supported=minimum if isinstance(minimum, int) else None,
    )


class ContractRegistry:
    """載入並驗證所有 contract 定義檔。"""

    def __init__(self, config_dir: str | Path) -> None:
        self._dir = Path(config_dir)
        self._contracts: dict[str, dict[str, Any]] = {}
        self._validations: dict[str, ContractValidation] = {}
        self._load_all()

    def _load_all(self) -> None:
        for path in sorted(self._dir.glob("*-contract.json")):
            name = path.stem[: -len("-contract")] or path.stem
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._validations[name] = ContractValidation(
                    name, False, [f"unreadable:{exc.__class__.__name__}"]
                )
                continue
            self._contracts[name] = data
            self._validations[name] = _validate_contract(name, data)

    # -- validation -----------------------------------------------------------

    def validate_all(self) -> dict[str, ContractValidation]:
        """全部 contract 的結構驗證結果。"""
        return dict(self._validations)

    def validation_errors(self) -> list[str]:
        out: list[str] = []
        for name, result in self._validations.items():
            out.extend(f"{name}:{e}" for e in result.errors)
        return out

    # -- compatibility ---------------------------------------------------------

    def check_compatibility(
        self, contract_name: str, consumer_contract_version: int
    ) -> CompatibilityResult:
        """版本軸分離的相容性判定——只比 contract 軸。

        consumer 版本必須 ≥ contract 的 ``minimum_supported_contract_version``
        且 ≤ contract 的 ``contract_version``（未來版本不相容）。
        """
        data = self._contracts.get(contract_name)
        if data is None:
            return CompatibilityResult(
                False, "contract-not-found", contract_name
            )
        version = data.get("contract_version")
        if version is None:
            version = _schema_version(data)
        minimum = data.get("minimum_supported_contract_version", 0)
        if not isinstance(version, int):
            return CompatibilityResult(
                False, "contract-version-invalid", contract_name
            )
        if consumer_contract_version > version:
            return CompatibilityResult(
                False,
                f"consumer-version-newer:{consumer_contract_version}>{version}",
                contract_name,
            )
        if consumer_contract_version < minimum:
            return CompatibilityResult(
                False,
                f"consumer-version-too-old:{consumer_contract_version}<{minimum}",
                contract_name,
            )
        return CompatibilityResult(True, "compatible", contract_name)

    def contract_version(self, contract_name: str) -> Optional[int]:
        data = self._contracts.get(contract_name)
        if data is None:
            return None
        version = data.get("contract_version")
        if version is None:
            version = _schema_version(data)
        return version if isinstance(version, int) else None


__all__ = [
    "CompatibilityResult",
    "ContractRegistry",
    "ContractValidation",
    "REGISTRY_VERSION",
]
