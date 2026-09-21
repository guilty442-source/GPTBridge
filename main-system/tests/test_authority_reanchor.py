import sys
import time
from pathlib import Path

# Use main project root for integrity manifest (path guard requires it)
ROOT = Path("E:/GPTBridge")
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from governance_rule.execution.integrity import (
    AuthorityIntegrityGuard,
    build_integrity_manifest,
)


def test_guard_reanchor_resigns_and_verifies() -> None:
    project_root = ROOT
    launcher_key = b"g" * 32
    manifest = build_integrity_manifest(
        project_root, launcher_key, issued_at=int(time.time()), key_id="test-key"
    )
    guard = AuthorityIntegrityGuard(
        project_root, launcher_key, manifest, expected_key_id="test-key"
    )
    guard.verify()

    reanchored = guard.reanchor()

    assert reanchored.key_id == "test-key"
    assert reanchored.issued_at >= manifest.issued_at
    assert reanchored.file_digests == manifest.file_digests
    guard.verify()


def test_authority_file_digest_tracks_content(tmp_path: Path) -> None:
    from core_system.authority_reanchor_service import authority_file_digest

    first = tmp_path / "a.bin"
    first.write_bytes(b"one")
    baseline = authority_file_digest([first])
    first.write_bytes(b"two")
    assert authority_file_digest([first]) != baseline
    missing = tmp_path / "missing.bin"
    assert authority_file_digest([missing]) != baseline


def _patch_service(monkeypatch, service, authority_file, *, audit_ok=True):
    import types

    fake_permission = types.ModuleType("governance.sovereigns.permission_sovereign")
    fake_permission.re_certify_permission_sovereign = lambda: None
    monkeypatch.setitem(
        sys.modules, "governance.sovereigns.permission_sovereign", fake_permission
    )
    monkeypatch.setattr(service, "_authority_paths", lambda: [authority_file])
    monkeypatch.setattr(service, "_run_audit", lambda: audit_ok)
    monkeypatch.setattr(
        "core_system.authority_reanchor_service.load_governance_codex",
        lambda: object(),
    )
    monkeypatch.setattr(
        "core_system.authority_reanchor_service.validate_loaded_authority_version",
        lambda: None,
    )


class _FakeGovernance:
    def __init__(self) -> None:
        self.reanchored = 0

    def reanchor_runtime_integrity(self) -> None:
        self.reanchored += 1

    def runtime_integrity_ready(self, max_age_seconds: float = 2.0) -> bool:
        return True


class _FakeApp:
    def __init__(self, root: Path) -> None:
        self.project_root = root
        self.governance = _FakeGovernance()


def test_service_adopts_stable_authority_update(monkeypatch, tmp_path: Path) -> None:
    import core_system.authority_reanchor_service as svc

    authority_file = tmp_path / "authority.json"
    authority_file.write_text('{"v": 1}', encoding="utf-8")
    app = _FakeApp(tmp_path)
    service = svc.AuthorityReanchorService(app)
    _patch_service(monkeypatch, service, authority_file)
    # Seed the baseline without starting the watcher thread so the manual
    # probe below cannot race a background adoption.
    digest, _paths = service._snapshot()
    service._baseline = digest
    service._status.update(state="anchored")

    authority_file.write_text('{"v": 2}', encoding="utf-8")
    started = time.monotonic()
    service._probe_once()
    elapsed = time.monotonic() - started

    assert elapsed >= svc.STABILITY_DELAY_SECONDS
    assert app.governance.reanchored == 1
    status = service.get_status()
    assert status["state"] == "anchored"
    assert status["attempts"] == 1


def test_service_defers_while_audit_fails(monkeypatch, tmp_path: Path) -> None:
    import core_system.authority_reanchor_service as svc

    authority_file = tmp_path / "authority.json"
    authority_file.write_text('{"v": 1}', encoding="utf-8")
    app = _FakeApp(tmp_path)
    service = svc.AuthorityReanchorService(app)
    _patch_service(monkeypatch, service, authority_file, audit_ok=False)
    digest, _paths = service._snapshot()
    service._baseline = digest
    service._status.update(state="anchored")

    authority_file.write_text('{"v": 2}', encoding="utf-8")
    service._probe_once()

    assert app.governance.reanchored == 0
    status = service.get_status()
    assert status["state"] == "deferred"
    assert "audit" in status["last_error"]
