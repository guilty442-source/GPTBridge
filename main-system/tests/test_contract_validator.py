"""§10.9 contract registry / compatibility tests."""

from __future__ import annotations

import json

from core_system.contract_validator import ContractRegistry


def _write(dir_path, name: str, data: dict):
    (dir_path / f"{name}-contract.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def test_loads_and_validates_real_contracts():
    """Real config dir: all shipped contracts must validate."""
    from pathlib import Path

    config_dir = Path(__file__).resolve().parents[1] / "config"
    reg = ContractRegistry(config_dir)
    assert reg._contracts  # found at least one *-contract.json
    errors = reg.validation_errors()
    assert errors == []


def test_version_axis_compatibility(tmp_path):
    _write(tmp_path, "tool-runtime", {
        "contract_version": 3,
        "minimum_supported_contract_version": 1,
    })
    reg = ContractRegistry(tmp_path)
    assert reg.check_compatibility("tool-runtime", 2).compatible
    assert reg.check_compatibility("tool-runtime", 1).compatible
    assert not reg.check_compatibility("tool-runtime", 0).compatible
    assert "too-old" in reg.check_compatibility("tool-runtime", 0).reason
    assert not reg.check_compatibility("tool-runtime", 4).compatible
    assert "newer" in reg.check_compatibility("tool-runtime", 4).reason


def test_missing_contract_fails_closed(tmp_path):
    reg = ContractRegistry(tmp_path)
    result = reg.check_compatibility("nonexistent", 1)
    assert not result.compatible
    assert result.reason == "contract-not-found"


def test_invalid_contract_version_flagged(tmp_path):
    _write(tmp_path, "bad", {"contract_version": "v1"})
    _write(tmp_path, "worse", {"contract_version": 1, "minimum_supported_contract_version": 5})
    reg = ContractRegistry(tmp_path)
    errors = reg.validation_errors()
    assert any("contract-version-invalid" in e for e in errors)
    assert any("minimum-exceeds-version" in e for e in errors)


def test_unreadable_contract_flagged(tmp_path):
    (tmp_path / "broken-contract.json").write_text("{not json", encoding="utf-8")
    reg = ContractRegistry(tmp_path)
    errors = reg.validation_errors()
    assert any("broken" in e for e in errors)
