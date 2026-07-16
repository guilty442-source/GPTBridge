from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_electron_windows_enable_renderer_sandbox() -> None:
    sources = (
        ROOT / "src-ui" / "main" / "index.ts",
        ROOT / "scripts" / "templates" / "platform-tool-app" / "main.cjs",
    )

    for source_path in sources:
        source = source_path.read_text(encoding="utf-8")
        assert "sandbox: true" in source
        assert "sandbox: false" not in source
        assert "contextIsolation: true" in source
        assert "nodeIntegration: false" in source


def test_startup_pipeline_uses_real_health_probes() -> None:
    source = (
        ROOT
        / "src-ui"
        / "renderer"
        / "services"
        / "RuntimeServiceManager.ts"
    ).read_text(encoding="utf-8")

    assert "getBackendConnectionSnapshot" in source
    assert "app:get-platform-tool-sizes" in source
    assert "backendManaged" in source
    assert "manual_dev_mode" not in source
    assert "setTimeout(resolve, 100)" not in source
    assert "setTimeout(resolve, 160)" not in source
    assert "setTimeout(resolve, 220)" not in source


def test_packaged_backend_is_bound_to_bundled_resources() -> None:
    path_source = (ROOT / "src-ui" / "main" / "pathLibrary.ts").read_text(
        encoding="utf-8"
    )
    backend_source = (ROOT / "src-ui" / "main" / "python-backend.ts").read_text(
        encoding="utf-8"
    )

    packaged_candidates = path_source.split("const candidates = [", 1)[1].split(
        "]", 1
    )[0]
    assert packaged_candidates.index("resourcesRoot") < packaged_candidates.index(
        "executableDir"
    )
    assert "process.cwd()" not in packaged_candidates
    assert "path.join(executableDir, '..', '..')" not in packaged_candidates
    assert "const backendArgs = ['-u', paths.pythonEntry, '--serve']" in backend_source
    assert "GPTBRIDGE_BACKEND_HOT_RELOAD: '0'" in backend_source
    assert "backend_hot_reload.py" not in backend_source


def test_product_version_never_uses_electron_engine_version() -> None:
    index_source = (ROOT / "src-ui" / "main" / "index.ts").read_text(
        encoding="utf-8"
    )
    backend_source = (ROOT / "src-ui" / "main" / "python-backend.ts").read_text(
        encoding="utf-8"
    )
    version_source = (ROOT / "src-ui" / "main" / "product-version.ts").read_text(
        encoding="utf-8"
    )

    assert "app.getVersion()" not in index_source
    assert "app.getVersion()" not in backend_source
    assert "packageMetadata.version" in version_source
    assert "LOCKED_PRODUCT_VERSION = '1.0.0'" in version_source
    assert "PRODUCT_VERSION" in index_source
    assert "PRODUCT_VERSION" in backend_source


def test_disconnect_repair_never_queues_state_changes() -> None:
    socket_source = (
        ROOT / "src-ui" / "renderer" / "shared" / "hooks" / "useBackendSocket.ts"
    ).read_text(encoding="utf-8")

    assert "queued: false" in socket_source
    assert "WS_REPAIR_AFTER_ATTEMPTS" in socket_source
    assert "app:restart-backend" in socket_source
    assert "AUTO_REPAIR_BACKEND" in socket_source


def test_python_requirements_are_exactly_pinned() -> None:
    requirement_lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert requirement_lines
    assert all("==" in line for line in requirement_lines)
