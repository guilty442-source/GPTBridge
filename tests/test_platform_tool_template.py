from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib import error as urllib_error

import pytest


def run_packaged_runtime_upgrade(
    tmp_path: Path,
    *,
    fail_recovery_completion: bool = False,
    probe_double_lock: bool = False,
    reload_after_success: bool = False,
    seed_interrupted_recovery: bool = False,
) -> subprocess.CompletedProcess[str]:
    app_root = tmp_path / "packaged-app"
    app_root.mkdir()
    shutil.copy2(
        Path("scripts/templates/platform-tool-app/main.cjs"),
        app_root / "main.cjs",
    )
    (app_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "demo",
                "name": "Demo",
                "standalone": {
                    "isolated_backend": True,
                    "backend_port": 23456,
                },
            }
        ),
        encoding="utf-8",
    )
    (app_root / ".gptbridge-package.json").write_text(
        "{}",
        encoding="utf-8",
    )
    packaged_core = app_root / "src-core"
    packaged_core.mkdir()
    (packaged_core / "main.py").write_text(
        "VERSION = 'new'\n",
        encoding="utf-8",
    )
    packaged_tool = app_root / "platform_tools" / "demo"
    (packaged_tool / "src").mkdir(parents=True)
    (packaged_tool / "src" / "main.py").write_text(
        "VERSION = 'new'\n",
        encoding="utf-8",
    )
    (packaged_tool / "manifest.json").write_text(
        '{"id":"demo","version":"2.0.0"}\n',
        encoding="utf-8",
    )

    project_root = tmp_path / "standalone-runtime"
    old_core = project_root / "src-core"
    old_core.mkdir(parents=True)
    (old_core / "main.py").write_text(
        "VERSION = 'old'\n",
        encoding="utf-8",
    )
    (old_core / "private-state.json").write_text(
        '{"private":true}\n',
        encoding="utf-8",
    )
    old_demo = project_root / "platform_tools" / "demo"
    (old_demo / "src").mkdir(parents=True)
    (old_demo / "src" / "main.py").write_text(
        "VERSION = 'old'\n",
        encoding="utf-8",
    )
    (old_demo / "runtime").mkdir()
    (old_demo / "runtime" / "state.json").write_text(
        '{"balance":42}\n',
        encoding="utf-8",
    )
    (old_demo / "custom").mkdir()
    (old_demo / "custom" / "unknown.bin").write_bytes(b"unique demo data")
    sibling_runtime = (
        project_root
        / "platform_tools"
        / "sibling"
        / "runtime"
    )
    sibling_runtime.mkdir(parents=True)
    (sibling_runtime / "unique.db").write_bytes(b"unique sibling data")

    user_data = tmp_path / "electron-user-data"
    user_data.mkdir()
    node_script = f"""
const fs = require('node:fs')
const path = require('node:path')
const Module = require('node:module')
const mainPath = {json.dumps(str(app_root / "main.cjs"))}
const projectRoot = {json.dumps(str(project_root))}
const userData = {json.dumps(str(user_data))}
const originalLoad = Module._load
const electronApp = {{
  isPackaged: true,
  commandLine: {{ hasSwitch: () => true }},
  setName: () => {{}},
  getPath: () => userData,
  setPath: () => {{}},
  requestSingleInstanceLock: () => false,
  quit: () => {{}},
  on: () => {{}},
  whenReady: () => Promise.resolve(),
}}
const electron = {{
  app: electronApp,
  BrowserWindow: {{ getAllWindows: () => [] }},
  dialog: {{}},
  ipcMain: {{ handle: () => {{}} }},
  Menu: {{ setApplicationMenu: () => {{}} }},
  shell: {{}},
}}
Module._load = function (request, parent, isMain) {{
  if (request === 'electron') return electron
  return originalLoad.call(this, request, parent, isMain)
}}
process.env.GPTBRIDGE_TEMPLATE_TEST_MODE = '1'
const verification = {{
  packageDigest: 'a'.repeat(64),
  protocolVersion: 1,
  backendVersion: '2.0.0',
}}
let upgradeError = ''
let duplicateLockError = ''
let runtimeTemplate = require(mainPath)
if ({json.dumps(probe_double_lock)}) {{
  const firstLock = runtimeTemplate.acquireRuntimePackageLock(
    projectRoot,
    'lock-probe-first'
  )
  try {{
    runtimeTemplate.acquireRuntimePackageLock(
      projectRoot,
      'lock-probe-second'
    )
  }} catch (error) {{
    duplicateLockError = String(error && error.message ? error.message : error)
  }} finally {{
    runtimeTemplate.releaseRuntimePackageLock(firstLock)
  }}
}}
if ({json.dumps(seed_interrupted_recovery)}) {{
  const crypto = require('node:crypto')
  const recoveryRoot = path.join(
    projectRoot,
    '.package-recovery-interrupted-fixture'
  )
  fs.mkdirSync(recoveryRoot)
  const trees = ['src-core', 'platform_tools'].map((treeName) => {{
    const treePath = path.join(projectRoot, treeName)
    const inventory = runtimeTemplate.inventoryOwnedRuntimeTree(treePath)
    return {{
      tree_name: treeName,
      original_path: treePath,
      recovery_relative_path: treeName,
      entries: inventory.entries,
      tree_digest: inventory.tree_digest,
    }}
  }})
  const payload = {{
    format_version: 1,
    status: 'prepared',
    reason: 'synthetic-interrupted-upgrade',
    tool_id: 'demo',
    package_digest: 'f'.repeat(64),
    created_at: new Date().toISOString(),
    project_root: projectRoot,
    recovery_root: recoveryRoot,
    trees,
  }}
  const manifest = {{
    ...payload,
    manifest_digest: crypto
      .createHash('sha256')
      .update(JSON.stringify(payload))
      .digest('hex'),
  }}
  fs.writeFileSync(
    path.join(recoveryRoot, 'recovery-manifest.json'),
    JSON.stringify(manifest, null, 2) + '\\n',
    'utf-8'
  )
}}
const originalRename = fs.renameSync
if ({json.dumps(fail_recovery_completion)}) {{
  fs.renameSync = function (source, target) {{
    if (path.basename(String(target)) === 'recovery-retained.json') {{
      throw new Error('synthetic recovery completion failure')
    }}
    return originalRename.call(this, source, target)
  }}
}}
try {{
  runtimeTemplate.preparePackagedBackendRuntime(projectRoot, verification)
}} catch (error) {{
  upgradeError = String(error && error.message ? error.message : error)
}} finally {{
  fs.renameSync = originalRename
}}
if ({json.dumps(reload_after_success)} && !upgradeError) {{
  delete require.cache[require.resolve(mainPath)]
  runtimeTemplate = require(mainPath)
  runtimeTemplate.preparePackagedBackendRuntime(projectRoot, verification)
}}
Module._load = originalLoad
const recoveryRoots = fs.readdirSync(projectRoot)
  .filter((name) => name.startsWith('.package-recovery-'))
console.log(JSON.stringify({{
  duplicateLockError,
  upgradeError,
  recoveryRoots,
}}))
"""
    harness = tmp_path / "runtime-upgrade-test.cjs"
    harness.write_text(node_script, encoding="utf-8")
    return subprocess.run(
        ["node", str(harness)],
        text=True,
        capture_output=True,
        check=False,
    )


def load_packager_module():
    module_path = Path("scripts/package_platform_tools.py")
    spec = importlib.util.spec_from_file_location("package_platform_tools", module_path)
    assert spec and spec.loader
    package_platform_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package_platform_tools)
    return package_platform_tools


def test_platform_tool_wrapper_prevents_duplicate_windows() -> None:
    source = Path("scripts/templates/platform-tool-app/main.cjs").read_text(
        encoding="utf-8"
    )

    assert "app.requestSingleInstanceLock({" in source
    assert "background: startHidden" in source
    assert "app.on('second-instance'" in source
    assert "requestMainWindowForeground('second-instance')" in source
    assert "let foregroundRequested = !startHidden" in source
    assert "if (!foregroundRequested)" in source
    assert "window.foregroundDeferred" in source
    assert "window.foregroundShown" in source
    assert "window.didFinishLoad" in source
    assert "window.bridgeStatus" in source
    assert "renderer.console" in source
    assert "backend.ensureRequested" in source
    assert "backend.ensureCompleted" in source
    assert "mainWindow.focus()" in source
    assert "app.commandLine.hasSwitch('user-data-dir')" in source
    assert "app.getPath('appData')" in source
    assert "app.setPath('userData', toolUserDataPath)" in source
    assert source.index("fs.mkdirSync(toolUserDataPath") < source.index(
        "app.setPath('userData', toolUserDataPath)"
    )
    assert source.index("app.setPath('userData', toolUserDataPath)") < source.index(
        "app.requestSingleInstanceLock({"
    )


def test_standalone_backend_ports_are_stable_unique_and_isolated() -> None:
    package_platform_tools = load_packager_module()
    tool_ids = [
        tool_id
        for tool_id, _tool_dir, _manifest in package_platform_tools.iter_tools(None)
    ]
    ports = [
        package_platform_tools.standalone_backend_port(tool_id)
        for tool_id in tool_ids
    ]

    assert len(ports) == len(set(ports))
    assert all(20_000 <= port < 40_000 for port in ports)
    assert package_platform_tools.standalone_backend_port("ai-assistant") == 21_265
    assert package_platform_tools.standalone_backend_port("file-sorter") == 22_964

    source = Path("scripts/templates/platform-tool-app/main.cjs").read_text(
        encoding="utf-8"
    )
    assert "[backendPortEnv]: String(backendPort)" in source
    assert "GPTBRIDGE_STANDALONE_TOOL_ID: toolId" in source
    assert "`ws://127.0.0.1:${backendPort}/" in source
    assert "Number(owner.backend_port || 8765) === backendPort" in source
    assert "standalone.isolated_backend !== false" in source
    assert "Package must use an isolated standalone backend" in source
    assert "app.isPackaged && standalone.isolated_backend !== false" in source
    assert (
        "if (app.isPackaged && !managedBackendReuseRequested())" in source
    )
    assert (
        "app.isPackaged\n    ? manifest.id || ''" in source
    )
    assert "fs.realpathSync.native(projectRoot)" in source
    assert "assertOwnedTree(liveToolsRoot" in source
    assert "stopRecordedLegacyBackend(projectRoot)" in source
    assert "backend.legacyPortStopped" in source
    assert "backend.foreignLegacyPortIgnored" in source
    assert "backend.staleLegacyOwnerIgnored" in source
    assert "windowsProcessOwnsPort" in source
    assert "backend.currentReuseBeforeLegacyMigration" in source
    ensure_start = source.index("async function ensureBackendStarted()")
    assert source.index(
        "backend.currentReuseBeforeLegacyMigration", ensure_start
    ) < source.index("stopRecordedLegacyBackend(projectRoot)", ensure_start)
    assert "forceStopVerifiedUnresponsiveBackend" in source
    assert "windowsProcessOwnsBackend" in source
    assert "backend.verifiedHungProcessStopped" in source
    assert "backend-handoff-recovery" in source
    assert "GPTBRIDGE_START_HIDDEN" in source
    assert "window.backgroundStartReady" in source
    assert "singleInstance.backgroundProbe" in source
    assert "background: startHidden" in source
    assert "net.createServer()" in source
    assert "exclusive: true" in source
    assert "net.createConnection" not in source
    assert "GPTBRIDGE_VISUAL_SMOKE_APP_DATA_ROOT" in source
    assert "gptbridge-ai-assistant-visual-" in source
    assert "function applyDeclaredEnvironmentBindings(environment)" in source
    assert "cleanerTargetProjectRoot" not in source
    assert "toolId !== 'project-cleaner'" not in source
    assert "return path.join(inferProjectRoot(), 'runtime', 'ipc')" in source
    assert source.index("const liveCore =") < source.index(
        "fs.existsSync(liveCore)"
    )
    assert "legacy-rule-migration-inbox" in source
    assert "Legacy file-sorter rules are not a bounded regular file" in source
    assert "fs.constants.COPYFILE_EXCL" in source
    assert "recovery-manifest.json" in source
    assert "recovery-retained.json" in source
    assert "previous-backend-package-marker.json" in source
    assert "backend-package-marker.json" in source
    assert "unlinkSync(markerPath)" not in source
    assert "inventoryOwnedRuntimeTree" in source
    assert "backend.packageRecoveryRetained" in source


def test_file_sorter_first_launch_atomically_migrates_legacy_profiles(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "packaged-app"
    app_root.mkdir()
    shutil.copy2(
        Path("scripts/templates/platform-tool-app/main.cjs"),
        app_root / "main.cjs",
    )
    (app_root / "manifest.json").write_text(
        json.dumps({"id": "file-sorter", "name": "File Sorter"}),
        encoding="utf-8",
    )
    (app_root / ".gptbridge-package.json").write_text("{}\n", encoding="utf-8")

    local_app_data = tmp_path / "local-app-data"
    legacy_profile = (
        local_app_data
        / "GPTBridge"
        / "file-sorter"
        / "profiles"
        / "target-fixture"
        / "profile.json"
    )
    legacy_profile.parent.mkdir(parents=True)
    legacy_profile.write_text(
        json.dumps({"profile_id": "target-fixture", "rules": [{"keyword": "old"}]}),
        encoding="utf-8",
    )
    legacy_quarantine = legacy_profile.with_name("legacy-rules-quarantine-fixture.json")
    legacy_quarantine.write_text(
        '{"rules":[{"keyword":"recoverable"}]}\n',
        encoding="utf-8",
    )

    project_root = tmp_path / "standalone-runtime"
    user_data = tmp_path / "electron-user-data"
    user_data.mkdir()
    node_script = f"""
const fs = require('node:fs')
const path = require('node:path')
const Module = require('node:module')
const mainPath = {json.dumps(str(app_root / 'main.cjs'))}
const projectRoot = {json.dumps(str(project_root))}
const userData = {json.dumps(str(user_data))}
const originalLoad = Module._load
const electron = {{
  app: {{
    isPackaged: true,
    commandLine: {{ hasSwitch: () => true }},
    setName: () => {{}},
    getPath: () => userData,
    setPath: () => {{}},
    requestSingleInstanceLock: () => false,
    quit: () => {{}},
    on: () => {{}},
    whenReady: () => Promise.resolve(),
  }},
  BrowserWindow: {{ getAllWindows: () => [] }},
  dialog: {{}},
  ipcMain: {{ handle: () => {{}} }},
  Menu: {{ setApplicationMenu: () => {{}} }},
  shell: {{}},
}}
Module._load = function (request, parent, isMain) {{
  if (request === 'electron') return electron
  return originalLoad.call(this, request, parent, isMain)
}}
process.env.GPTBRIDGE_TEMPLATE_TEST_MODE = '1'
const runtimeTemplate = require(mainPath)
const first = runtimeTemplate.migrateLegacyFileSorterProfiles(projectRoot)
const migratedProfile = path.join(
  projectRoot,
  'runtime',
  'file-sorter-state',
  'profiles',
  'target-fixture',
  'profile.json'
)
fs.writeFileSync(
  migratedProfile,
  '{{"profile_id":"target-fixture","rules":[{{"keyword":"current"}}]}}'
)
fs.writeFileSync(
  {json.dumps(str(legacy_profile))},
  '{{"profile_id":"target-fixture","rules":[{{"keyword":"changed-old"}}]}}'
)
const second = runtimeTemplate.migrateLegacyFileSorterProfiles(projectRoot)
console.log(JSON.stringify({{
  first,
  second,
  profile: JSON.parse(fs.readFileSync(migratedProfile, 'utf-8')),
  quarantineExists: fs.existsSync(path.join(
    path.dirname(migratedProfile),
    'legacy-rules-quarantine-fixture.json'
  )),
}}))
"""
    harness = tmp_path / "file-sorter-state-migration-test.cjs"
    harness.write_text(node_script, encoding="utf-8")
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local_app_data)
    result = subprocess.run(
        ["node", str(harness)],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome["first"]["migrated"] is True
    assert outcome["second"] == {
        "migrated": False,
        "reason": "current-profiles-exist",
    }
    assert outcome["profile"]["rules"] == [{"keyword": "current"}]
    assert outcome["quarantineExists"] is True


def test_runtime_upgrade_retains_shared_and_unknown_data_with_hash_manifest(
    tmp_path: Path,
) -> None:
    result = run_packaged_runtime_upgrade(
        tmp_path,
        reload_after_success=True,
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome["upgradeError"] == ""
    assert len(outcome["recoveryRoots"]) == 1

    project_root = tmp_path / "standalone-runtime"
    live_tools = project_root / "platform_tools"
    assert sorted(path.name for path in live_tools.iterdir()) == ["demo"]
    assert (live_tools / "demo" / "src" / "main.py").read_text(
        encoding="utf-8"
    ) == "VERSION = 'new'\n"
    assert (live_tools / "demo" / "runtime" / "state.json").read_text(
        encoding="utf-8"
    ) == '{"balance":42}\n'

    recovery_root = project_root / outcome["recoveryRoots"][0]
    sibling_data = (
        recovery_root
        / "platform_tools"
        / "sibling"
        / "runtime"
        / "unique.db"
    )
    unknown_data = (
        recovery_root
        / "platform_tools"
        / "demo"
        / "custom"
        / "unknown.bin"
    )
    assert sibling_data.read_bytes() == b"unique sibling data"
    assert unknown_data.read_bytes() == b"unique demo data"
    assert (recovery_root / "src-core" / "private-state.json").read_text(
        encoding="utf-8"
    ) == '{"private":true}\n'

    manifest = json.loads(
        (recovery_root / "recovery-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    completion = json.loads(
        (recovery_root / "recovery-retained.json").read_text(
            encoding="utf-8"
        )
    )
    update_journal = json.loads(
        (recovery_root / "package-update-journal.json").read_text(
            encoding="utf-8"
        )
    )
    update_completion = json.loads(
        (recovery_root / "package-update-complete.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["status"] == "prepared"
    assert completion["status"] == "retained"
    assert update_journal["status"] == "prepared"
    assert update_completion["status"] == "complete"
    assert completion["prepared_manifest_digest"] == manifest["manifest_digest"]
    assert len(manifest["manifest_digest"]) == 64
    platform_tree = next(
        tree
        for tree in manifest["trees"]
        if tree["tree_name"] == "platform_tools"
    )
    sibling_entry = next(
        entry
        for entry in platform_tree["entries"]
        if entry["path"] == "sibling/runtime/unique.db"
    )
    assert sibling_entry["size"] == len(b"unique sibling data")
    assert sibling_entry["sha256"] == (
        "a886e0f2693debca22600977f26c51d7"
        "079eb435a078d3beacbcf9a398abc298"
    )


def test_runtime_upgrade_fails_closed_if_recovery_completion_cannot_publish(
    tmp_path: Path,
) -> None:
    result = run_packaged_runtime_upgrade(
        tmp_path,
        fail_recovery_completion=True,
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert "synthetic recovery completion failure" in outcome["upgradeError"]
    assert len(outcome["recoveryRoots"]) == 1

    project_root = tmp_path / "standalone-runtime"
    assert (project_root / "src-core" / "main.py").read_text(
        encoding="utf-8"
    ) == "VERSION = 'old'\n"
    assert (
        project_root
        / "platform_tools"
        / "demo"
        / "custom"
        / "unknown.bin"
    ).read_bytes() == b"unique demo data"
    assert (
        project_root
        / "platform_tools"
        / "sibling"
        / "runtime"
        / "unique.db"
    ).read_bytes() == b"unique sibling data"
    assert not (project_root / ".backend-package.json").exists()

    recovery_root = project_root / outcome["recoveryRoots"][0]
    manifest = json.loads(
        (recovery_root / "recovery-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["status"] == "prepared"
    assert len(manifest["manifest_digest"]) == 64
    assert not (recovery_root / "recovery-retained.json").exists()
    aborted = json.loads(
        (recovery_root / "package-update-aborted.json").read_text(
            encoding="utf-8"
        )
    )
    assert aborted["status"] == "aborted-and-rolled-back"


def test_runtime_package_lock_and_crash_journal_reconcile_without_cleanup(
    tmp_path: Path,
) -> None:
    result = run_packaged_runtime_upgrade(
        tmp_path,
        probe_double_lock=True,
        seed_interrupted_recovery=True,
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert "Another runtime package update is active" in outcome[
        "duplicateLockError"
    ]
    assert outcome["upgradeError"] == ""
    assert len(outcome["recoveryRoots"]) == 2

    project_root = tmp_path / "standalone-runtime"
    interrupted_root = (
        project_root / ".package-recovery-interrupted-fixture"
    )
    reconciliation = json.loads(
        (
            interrupted_root
            / "package-update-reconciled.json"
        ).read_text(encoding="utf-8")
    )
    assert reconciliation["status"] == "reconciled"
    assert {
        tree["tree_name"]
        for tree in reconciliation["trees"]
    } == {"src-core", "platform_tools"}
    assert (
        interrupted_root / "recovery-manifest.json"
    ).is_file()
    assert (
        project_root
        / "platform_tools"
        / "demo"
        / "runtime"
        / "state.json"
    ).read_text(encoding="utf-8") == '{"balance":42}\n'


def test_standalone_ipc_owner_state_is_scoped_to_the_tool_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))

    project_root = package_platform_tools.standalone_project_root("ai-assistant")
    owner_path = package_platform_tools.standalone_backend_owner_path(
        "ai-assistant"
    )
    legacy_owner = package_platform_tools.legacy_standalone_backend_owner_path(
        "ai-assistant"
    )

    owner_path.relative_to(project_root)
    assert owner_path.parent == project_root / "runtime" / "ipc"
    assert legacy_owner != owner_path


def test_platform_tool_backend_session_does_not_shadow_instance_id_function() -> None:
    source = Path("scripts/templates/platform-tool-app/main.cjs").read_text(
        encoding="utf-8"
    )

    assert "const workspaceInstanceId = workspaceInstanceId(" not in source
    assert "const projectRoot = inferProjectRoot()" in source
    assert "const instanceId = workspaceInstanceId(projectRoot)" in source
    assert "workspaceInstanceId: instanceId" in source


def test_platform_tool_packager_copies_runtime_source(tmp_path: Path) -> None:
    package_platform_tools = load_packager_module()

    tool_dir = tmp_path / "demo-tool"
    entry = tool_dir / "src" / "main.py"
    backend_file = tool_dir / "src" / "backend" / "service.py"
    pycache_file = tool_dir / "src" / "backend" / "__pycache__" / "service.pyc"
    backend_file.parent.mkdir(parents=True)
    pycache_file.parent.mkdir(parents=True)
    entry.write_text("print('demo')\n", encoding="utf-8")
    backend_file.write_text("class Service:\n    pass\n", encoding="utf-8")
    pycache_file.write_bytes(b"cache")

    app_dir = tmp_path / "dist" / "resources" / "app"
    app_dir.mkdir(parents=True)
    copied = package_platform_tools.copy_runtime_source(tool_dir, entry, app_dir)

    assert copied == app_dir / "src"
    assert (app_dir / "src" / "main.py").exists()
    assert (app_dir / "src" / "backend" / "service.py").exists()
    assert not (app_dir / "src" / "backend" / "__pycache__").exists()


def test_standalone_backend_bundle_contains_only_its_tool_directory(
    tmp_path: Path, monkeypatch
) -> None:
    package_platform_tools = load_packager_module()
    core_dir = tmp_path / "src-core"
    core_dir.mkdir()
    (core_dir / "main.py").write_text("print('core')\n", encoding="utf-8")
    monkeypatch.setattr(package_platform_tools, "SRC_CORE_DIR", core_dir)

    tools_dir = tmp_path / "platform_tools"
    tool_dir = tools_dir / "isolated-tool"
    entry = tool_dir / "src" / "main.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("print('isolated')\n", encoding="utf-8")
    (tool_dir / "manifest.json").write_text(
        json.dumps({"id": "isolated-tool"}), encoding="utf-8"
    )
    sibling_dir = tools_dir / "other-tool"
    sibling_dir.mkdir()
    (sibling_dir / "secret.txt").write_text("must not be copied\n", encoding="utf-8")
    app_dir = tmp_path / "app"
    app_dir.mkdir()

    package_platform_tools.copy_backend_source_bundle(
        "isolated-tool", tool_dir, entry, app_dir
    )

    packaged_tools = app_dir / "platform_tools"
    assert {path.name for path in packaged_tools.iterdir()} == {"isolated-tool"}
    assert not (packaged_tools / "other-tool").exists()
    assert (packaged_tools / "isolated-tool" / "src" / "main.py").exists()


def test_file_sorter_bundle_replaces_mutable_rules_with_clean_seed(
    tmp_path: Path, monkeypatch
) -> None:
    package_platform_tools = load_packager_module()
    core_dir = tmp_path / "src-core"
    core_dir.mkdir()
    (core_dir / "main.py").write_text("print('core')\n", encoding="utf-8")
    monkeypatch.setattr(package_platform_tools, "SRC_CORE_DIR", core_dir)

    tool_dir = tmp_path / "platform_tools" / "file-sorter"
    runtime_dir = tool_dir / "src"
    runtime_dir.mkdir(parents=True)
    entry = runtime_dir / "main.py"
    entry.write_text("print('sorter')\n", encoding="utf-8")
    source_rules = {
        "keyword_rules.json": '[{"keyword": "private-json"}]\n',
        "keyword_rules.py": "KEYWORD_RULES = [('private-python', 'outside')]\n",
        ".file-sorter-rules.json": '[{"keyword": "private-hidden"}]\n',
    }
    for name, content in source_rules.items():
        (runtime_dir / name).write_text(content, encoding="utf-8")
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "file-sorter",
                "package": {
                    "source_excludes": [
                        "src/keyword_rules.json",
                        "src/keyword_rules.py",
                        "src/.file-sorter-rules.json",
                    ],
                    "generated_files": {
                        "src/keyword_rules.json": [],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    app_dir = tmp_path / "app"
    app_dir.mkdir()

    package_platform_tools.copy_backend_source_bundle(
        "file-sorter",
        tool_dir,
        entry,
        app_dir,
    )

    packaged_runtime = app_dir / "platform_tools" / "file-sorter" / "src"
    assert (packaged_runtime / "keyword_rules.json").read_text(
        encoding="utf-8"
    ) == "[]\n"
    assert not (packaged_runtime / "keyword_rules.py").exists()
    assert not (packaged_runtime / ".file-sorter-rules.json").exists()
    for name, content in source_rules.items():
        assert (runtime_dir / name).read_text(encoding="utf-8") == content


def test_file_sorter_source_snapshot_excludes_mutable_rule_files() -> None:
    package_platform_tools = load_packager_module()
    tool_dir = Path("platform_tools/file-sorter").resolve()
    manifest = package_platform_tools.load_manifest(tool_dir)
    assert manifest is not None
    entry = package_platform_tools.resolve_entry(tool_dir, manifest)

    roots = package_platform_tools.package_source_roots(tool_dir, entry)
    excluded_paths = package_platform_tools.package_source_excluded_paths(
        tool_dir,
        entry,
    )
    source_files = package_platform_tools.collect_file_hashes(
        package_platform_tools.PROJECT_ROOT,
        roots,
        ignored_directory_names=(
            package_platform_tools.SOURCE_IGNORED_DIRECTORY_NAMES
        ),
        excluded_relative_paths=frozenset(excluded_paths),
    )

    mutable_paths = {
        "platform_tools/file-sorter/src/keyword_rules.json",
        "platform_tools/file-sorter/src/keyword_rules.py",
        "platform_tools/file-sorter/src/.file-sorter-rules.json",
    }
    assert "platform_tools/file-sorter/src" in roots
    assert set(excluded_paths) == mutable_paths
    assert mutable_paths.isdisjoint(source_files)
    assert "platform_tools/file-sorter/src/main.py" in source_files


def test_platform_tool_source_snapshot_includes_all_packaged_sources() -> None:
    package_platform_tools = load_packager_module()
    tool_dir = Path("platform_tools/ai-assistant").resolve()
    manifest = package_platform_tools.load_manifest(tool_dir)
    assert manifest is not None
    entry = package_platform_tools.resolve_entry(tool_dir, manifest)

    roots = package_platform_tools.package_source_roots(tool_dir, entry)

    assert "platform_tools/ai-assistant/README.md" in roots
    assert "config/tool-runtime-contract.json" in roots
    assert "src-core" not in roots
    assert "scripts/templates/platform-tool-app" not in roots
    assert "src-ui/platform-tools/renderPlatformTool.tsx" not in roots
    assert "package-lock.json" not in roots


def test_completed_package_recovery_is_bounded_per_tool(
    tmp_path: Path,
) -> None:
    package_platform_tools = load_packager_module()
    tool_dir = tmp_path / "platform_tools" / "demo"
    build_root = tool_dir / "build"
    retained = []
    for index in range(3):
        package_root = build_root / f"package-{index}"
        package_root.mkdir(parents=True)
        (package_root / "promotion-complete.json").write_text(
            '{"status":"complete-with-recovery"}',
            encoding="utf-8",
        )
        (package_root / "promotion-recovery-manifest.json").write_text(
            '{"status":"retained"}',
            encoding="utf-8",
        )
        os.utime(package_root, (index + 1, index + 1))
        retained.append(package_root)
    incomplete = build_root / "package-incomplete"
    incomplete.mkdir()
    (incomplete / "promotion-complete.json").write_text(
        '{"status":"complete-with-recovery"}',
        encoding="utf-8",
    )
    (incomplete / "promotion-recovery-manifest.json").write_text(
        '{"status":"retained"}',
        encoding="utf-8",
    )
    (incomplete / "promotion-aborted.json").write_text(
        '{"status":"rollback-incomplete"}',
        encoding="utf-8",
    )

    removed = package_platform_tools.prune_completed_recovery_roots(
        tool_dir,
        keep=1,
    )

    assert len(removed) == 2
    assert retained[-1].is_dir()
    assert incomplete.is_dir()


def test_upgrade_auto_repair_repairs_then_rechecks_project_anomalies(
    tmp_path: Path,
) -> None:
    package_platform_tools = load_packager_module()

    class FakeCleaner:
        calls: list[tuple[bool, bool]] = []
        dry_run_count = 0

        def __init__(self, project_root: Path) -> None:
            assert project_root == tmp_path

        def repair_anomalies(
            self,
            *,
            dry_run: bool,
            include_shared: bool,
            selected_anomalies: set[str],
        ) -> dict[str, object]:
            assert selected_anomalies == set(
                package_platform_tools.UPGRADE_AUTO_REPAIR_ANOMALIES
            )
            self.calls.append((dry_run, include_shared))
            if dry_run:
                self.__class__.dry_run_count += 1
                repairable = 1 if self.dry_run_count == 1 else 0
                return {
                    "ok": True,
                    "anomaly_count": repairable,
                    "repairable_count": repairable,
                    "diagnostics": [],
                }
            return {
                "ok": True,
                "repaired_count": 1,
                "repaired_bytes": 42,
                "quarantine_path": str(tmp_path / "quarantine"),
            }

    result = package_platform_tools.run_upgrade_auto_repair(
        tmp_path,
        service_factory=FakeCleaner,
    )

    assert result["ok"] is True
    assert result["repaired_count"] == 1
    assert result["repaired_bytes"] == 42
    assert result["unresolved_count"] == 0
    assert FakeCleaner.calls == [(True, True), (False, True), (True, True)]


def test_upgrade_auto_repair_fails_closed_when_anomaly_remains(
    tmp_path: Path,
) -> None:
    package_platform_tools = load_packager_module()

    class IncompleteCleaner:
        def __init__(self, _project_root: Path) -> None:
            pass

        def repair_anomalies(
            self,
            *,
            dry_run: bool,
            include_shared: bool,
            selected_anomalies: set[str],
        ) -> dict[str, object]:
            assert include_shared is True
            assert selected_anomalies == set(
                package_platform_tools.UPGRADE_AUTO_REPAIR_ANOMALIES
            )
            if dry_run:
                return {
                    "ok": True,
                    "anomaly_count": 1,
                    "repairable_count": 1,
                    "diagnostics": [],
                }
            return {
                "ok": True,
                "repaired_count": 0,
                "repaired_bytes": 0,
                "skipped": [{"reason": "locked"}],
            }

    result = package_platform_tools.run_upgrade_auto_repair(
        tmp_path,
        service_factory=IncompleteCleaner,
    )

    assert result["ok"] is False
    assert result["error_code"] == "UPGRADE_REPAIR_INCOMPLETE"
    assert result["unresolved_count"] == 1


def test_upgrade_post_verification_retries_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    verifications = iter(
        [
            {"ok": False, "message": "transient package mismatch"},
            {"ok": True, "package_digest": "verified"},
        ]
    )
    package_calls: list[str] = []

    monkeypatch.setattr(
        package_platform_tools,
        "verify_tool_package",
        lambda *_args: next(verifications),
    )

    def fake_package_tool(
        tool_id: str,
        _tool_dir: Path,
        _manifest: dict[str, object],
    ) -> dict[str, object]:
        package_calls.append(tool_id)
        return {"ok": True, "tool_id": tool_id}

    monkeypatch.setattr(
        package_platform_tools,
        "package_tool",
        fake_package_tool,
    )

    result = package_platform_tools.verify_upgrade_result(
        "demo",
        tmp_path,
        {"id": "demo"},
        {"ok": True, "tool_id": "demo"},
    )

    assert result["ok"] is True
    assert result["upgrade_auto_retry"] is True
    assert result["post_verification"]["ok"] is True
    assert package_calls == ["demo"]


def test_locked_completed_recovery_does_not_fail_successful_package_pruning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    tool_dir = tmp_path / "platform_tools" / "demo"
    build_root = tool_dir / "build"
    recovery_roots: list[Path] = []
    for index in range(2):
        package_root = build_root / f"package-{index}"
        package_root.mkdir(parents=True)
        (package_root / "promotion-complete.json").write_text(
            '{"status":"complete-with-recovery"}',
            encoding="utf-8",
        )
        (package_root / "promotion-recovery-manifest.json").write_text(
            '{"status":"retained"}',
            encoding="utf-8",
        )
        os.utime(package_root, (index + 1, index + 1))
        recovery_roots.append(package_root)

    real_rmtree = package_platform_tools.shutil.rmtree

    def locked_rmtree(path: Path) -> None:
        if Path(path) == recovery_roots[0]:
            raise PermissionError("recovery is temporarily locked")
        real_rmtree(path)

    monkeypatch.setattr(
        package_platform_tools.shutil,
        "rmtree",
        locked_rmtree,
    )

    removed = package_platform_tools.prune_completed_recovery_roots(
        tool_dir,
        keep=1,
    )

    assert removed == []
    assert recovery_roots[0].is_dir()
    assert recovery_roots[1].is_dir()


def test_platform_tool_packager_excludes_runtime_and_financial_data(
    tmp_path: Path,
) -> None:
    package_platform_tools = load_packager_module()

    tool_dir = tmp_path / "investment-tool"
    entry = tool_dir / "src" / "main.py"
    runtime_state = tool_dir / "src" / "runtime" / "investment_watch_state.json"
    imported_file = tool_dir / "src" / "imports" / "statement.xlsx"
    database = tool_dir / "src" / "backend" / "investment.sqlite3"
    vault = tool_dir / "src" / "backend" / "backup.ivault"
    source = tool_dir / "src" / "backend" / "service.py"
    for file_path in (entry, runtime_state, imported_file, database, vault, source):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("content\n", encoding="utf-8")

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    package_platform_tools.copy_runtime_source(tool_dir, entry, app_dir)

    assert (app_dir / "src" / "main.py").exists()
    assert (app_dir / "src" / "backend" / "service.py").exists()
    assert not (app_dir / "src" / "runtime").exists()
    assert not (app_dir / "src" / "imports").exists()
    assert not (app_dir / "src" / "backend" / "investment.sqlite3").exists()
    assert not (app_dir / "src" / "backend" / "backup.ivault").exists()


def test_platform_tool_packager_builds_portable_python_runtime(
    tmp_path: Path,
) -> None:
    package_platform_tools = load_packager_module()
    base = tmp_path / "base-python"
    environment = tmp_path / "environment"
    (base / "DLLs").mkdir(parents=True)
    (base / "Lib" / "ensurepip").mkdir(parents=True)
    (environment / "Lib" / "site-packages" / "websockets").mkdir(parents=True)
    (environment / "Lib" / "site-packages" / "playwright").mkdir(parents=True)
    (environment / "Lib" / "site-packages" / "PIL").mkdir(parents=True)
    (environment / "Lib" / "site-packages" / "imageio_ffmpeg").mkdir(
        parents=True
    )
    (environment / "Lib" / "site-packages" / "pytest").mkdir(parents=True)
    (base / "python.exe").write_bytes(b"python")
    (base / "python311.dll").write_bytes(b"dll")
    (base / "DLLs" / "_ssl.pyd").write_bytes(b"ssl")
    (base / "Lib" / "os.py").write_text("# stdlib\n", encoding="utf-8")
    (base / "Lib" / "ensurepip" / "__init__.py").write_text(
        "# excluded\n", encoding="utf-8"
    )
    (
        environment / "Lib" / "site-packages" / "websockets" / "__init__.py"
    ).write_text("# dependency\n", encoding="utf-8")
    for dependency in ("playwright", "PIL", "imageio_ffmpeg"):
        (
            environment
            / "Lib"
            / "site-packages"
            / dependency
            / "__init__.py"
        ).write_text("# required runtime dependency\n", encoding="utf-8")
    (
        environment / "Lib" / "site-packages" / "pytest" / "__init__.py"
    ).write_text("# dev-only\n", encoding="utf-8")

    runtime = package_platform_tools.copy_portable_python_runtime(
        tmp_path / "app",
        base_prefix=base,
        environment_prefix=environment,
    )

    assert (runtime / "python.exe").read_bytes() == b"python"
    assert (runtime / "DLLs" / "_ssl.pyd").exists()
    assert (runtime / "Lib" / "os.py").exists()
    assert (runtime / "Lib" / "site-packages" / "websockets" / "__init__.py").exists()
    for dependency in ("playwright", "PIL", "imageio_ffmpeg"):
        assert (
            runtime
            / "Lib"
            / "site-packages"
            / dependency
            / "__init__.py"
        ).exists()
    assert not (runtime / "Lib" / "ensurepip").exists()
    assert not (runtime / "Lib" / "site-packages" / "pytest").exists()


def test_staged_runtime_probe_includes_feature_dependencies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    app_dir = tmp_path / "app"
    (app_dir / "python").mkdir(parents=True)
    (app_dir / "src-core").mkdir()
    (app_dir / "python" / "python.exe").write_bytes(b"python")
    (app_dir / "src-core" / "main.py").write_text(
        "# backend\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        commands.append(command)
        return Completed()

    monkeypatch.setattr(package_platform_tools.subprocess, "run", fake_run)
    package_platform_tools.validate_staged_python_runtime(
        app_dir,
        "file-sorter",
    )

    imported = json.loads(commands[0][-1])
    assert "playwright.async_api" in imported
    assert "websockets" in imported
    assert "PIL" in imported
    assert "imageio_ffmpeg" in imported
    assert commands[1][-1] == "--help"


def test_packager_only_stops_backend_with_verified_owner(
    tmp_path: Path, monkeypatch
) -> None:
    package_platform_tools = load_packager_module()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    owner_path = package_platform_tools.standalone_backend_owner_path(
        "ai-assistant"
    )
    owner_path.parent.mkdir(parents=True)
    owner_path.write_text(
        json.dumps(
            {
                "tool_id": "different-tool",
                "project_root": str(
                    package_platform_tools.standalone_project_root(
                        "ai-assistant"
                    )
                ),
                "workspace_instance_id": "forged",
                "shutdown_token": "not-a-token",
                "backend_port": 31_001,
            }
        ),
        encoding="utf-8",
    )

    def forbidden_urlopen(*_args, **_kwargs):
        raise AssertionError("invalid owner must not trigger a network request")

    monkeypatch.setattr(
        package_platform_tools.urllib_request,
        "urlopen",
        forbidden_urlopen,
    )

    assert (
        package_platform_tools.stop_verified_packaged_backend(
            "ai-assistant",
            tmp_path / "dist" / "resources" / "app",
        )
        is False
    )


def test_packager_force_stops_only_digest_and_listener_verified_hung_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Packaged executable handoff is Windows-specific")

    package_platform_tools = load_packager_module()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    tool_id = "ai-assistant"
    expected_root = package_platform_tools.standalone_project_root(tool_id)
    expected_instance = package_platform_tools.workspace_instance_id(expected_root)
    live_app = tmp_path / "dist" / "resources" / "app"
    runtime = live_app / "python" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"verified packaged python runtime")
    runtime_digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    package_digest = "d" * 64
    (live_app / package_platform_tools.PACKAGE_METADATA_NAME).write_text(
        json.dumps(
            {
                "format_version": package_platform_tools.PACKAGE_FORMAT_VERSION,
                "payload_digest": package_digest,
                "payload_files": {
                    "python/python.exe": runtime_digest,
                },
            }
        ),
        encoding="utf-8",
    )
    owner_path = package_platform_tools.legacy_standalone_backend_owner_path(
        tool_id
    )
    owner_path.parent.mkdir(parents=True)
    owner_path.write_text(
        json.dumps(
            {
                "tool_id": tool_id,
                "pid": 42_424,
                "project_root": str(expected_root),
                "workspace_instance_id": expected_instance,
                "package_digest": package_digest,
                "shutdown_token": "a" * 64,
                "backend_port": 31_003,
            }
        ),
        encoding="utf-8",
    )

    killed = False
    taskkill_commands: list[list[str]] = []

    def unavailable_health(*_args, **_kwargs):
        raise urllib_error.URLError("synthetic timeout")

    def fake_run(command, **_kwargs):
        nonlocal killed
        assert command[0].casefold() == "taskkill"
        killed = True
        taskkill_commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "stopped", "")

    monkeypatch.setattr(
        package_platform_tools.urllib_request,
        "urlopen",
        unavailable_health,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "running_executable_process_ids",
        lambda _path: [42_424],
    )
    monkeypatch.setattr(
        package_platform_tools,
        "windows_process_owns_listening_port",
        lambda process_id, port: (
            process_id == 42_424 and port == 31_003 and not killed
        ),
    )
    monkeypatch.setattr(
        package_platform_tools,
        "_process_is_alive",
        lambda process_id: process_id == 42_424 and not killed,
    )
    monkeypatch.setattr(package_platform_tools.subprocess, "run", fake_run)

    assert package_platform_tools.stop_verified_packaged_backend(
        tool_id,
        live_app,
    ) is True
    assert taskkill_commands == [
        ["taskkill", "/PID", "42424", "/T", "/F"]
    ]
    audit_root = (
        expected_root / "runtime" / "backend-handoff-recovery"
    )
    requested = list(audit_root.glob("*.requested.json"))
    completed = list(audit_root.glob("*.completed.json"))
    assert len(requested) == 1
    assert len(completed) == 1
    assert "shutdown_token" not in requested[0].read_text(encoding="utf-8")
    assert owner_path.exists()


def test_packager_skips_invalid_current_owner_and_stops_valid_legacy_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    tool_id = "ai-assistant"
    expected_root = package_platform_tools.standalone_project_root(tool_id)
    expected_instance = package_platform_tools.workspace_instance_id(expected_root)
    current_owner = package_platform_tools.standalone_backend_owner_path(tool_id)
    legacy_owner = package_platform_tools.legacy_standalone_backend_owner_path(
        tool_id
    )
    current_owner.parent.mkdir(parents=True)
    legacy_owner.parent.mkdir(parents=True)
    current_owner.write_text(
        json.dumps(
            {
                "tool_id": "wrong-tool",
                "project_root": str(expected_root),
                "workspace_instance_id": expected_instance,
                "shutdown_token": "a" * 64,
                "backend_port": 31_001,
            }
        ),
        encoding="utf-8",
    )
    legacy_token = "b" * 64
    legacy_owner.write_text(
        json.dumps(
            {
                "tool_id": tool_id,
                "project_root": str(expected_root),
                "workspace_instance_id": expected_instance,
                "shutdown_token": legacy_token,
                "backend_port": 31_002,
            }
        ),
        encoding="utf-8",
    )
    requested_urls: list[str] = []
    shutdown_tokens: list[str] = []
    shutdown_sent = False

    class Response:
        status = 200

        def __init__(self, payload: bytes = b"") -> None:
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

        def close(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(request, **_kwargs):
        nonlocal shutdown_sent
        url = (
            request.full_url
            if hasattr(request, "full_url")
            else str(request)
        )
        requested_urls.append(url)
        if url.endswith("/shutdown"):
            shutdown_sent = True
            headers = {
                key.lower(): value
                for key, value in request.header_items()
            }
            shutdown_tokens.append(headers["x-gptbridge-shutdown-token"])
            return Response()
        if shutdown_sent:
            raise urllib_error.URLError("stopped")
        return Response(
            json.dumps(
                {"workspace_instance_id": expected_instance}
            ).encode("utf-8")
        )

    monkeypatch.setattr(
        package_platform_tools.urllib_request,
        "urlopen",
        fake_urlopen,
    )

    assert package_platform_tools.stop_verified_packaged_backend(
        tool_id,
        tmp_path / "dist" / "resources" / "app",
    ) is True
    assert all(":31001/" not in url for url in requested_urls)
    assert shutdown_tokens == [legacy_token]


def test_packager_tries_valid_legacy_owner_after_foreign_current_health(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    tool_id = "file-sorter"
    expected_root = package_platform_tools.standalone_project_root(tool_id)
    expected_instance = package_platform_tools.workspace_instance_id(expected_root)
    owners = (
        (
            package_platform_tools.standalone_backend_owner_path(tool_id),
            31_011,
            "c" * 64,
        ),
        (
            package_platform_tools.legacy_standalone_backend_owner_path(tool_id),
            31_012,
            "d" * 64,
        ),
    )
    for owner_path, port, token in owners:
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        owner_path.write_text(
            json.dumps(
                {
                    "tool_id": tool_id,
                    "project_root": str(expected_root),
                    "workspace_instance_id": expected_instance,
                    "shutdown_token": token,
                    "backend_port": port,
                }
            ),
            encoding="utf-8",
        )
    requested_urls: list[str] = []
    stopped = False

    class Response:
        status = 200

        def __init__(self, payload: bytes = b"") -> None:
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

        def close(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_urlopen(request, **_kwargs):
        nonlocal stopped
        url = (
            request.full_url
            if hasattr(request, "full_url")
            else str(request)
        )
        requested_urls.append(url)
        if stopped and ":31012/" in url:
            raise urllib_error.URLError("stopped")
        if url.endswith("/shutdown"):
            stopped = True
            return Response()
        workspace = (
            "foreign-workspace"
            if ":31011/" in url
            else expected_instance
        )
        return Response(
            json.dumps({"workspace_instance_id": workspace}).encode("utf-8")
        )

    monkeypatch.setattr(
        package_platform_tools.urllib_request,
        "urlopen",
        fake_urlopen,
    )

    assert package_platform_tools.stop_verified_packaged_backend(
        tool_id,
        tmp_path / "dist" / "resources" / "app",
    ) is True
    assert not any(
        ":31011/" in url and url.endswith("/shutdown")
        for url in requested_urls
    )
    assert any(
        ":31012/" in url and url.endswith("/shutdown")
        for url in requested_urls
    )


def test_platform_tool_packager_prefers_hardlinks_for_runtime_files(tmp_path: Path) -> None:
    package_platform_tools = load_packager_module()

    source = tmp_path / "electron" / "resources" / "app.asar"
    target = tmp_path / "tool" / "resources" / "app.asar"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"shared")

    package_platform_tools.copy_runtime_item(source, target)

    assert target.read_bytes() == b"shared"
    try:
        assert source.samefile(target)
    except OSError:
        assert target.exists()


def test_package_integrity_detects_payload_and_source_changes(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(Path("src-core").resolve()))
    from tasks.package_integrity import (
        PACKAGE_FORMAT_VERSION,
        PACKAGE_METADATA_NAME,
        collect_file_hashes,
        snapshot_digest,
        verify_packaged_app,
    )

    project_root = tmp_path / "project"
    source_file = project_root / "platform_tools" / "demo" / "src" / "main.py"
    core_file = project_root / "src-core" / "main.py"
    app_dir = project_root / "platform_tools" / "demo" / "dist" / "resources" / "app"
    payload_file = app_dir / "main.cjs"
    source_file.parent.mkdir(parents=True)
    core_file.parent.mkdir(parents=True)
    app_dir.mkdir(parents=True)
    source_file.write_text("print('current')\n", encoding="utf-8")
    core_file.write_text("print('packaged runtime')\n", encoding="utf-8")
    payload_file.write_text("module.exports = {}\n", encoding="utf-8")

    source_roots = ["platform_tools/demo/src", "src-core"]
    source_files = collect_file_hashes(project_root, source_roots)
    payload_files = collect_file_hashes(
        app_dir,
        ["."],
        excluded_relative_paths=frozenset({PACKAGE_METADATA_NAME}),
    )
    (app_dir / PACKAGE_METADATA_NAME).write_text(
        json.dumps(
            {
                "format_version": PACKAGE_FORMAT_VERSION,
                "tool_id": "demo",
                "source_roots": source_roots,
                "source_files": source_files,
                "source_digest": snapshot_digest(source_files),
                "payload_files": payload_files,
                "payload_digest": snapshot_digest(payload_files),
            }
        ),
        encoding="utf-8",
    )

    assert verify_packaged_app(app_dir, project_root=project_root)["ok"] is True

    metadata_path = app_dir / PACKAGE_METADATA_NAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source_excluded_paths"] = [
        "platform_tools/demo/src/ignored.json"
    ]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert (
        verify_packaged_app(app_dir, project_root=project_root)["error_code"]
        == "PACKAGE_UNVERIFIED"
    )
    metadata.pop("source_excluded_paths")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    payload_file.write_text("module.exports = { stale: true }\n", encoding="utf-8")
    assert (
        verify_packaged_app(app_dir, project_root=project_root)["error_code"]
        == "PACKAGE_CORRUPT"
    )

    payload_file.write_text("module.exports = {}\n", encoding="utf-8")
    core_file.write_text("print('new main runtime')\n", encoding="utf-8")
    decoupled = verify_packaged_app(app_dir, project_root=project_root)
    assert decoupled["ok"] is True
    assert decoupled["source_check"] == "current-runtime-contract"

    source_file.write_text("print('newer')\n", encoding="utf-8")
    assert (
        verify_packaged_app(app_dir, project_root=project_root)["error_code"]
        == "STALE_PACKAGE"
    )


def test_package_integrity_excludes_only_mutable_rules_and_detects_new_code(
    tmp_path: Path,
) -> None:
    import sys

    sys.path.insert(0, str(Path("src-core").resolve()))
    from tasks.package_integrity import (
        PACKAGE_FORMAT_VERSION,
        PACKAGE_METADATA_NAME,
        collect_file_hashes,
        snapshot_digest,
        verify_packaged_app,
    )

    project_root = tmp_path / "project"
    source_dir = project_root / "platform_tools" / "file-sorter" / "src"
    source_file = source_dir / "main.py"
    mutable_rule = source_dir / "keyword_rules.json"
    app_dir = (
        project_root
        / "platform_tools"
        / "file-sorter"
        / "dist"
        / "resources"
        / "app"
    )
    payload_file = app_dir / "main.cjs"
    source_dir.mkdir(parents=True)
    app_dir.mkdir(parents=True)
    (source_dir.parent / "manifest.json").write_text(
        json.dumps(
            {
                "id": "file-sorter",
                "package": {
                    "source_excludes": [
                        "src/keyword_rules.json",
                        "src/keyword_rules.py",
                        "src/.file-sorter-rules.json",
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    source_file.write_text("print('current')\n", encoding="utf-8")
    mutable_rule.write_text('[{"keyword": "private"}]\n', encoding="utf-8")
    payload_file.write_text("module.exports = {}\n", encoding="utf-8")

    source_roots = ["platform_tools/file-sorter/src"]
    source_excluded_paths = [
        "platform_tools/file-sorter/src/keyword_rules.json",
        "platform_tools/file-sorter/src/keyword_rules.py",
        "platform_tools/file-sorter/src/.file-sorter-rules.json",
    ]
    source_files = collect_file_hashes(
        project_root,
        source_roots,
        excluded_relative_paths=frozenset(source_excluded_paths),
    )
    payload_files = collect_file_hashes(
        app_dir,
        ["."],
        excluded_relative_paths=frozenset({PACKAGE_METADATA_NAME}),
    )
    metadata = {
        "format_version": PACKAGE_FORMAT_VERSION,
        "tool_id": "file-sorter",
        "source_roots": source_roots,
        "source_excluded_paths": source_excluded_paths,
        "source_files": source_files,
        "source_digest": snapshot_digest(source_files),
        "payload_files": payload_files,
        "payload_digest": snapshot_digest(payload_files),
    }
    metadata_path = app_dir / PACKAGE_METADATA_NAME
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert verify_packaged_app(
        app_dir,
        project_root=project_root,
    ) == {
        "ok": True,
        "tool_id": "file-sorter",
        "package_digest": snapshot_digest(payload_files),
        "source_check": "current",
    }

    mutable_rule.write_text('[{"keyword": "changed-user-rule"}]\n', encoding="utf-8")
    assert verify_packaged_app(
        app_dir,
        project_root=project_root,
    )["ok"] is True

    (source_dir / "new_module.py").write_text(
        "VALUE = 'new source'\n",
        encoding="utf-8",
    )
    assert verify_packaged_app(
        app_dir,
        project_root=project_root,
    )["error_code"] == "STALE_PACKAGE"

    (source_dir / "new_module.py").unlink()
    metadata["source_excluded_paths"] = ["../outside.json"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    invalid = verify_packaged_app(app_dir, project_root=project_root)
    assert invalid["ok"] is False
    assert invalid["error_code"] == "PACKAGE_UNVERIFIED"

    metadata["source_excluded_paths"] = [
        *source_excluded_paths,
        "platform_tools/file-sorter/src/main.py",
    ]
    metadata["source_files"] = {}
    metadata["source_digest"] = snapshot_digest({})
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    expanded = verify_packaged_app(app_dir, project_root=project_root)
    assert expanded["ok"] is False
    assert expanded["error_code"] == "PACKAGE_UNVERIFIED"


def test_verify_tool_package_rejects_standalone_semantic_mismatches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    project_root = tmp_path / "project"
    tool_dir = project_root / "platform_tools" / "demo"
    app_dir = tool_dir / "dist" / "resources" / "app"
    app_dir.mkdir(parents=True)
    (tool_dir / "dist" / "demo.exe").write_bytes(b"executable")
    (app_dir / "src-core").mkdir()
    (app_dir / "src-core" / "main.py").write_text(
        "print('backend')\n",
        encoding="utf-8",
    )
    (app_dir / "python").mkdir()
    (app_dir / "python" / "python.exe").write_bytes(b"python")
    (app_dir / "main.cjs").write_text(
        "module.exports = {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(package_platform_tools, "PROJECT_ROOT", project_root)
    tool_id = "demo"
    version = "1.2.3"
    port = package_platform_tools.standalone_backend_port(tool_id)
    source_manifest = {
        "id": tool_id,
        "version": version,
    }
    app_manifest = {
        **source_manifest,
        "standalone": {
            "backend_entry": "src-core/main.py",
            "backend_service_version": version,
            "backend_port": port,
            "isolated_backend": True,
            "protocol_version": 1,
            "python_runtime": "python/python.exe",
        },
    }
    metadata = {
        "format_version": package_platform_tools.PACKAGE_FORMAT_VERSION,
        "tool_id": tool_id,
        "tool_version": version,
        "backend_service_version": version,
        "backend_port": port,
        "isolated_backend": True,
        "protocol_version": 1,
        "backend_entry": "src-core/main.py",
        "python_runtime": "python/python.exe",
    }
    metadata_path = app_dir / package_platform_tools.PACKAGE_METADATA_NAME

    def write_package_documents(
        selected_metadata: dict,
        selected_manifest: dict,
    ) -> None:
        (app_dir / "manifest.json").write_text(
            json.dumps(selected_manifest),
            encoding="utf-8",
        )
        payload_files = package_platform_tools.collect_file_hashes(
            app_dir,
            ["."],
            excluded_relative_paths=frozenset(
                {package_platform_tools.PACKAGE_METADATA_NAME}
            ),
        )
        complete_metadata = {
            **selected_metadata,
            "payload_files": payload_files,
            "payload_digest": package_platform_tools.snapshot_digest(
                payload_files
            ),
        }
        metadata_path.write_text(
            json.dumps(complete_metadata),
            encoding="utf-8",
        )

    write_package_documents(metadata, app_manifest)
    assert package_platform_tools.verify_tool_package(
        tool_id,
        tool_dir,
        source_manifest,
    )["ok"] is True

    metadata_mutations = {
        "tool_id": "other-tool",
        "backend_port": port + 1,
        "isolated_backend": False,
        "protocol_version": 2,
        "backend_entry": "other/main.py",
        "python_runtime": "other/python.exe",
    }
    for field, invalid_value in metadata_mutations.items():
        write_package_documents(
            {**metadata, field: invalid_value},
            app_manifest,
        )
        result = package_platform_tools.verify_tool_package(
            tool_id,
            tool_dir,
            source_manifest,
        )
        assert result["ok"] is False, field
        assert result["error_code"] == "PACKAGE_SEMANTICS_INVALID", field

    invalid_app_manifest = {
        **app_manifest,
        "id": "other-tool",
        "standalone": {
            **app_manifest["standalone"],
            "protocol_version": 2,
        },
    }
    write_package_documents(metadata, invalid_app_manifest)
    result = package_platform_tools.verify_tool_package(
        tool_id,
        tool_dir,
        source_manifest,
    )
    assert result["ok"] is False
    assert result["error_code"] == "PACKAGE_SEMANTICS_INVALID"


def test_atomic_promotion_rolls_back_if_staged_rename_fails(
    tmp_path: Path, monkeypatch
) -> None:
    package_platform_tools = load_packager_module()

    package_root = tmp_path / "build" / "package-id"
    staged_dist = package_root / "dist"
    dist_dir = tmp_path / "dist"
    staged_dist.mkdir(parents=True)
    dist_dir.mkdir()
    (staged_dist / "version.txt").write_text("new", encoding="utf-8")
    (dist_dir / "version.txt").write_text("old", encoding="utf-8")

    real_replace = package_platform_tools.os.replace
    calls = 0

    def fail_second_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("simulated lock")
        real_replace(source, target)

    monkeypatch.setattr(package_platform_tools.os, "replace", fail_second_replace)

    try:
        package_platform_tools.promote_staged_distribution(staged_dist, dist_dir)
    except PermissionError:
        pass
    else:
        raise AssertionError("promotion should have failed")

    assert (dist_dir / "version.txt").read_text(encoding="utf-8") == "old"


def test_atomic_promotion_preserves_runtime_backup_if_rollback_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    package_root = tmp_path / "build" / "package-id"
    staged_dist = package_root / "dist"
    dist_dir = tmp_path / "dist"
    staged_dist.mkdir(parents=True)
    dist_dir.mkdir()
    staged_file = staged_dist / "version.txt"
    live_file = dist_dir / "version.txt"
    previous_dist = package_root / "previous-dist"
    backup_file = previous_dist / "version.txt"
    staged_file.write_text("new", encoding="utf-8")
    live_file.write_text("old", encoding="utf-8")
    real_replace = package_platform_tools.os.replace

    def fail_install_and_rollback(source: Path, target: Path) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if (
            source_path == staged_dist
            or source_path == backup_file
            or source_path == previous_dist
        ) and target_path == dist_dir:
            raise PermissionError(f"simulated lock: {source_path}")
        real_replace(source_path, target_path)

    monkeypatch.setattr(
        package_platform_tools.os,
        "replace",
        fail_install_and_rollback,
    )

    with pytest.raises(
        package_platform_tools.PromotionRecoveryRequired
    ) as captured:
        package_platform_tools.promote_staged_distribution(
            staged_dist,
            dist_dir,
        )

    assert captured.value.recovery_root == package_root
    assert backup_file.read_text(encoding="utf-8") == "old"
    assert staged_file.read_text(encoding="utf-8") == "new"
    assert not dist_dir.exists()
    journal = json.loads(
        (package_root / "promotion-journal.json").read_text(encoding="utf-8")
    )
    aborted = json.loads(
        (package_root / "promotion-aborted.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "prepared"
    assert aborted["status"] == "rollback-incomplete"


def test_atomic_promotion_retains_complete_previous_dist_and_unknown_data(
    tmp_path: Path,
) -> None:
    package_platform_tools = load_packager_module()
    package_root = tmp_path / "build" / "package-id"
    staged_dist = package_root / "dist"
    dist_dir = tmp_path / "dist"
    staged_app = staged_dist / "resources" / "app"
    live_app = dist_dir / "resources" / "app"
    staged_app.mkdir(parents=True)
    live_app.mkdir(parents=True)
    (staged_app / "version.txt").write_text("new", encoding="utf-8")
    (live_app / "version.txt").write_text("old", encoding="utf-8")
    (live_app / "unknown-user-state.db").write_bytes(b"only old copy")
    (dist_dir / "old-runtime.dll").write_bytes(b"old runtime")
    (staged_dist / "new-runtime.dll").write_bytes(b"new runtime")
    (live_app / ".gptbridge-package.json").write_text(
        json.dumps(
            {
                "payload_files": {
                    "version.txt": "old-digest",
                },
                "payload_digest": "d" * 64,
            }
        ),
        encoding="utf-8",
    )

    recovery_root = package_platform_tools.promote_staged_distribution(
        staged_dist,
        dist_dir,
    )

    assert recovery_root == package_root
    previous_dist = package_root / "previous-dist"
    assert (previous_dist / "resources" / "app" / "version.txt").read_text(
        encoding="utf-8"
    ) == "old"
    assert (
        previous_dist
        / "resources"
        / "app"
        / "unknown-user-state.db"
    ).read_bytes() == b"only old copy"
    assert (previous_dist / "old-runtime.dll").read_bytes() == b"old runtime"
    assert (dist_dir / "resources" / "app" / "version.txt").read_text(
        encoding="utf-8"
    ) == "new"
    assert not (
        dist_dir / "resources" / "app" / "unknown-user-state.db"
    ).exists()
    manifest = json.loads(
        (
            package_root
            / "promotion-recovery-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "retained"
    assert manifest["previous_tree_digest"]
    assert (
        "resources/app/unknown-user-state.db"
        in manifest["unknown_live_app_paths"]
    )
    assert "old-runtime.dll" in manifest["unknown_live_runtime_paths"]
    assert (package_root / "promotion-complete.json").is_file()


def test_promotion_uses_verified_in_place_fallback_when_root_is_watched(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    package_root = tmp_path / "build" / "package-id"
    staged_dist = package_root / "dist"
    dist_dir = tmp_path / "dist"
    (staged_dist / "resources" / "app").mkdir(parents=True)
    (dist_dir / "resources" / "app").mkdir(parents=True)
    (staged_dist / "version.txt").write_text("new", encoding="utf-8")
    (staged_dist / "resources" / "app" / "main.py").write_text(
        "print('new')\n",
        encoding="utf-8",
    )
    (dist_dir / "version.txt").write_text("old", encoding="utf-8")
    (dist_dir / "old-only.dat").write_bytes(b"retain old-only")
    (dist_dir / "resources" / "app" / "main.py").write_text(
        "print('old')\n",
        encoding="utf-8",
    )

    real_replace = package_platform_tools.os.replace

    def deny_only_live_root_rename(source: Path, target: Path) -> None:
        if Path(source) == dist_dir and Path(target) == package_root / "previous-dist":
            raise PermissionError("synthetic frontend watcher")
        real_replace(source, target)

    monkeypatch.setattr(
        package_platform_tools.os,
        "replace",
        deny_only_live_root_rename,
    )

    recovery_root = package_platform_tools.promote_staged_distribution(
        staged_dist,
        dist_dir,
    )

    assert recovery_root == package_root
    assert (dist_dir / "version.txt").read_text(encoding="utf-8") == "new"
    assert (
        dist_dir / "resources" / "app" / "main.py"
    ).read_text(encoding="utf-8") == "print('new')\n"
    assert not (dist_dir / "old-only.dat").exists()
    assert (
        package_root / "previous-dist" / "old-only.dat"
    ).read_bytes() == b"retain old-only"
    assert (
        package_root
        / "retired-live-paths"
        / "removed-from-live"
        / "old-only.dat"
    ).read_bytes() == b"retain old-only"
    manifest = json.loads(
        (
            package_root / "promotion-recovery-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["strategy"] == "verified-in-place"


def test_package_operation_lock_is_per_tool_and_fail_closed(
    tmp_path: Path,
) -> None:
    package_platform_tools = load_packager_module()
    tool_dir = tmp_path / "platform_tools" / "demo"
    tool_dir.mkdir(parents=True)

    with package_platform_tools.package_operation_lock("demo", tool_dir):
        with pytest.raises(package_platform_tools.PackageOperationBusy):
            with package_platform_tools.package_operation_lock(
                "demo",
                tool_dir,
            ):
                raise AssertionError("second package lock must not be acquired")

    assert not (
        tool_dir / "build" / ".package-demo.lock"
    ).exists()


def test_package_tool_success_keeps_verified_previous_distribution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    project_root = tmp_path / "project"
    tool_dir = project_root / "platform_tools" / "demo"
    entry = tool_dir / "src" / "main.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("print('demo')\n", encoding="utf-8")
    manifest = {
        "id": "demo",
        "version": "2.0.0",
        "runtime": {"entry": "src/main.py"},
    }
    electron_dir = tmp_path / "electron"
    electron_dir.mkdir()
    (electron_dir / "electron.exe").write_bytes(b"electron")
    renderer_dir = tmp_path / "renderer"
    renderer_dir.mkdir()
    (renderer_dir / "index.html").write_text("demo", encoding="utf-8")
    old_dist = tool_dir / "dist"
    old_app = old_dist / "resources" / "app"
    old_app.mkdir(parents=True)
    (old_app / "old-only.db").write_bytes(b"only previous copy")
    (old_dist / "old-runtime.dll").write_bytes(b"old runtime")

    monkeypatch.setattr(package_platform_tools, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(
        package_platform_tools,
        "ELECTRON_DIST_DIR",
        electron_dir,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "build_platform_renderer",
        lambda _tool_id: {
            "ok": True,
            "renderer_path": str(renderer_dir),
        },
    )
    monkeypatch.setattr(
        package_platform_tools,
        "running_executable_process_ids",
        lambda _path: [],
    )
    monkeypatch.setattr(
        package_platform_tools,
        "package_source_roots",
        lambda _tool_dir, _entry: [
            "platform_tools/demo/src/main.py"
        ],
    )
    monkeypatch.setattr(
        package_platform_tools,
        "package_source_excluded_paths",
        lambda _tool_dir, _entry: [],
    )

    def fake_copy_electron(staged_dist: Path, staged_exe: Path) -> None:
        staged_dist.mkdir(parents=True)
        staged_exe.write_bytes(b"new executable")
        (staged_dist / "new-runtime.dll").write_bytes(b"new runtime")

    def fake_copy_backend(
        tool_id: str,
        _tool_dir: Path,
        _entry: Path,
        app_dir: Path,
    ) -> Path:
        (app_dir / "src-core").mkdir()
        (app_dir / "src-core" / "main.py").write_text(
            "print('backend')\n",
            encoding="utf-8",
        )
        runtime = app_dir / "platform_tools" / tool_id / "src"
        runtime.mkdir(parents=True)
        (runtime / "main.py").write_text(
            "print('demo')\n",
            encoding="utf-8",
        )
        return runtime

    def fake_copy_python(app_dir: Path) -> Path:
        runtime = app_dir / "python"
        runtime.mkdir()
        executable = runtime / "python.exe"
        executable.write_bytes(b"python")
        return executable

    def fake_copy_templates(app_dir: Path) -> None:
        (app_dir / "main.cjs").write_text(
            "module.exports = {}\n",
            encoding="utf-8",
        )
        (app_dir / "preload.cjs").write_text(
            "module.exports = {}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        package_platform_tools,
        "copy_electron_runtime",
        fake_copy_electron,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "copy_backend_source_bundle",
        fake_copy_backend,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "copy_portable_python_runtime",
        fake_copy_python,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "copy_app_templates",
        fake_copy_templates,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "validate_staged_python_runtime",
        lambda _app_dir, _tool_id: None,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "verify_packaged_app",
        lambda _app_dir, project_root: {
            "ok": True,
            "package_digest": "a" * 64,
        },
    )
    monkeypatch.setattr(
        package_platform_tools,
        "stop_verified_packaged_backend",
        lambda _tool_id, _app_dir: False,
    )

    result = package_platform_tools.package_tool(
        "demo",
        tool_dir,
        manifest,
    )

    assert result["ok"] is True
    recovery_root = Path(result["recovery_path"])
    assert recovery_root.is_dir()
    assert (
        recovery_root
        / "previous-dist"
        / "resources"
        / "app"
        / "old-only.db"
    ).read_bytes() == b"only previous copy"
    assert (
        recovery_root / "previous-dist" / "old-runtime.dll"
    ).read_bytes() == b"old runtime"
    assert (
        recovery_root / "promotion-recovery-manifest.json"
    ).is_file()
    assert (tool_dir / "dist" / "new-runtime.dll").read_bytes() == b"new runtime"


def test_package_tool_does_not_cleanup_incomplete_rollback_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    project_root = tmp_path / "project"
    tool_dir = project_root / "platform_tools" / "demo"
    entry = tool_dir / "src" / "main.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("print('demo')\n", encoding="utf-8")
    manifest = {
        "id": "demo",
        "version": "1.0.0",
        "runtime": {"entry": "src/main.py"},
    }
    electron_dir = tmp_path / "electron"
    electron_dir.mkdir()
    (electron_dir / "electron.exe").write_bytes(b"electron")
    renderer_dir = tmp_path / "renderer"
    renderer_dir.mkdir()
    (renderer_dir / "index.html").write_text("demo", encoding="utf-8")
    monkeypatch.setattr(package_platform_tools, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(
        package_platform_tools,
        "ELECTRON_DIST_DIR",
        electron_dir,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "build_platform_renderer",
        lambda _tool_id: {
            "ok": True,
            "renderer_path": str(renderer_dir),
        },
    )
    monkeypatch.setattr(
        package_platform_tools,
        "running_executable_process_ids",
        lambda _path: [],
    )
    monkeypatch.setattr(
        package_platform_tools,
        "package_source_roots",
        lambda _tool_dir, _entry: [
            "platform_tools/demo/src/main.py"
        ],
    )
    monkeypatch.setattr(
        package_platform_tools,
        "package_source_excluded_paths",
        lambda _tool_dir, _entry: [],
    )

    def fake_copy_electron(staged_dist: Path, staged_exe: Path) -> None:
        staged_dist.mkdir(parents=True)
        staged_exe.write_bytes(b"executable")

    def fake_copy_backend(
        tool_id: str,
        _tool_dir: Path,
        _entry: Path,
        app_dir: Path,
    ) -> Path:
        runtime = app_dir / "platform_tools" / tool_id / "src"
        runtime.mkdir(parents=True)
        (runtime / "main.py").write_text("print('demo')\n", encoding="utf-8")
        return runtime

    def fake_copy_python(app_dir: Path) -> Path:
        runtime = app_dir / "python"
        runtime.mkdir()
        executable = runtime / "python.exe"
        executable.write_bytes(b"python")
        return executable

    def fake_copy_templates(app_dir: Path) -> None:
        (app_dir / "main.cjs").write_text("module.exports = {}\n", encoding="utf-8")
        (app_dir / "preload.cjs").write_text(
            "module.exports = {}\n",
            encoding="utf-8",
        )

    def fail_promotion(staged_dist: Path, _dist_dir: Path) -> None:
        recovery_root = staged_dist.parent
        recovery_file = recovery_root / "previous-app" / "old.txt"
        recovery_file.parent.mkdir(parents=True)
        recovery_file.write_text("old app", encoding="utf-8")
        raise package_platform_tools.PromotionRecoveryRequired(
            "simulated incomplete rollback.",
            recovery_root=recovery_root,
            rollback_errors=["simulated rollback failure"],
        )

    monkeypatch.setattr(
        package_platform_tools,
        "copy_electron_runtime",
        fake_copy_electron,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "copy_backend_source_bundle",
        fake_copy_backend,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "copy_portable_python_runtime",
        fake_copy_python,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "copy_app_templates",
        fake_copy_templates,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "validate_staged_python_runtime",
        lambda _app_dir, _tool_id: None,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "stop_verified_packaged_backend",
        lambda _tool_id, _app_dir: False,
    )
    monkeypatch.setattr(
        package_platform_tools,
        "promote_staged_distribution",
        fail_promotion,
    )

    result = package_platform_tools.package_tool("demo", tool_dir, manifest)
    recovery_root = Path(result["recovery_path"])

    assert result["ok"] is False
    assert result["error_code"] == "PROMOTION_ROLLBACK_INCOMPLETE"
    assert recovery_root.exists()
    assert (recovery_root / "previous-app" / "old.txt").read_text(
        encoding="utf-8"
    ) == "old app"


def test_standalone_wrapper_uses_verified_packaged_backend() -> None:
    source = Path("scripts/templates/platform-tool-app/main.cjs").read_text(
        encoding="utf-8"
    )

    assert "const packagedBackend = path.join(appRoot, 'src-core', 'main.py')" in source
    assert "verifyPackagedPayload()" in source
    assert "pruneGeneratedPythonBytecode(expectedPaths)" in source
    assert "package.bytecodeRepaired" in source
    assert "package.bytecodeRepairFailed" in source
    assert "cacheEntry.name.endsWith('.pyc')" in source
    assert "GPTBRIDGE_PACKAGE_DIGEST" in source
    assert "backendOwnerMatches(owner, projectRoot, verification)" in source
    assert "!app.isPackaged &&" in source
    assert "process.env.GPTBRIDGE_BACKEND_HOT_RELOAD === '1'" in source
    assert "stdio: ['ignore', backendLogDescriptor, backendLogDescriptor]" in source
    assert "backend.processExit" in source
    assert "compactBackendLog" in source
    assert "PYTHONDONTWRITEBYTECODE: '1'" in source
    assert "PYTHONNOUSERSITE: '1'" in source
    assert "'-B'," in source
    assert "'-s'," in source
    assert "'-E'," in source
    assert "'utf8'," in source


def test_production_upgrade_stops_only_reverified_tool_processes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    executable = tmp_path / "demo.exe"
    executable.write_bytes(b"demo")
    alive = {1201, 1202}
    terminated: list[int] = []

    monkeypatch.setattr(package_platform_tools.os, "name", "nt")
    monkeypatch.setattr(
        package_platform_tools,
        "running_executable_process_ids",
        lambda _path: sorted(alive),
    )

    def fake_run(command, **_kwargs):
        process_id = int(command[2])
        assert process_id in alive
        terminated.append(process_id)
        alive.discard(process_id)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(package_platform_tools.subprocess, "run", fake_run)

    assert package_platform_tools.stop_running_executable_for_upgrade(
        executable,
        [1201, 1202, 9999],
    )
    assert terminated == [1201, 1202]
    assert not alive


def test_production_upgrade_restart_drops_electron_node_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_platform_tools = load_packager_module()
    executable = tmp_path / "demo.exe"
    executable.write_bytes(b"demo")
    captured: dict[str, object] = {}
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setenv("GPTBRIDGE_START_HIDDEN", "1")
    monkeypatch.setattr(package_platform_tools.os, "name", "nt")

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return object()

    monkeypatch.setattr(package_platform_tools.subprocess, "Popen", fake_popen)

    assert package_platform_tools.restart_packaged_executable(executable)
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert "ELECTRON_RUN_AS_NODE" not in environment
    assert "GPTBRIDGE_START_HIDDEN" not in environment


def test_managed_tool_launch_reuses_only_matching_shared_backend() -> None:
    source = Path("scripts/templates/platform-tool-app/main.cjs").read_text(
        encoding="utf-8"
    )

    assert "'GPTBRIDGE_TRUSTED_MANAGED_BACKEND_REUSE'" in source
    assert "'GPTBRIDGE_MANAGED_BACKEND_TOOL_ID'" in source
    assert "'GPTBRIDGE_MANAGED_BACKEND_WORKSPACE_INSTANCE_ID'" in source
    assert "'GPTBRIDGE_MANAGED_BACKEND_VERSION'" in source
    assert "managedBackendReuseRequested() && explicit" in source
    assert "managedBackendCapability(projectRoot, verification)" in source
    assert "fs.realpathSync(explicitRoot)" in source
    assert "workspaceInstanceId(canonicalRoot)" in source
    assert "String(workspaceManifest.version || '').trim() !== requiredVersion" in source
    assert "if (managedBackendReuseRequested()) {" in source
    assert "backendMatchesWorkspace(health, projectRoot)" in source
    assert "backendHasCurrentTool(health, projectRoot, verification)" in source
    assert "MANAGED_BACKEND_PROJECT_ROOT_MISSING" in source
    assert "MANAGED_BACKEND_CAPABILITY_MISMATCH" in source
    assert "MANAGED_BACKEND_UNAVAILABLE" in source
    assert "MANAGED_BACKEND_SERVICE_VERSION_MISMATCH" in source
    assert "backend.managedReuse" in source
    assert "managedBackend: true" in source
    assert "GPTBRIDGE_ALLOW_EXTERNAL_PROJECT_ROOT" not in source
    managed_branch = source.index("if (managedBackendReuseRequested()) {", source.index("async function ensureBackendStarted"))
    standalone_token = source.index(
        "const sessionToken = backendSessionToken()",
        managed_branch,
    )
    assert managed_branch < source.index("return {", managed_branch) < standalone_token


def test_packaged_wrapper_rejects_untrusted_runtime_and_external_paths() -> None:
    source = Path("scripts/templates/platform-tool-app/main.cjs").read_text(
        encoding="utf-8"
    )

    assert "if (app.isPackaged || !app.commandLine.hasSwitch('user-data-dir'))" in source
    assert "'GPTBridge'," in source
    assert "'standalone'," in source
    assert "stableToolId" in source
    assert "cannot traverse a link or junction" in source
    assert "if (app.isPackaged) {" in source
    assert "delete backendEnvironment.GPTBRIDGE_PYTHON" in source
    assert "isPathInside(canonicalAppRoot, canonicalCandidate)" in source
    assert "consumeOpenPathCapability(canonicalTargetPath)" in source
    assert "capabilities.toolbox" in source
    assert "commands.includes('toolbox_run_tool')" in source
    assert "commands.includes('toolbox_cancel_tool_run')" in source
    assert (
        "!isPathInside(projectRoot, targetPath) && !path.isAbsolute(rawPath)"
        not in source
    )
    assert "TOOL_OUTSIDE_STANDALONE_SCOPE" not in source


def test_ai_assistant_socket_never_queues_mutations() -> None:
    source = Path(
        "platform_tools/ai-assistant/src/ui/backendSocket.ts"
    ).read_text(encoding="utf-8")
    app_source = Path(
        "platform_tools/ai-assistant/src/ui/AiAssistantWindowApp.tsx"
    ).read_text(encoding="utf-8")

    assert "queueRef" not in source
    assert "queued: true" not in source
    assert "後端連線尚未就緒，指令未送出" in source
    assert "cancelQueuedCommands" in source
    assert "waitUntilConnected" in source
    assert "result.ok !== true" in source
    assert "protocolVersion" in source
    assert app_source.index("await waitUntilConnected(15_000)") < app_source.index(
        "sendCommand(command"
    )


def test_desktop_builder_excludes_platform_tool_runtime_data() -> None:
    package = json.loads(Path("package.json").read_text(encoding="utf-8"))
    platform_copy = next(
        entry
        for entry in package["build"]["extraResources"]
        if entry.get("from") == "platform_tools"
    )
    filters = set(platform_copy["filter"])

    assert "*/src/**/*" in filters
    assert "!**/runtime/**" in filters
    assert "!**/imports/**" in filters
    assert "!**/exports/**" in filters
    assert "!**/*.sqlite3" in filters
    assert "!**/*.ivault" in filters
    assert "!**/*.jsonl" in filters
