const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require('electron')
const childProcess = require('node:child_process')
const crypto = require('node:crypto')
const fs = require('node:fs')
const http = require('node:http')
const net = require('node:net')
const os = require('node:os')
const path = require('node:path')

const appRoot = __dirname
const manifestPath = path.join(appRoot, 'manifest.json')
const packageMetadataPath = path.join(appRoot, '.gptbridge-package.json')
const runtimeLogMaxBytes = 2 * 1024 * 1024
const backendLogMaxBytes = 4 * 1024 * 1024

function managedToolLogDirectory() {
  const managedRoot = String(process.env.GPTBRIDGE_MANAGED_STORAGE_ROOT || '').trim()
  const toolId = String(process.env.GPTBRIDGE_TOOL_ID || 'unknown-tool')
    .trim()
    .replace(/[^a-z0-9_-]/gi, '-') || 'unknown-tool'
  return managedRoot
    ? path.join(managedRoot, 'logs', toolId)
    : path.join(app.getPath('userData'), 'logs')
}

function rotateLogFile(logPath, maxBytes = runtimeLogMaxBytes) {
  try {
    if (!fs.existsSync(logPath) || fs.statSync(logPath).size <= maxBytes) return
    const archivePath = `${logPath}.${Date.now()}-${crypto
      .randomBytes(4)
      .toString('hex')}.archive`
    fs.renameSync(logPath, archivePath)
  } catch {}
}

function boundedLogValue(value, maxLength = 16000) {
  const text = String(value == null ? '' : value)
  return text.length <= maxLength ? text : `${text.slice(0, maxLength)}…`
}

function writeRuntimeLog(event, payload = {}) {
  if (governedChannelRuntimeRequested()) return
  try {
    const logDir = managedToolLogDirectory()
    fs.mkdirSync(logDir, { recursive: true })
    const logPath = path.join(logDir, 'tool-runtime.log')
    rotateLogFile(logPath)
    fs.appendFileSync(
      logPath,
      `[${new Date().toISOString()}] ${event} ${boundedLogValue(
        JSON.stringify(payload)
      )}\n`,
      'utf-8'
    )
  } catch {}
}

process.on('uncaughtException', (error) => {
  writeRuntimeLog('uncaughtException', {
    message: error.message,
    stack: error.stack,
  })
})

process.on('unhandledRejection', (reason) => {
  writeRuntimeLog('unhandledRejection', {
    message: reason instanceof Error ? reason.message : String(reason),
    stack: reason instanceof Error ? reason.stack : undefined,
  })
})

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf-8'))
  } catch {
    return {}
  }
}

const manifest = readJson(manifestPath)
const packageMetadata = readJson(packageMetadataPath)
const toolId = String(
  app.isPackaged
    ? manifest.id || ''
    : process.env.GPTBRIDGE_TOOL_ID || manifest.id || ''
).trim()
const toolName = String(manifest.name || toolId || 'GPTBridge Application')
const startHidden =
  String(process.env.GPTBRIDGE_START_HIDDEN || '').trim() === '1'
let mainWindow = null
let mainWindowReady = false
let rendererReloadTimer = null
let rendererWatchedPath = ''
let foregroundRequested = !startHidden
let backendProcess = null
let backendLastFailure = ''
let packageVerification = null
let preparedBackendPackageDigest = ''
let quitAfterBackendShutdown = false
const oneTimeOpenPathCapabilities = new Map()
const trustedManagedBackendReuseEnv =
  'GPTBRIDGE_TRUSTED_MANAGED_BACKEND_REUSE'
const managedBackendToolIdEnv = 'GPTBRIDGE_MANAGED_BACKEND_TOOL_ID'
const managedBackendWorkspaceIdEnv =
  'GPTBRIDGE_MANAGED_BACKEND_WORKSPACE_INSTANCE_ID'
const managedBackendVersionEnv = 'GPTBRIDGE_MANAGED_BACKEND_VERSION'
const backendPortEnv = 'GPTBRIDGE_IPC_PORT'

function governedChannelRuntimeRequested() {
  const standalone =
    manifest.standalone && typeof manifest.standalone === 'object'
      ? manifest.standalone
      : {}
  return standalone.governed_channel === 'shared-layer'
}

app.setName(toolName)

if (app.isPackaged || !app.commandLine.hasSwitch('user-data-dir')) {
  const stableToolId =
    toolId.replace(/[^0-9A-Za-z_.-]+/g, '_').trim() || 'unknown-tool'
  let appDataRoot = fs.realpathSync(app.getPath('appData'))
  const visualSmokeRoot = String(
    process.env.GPTBRIDGE_VISUAL_SMOKE_APP_DATA_ROOT || ''
  ).trim()
  if (visualSmokeRoot) {
    const temporaryRoot = fs.realpathSync(os.tmpdir())
    const smokeSandboxRoot = path.dirname(temporaryRoot)
    const candidate = path.resolve(visualSmokeRoot)
    const relativeCandidate = path.relative(smokeSandboxRoot, candidate)
    if (
      !path.basename(smokeSandboxRoot).startsWith(
        'gptbridge-tool-visual-'
      ) ||
      !relativeCandidate ||
      relativeCandidate.startsWith('..') ||
      path.isAbsolute(relativeCandidate)
    ) {
      throw new Error('Visual smoke appData override is outside its owned sandbox.')
    }
    let currentSmokePath = smokeSandboxRoot
    for (const part of relativeCandidate.split(path.sep)) {
      if (!part) continue
      currentSmokePath = path.join(currentSmokePath, part)
      if (fs.existsSync(currentSmokePath)) {
        const info = fs.lstatSync(currentSmokePath)
        if (info.isSymbolicLink()) {
          throw new Error(
            `Visual smoke appData cannot traverse a link: ${currentSmokePath}`
          )
        }
      } else {
        fs.mkdirSync(currentSmokePath)
      }
    }
    appDataRoot = fs.realpathSync(candidate)
  }
  const governedToolDataRoot = String(
    process.env.GPTBRIDGE_TOOL_DATA_ROOT || ''
  ).trim()
  if (governedChannelRuntimeRequested() && governedToolDataRoot) {
    const expectedToolRoot = path.resolve(
      String(process.env.GPTBRIDGE_TOOL_DIR || '')
    )
    const resolvedDataRoot = path.resolve(governedToolDataRoot)
    if (
      !expectedToolRoot ||
      !isPathInside(expectedToolRoot, resolvedDataRoot)
    ) {
      throw new Error('PERMISSION_DENIED')
    }
    fs.mkdirSync(resolvedDataRoot, { recursive: true })
    appDataRoot = fs.realpathSync(resolvedDataRoot)
  }
  const toolUserDataPath = governedChannelRuntimeRequested()
    ? path.join(appDataRoot, 'electron-user-data')
    : path.join(
        appDataRoot,
        'GPTBridge',
        'standalone',
        stableToolId,
        'electron-user-data'
      )
  let currentProfilePath = appDataRoot
  for (const part of path.relative(appDataRoot, toolUserDataPath).split(path.sep)) {
    if (!part) continue
    currentProfilePath = path.join(currentProfilePath, part)
    if (fs.existsSync(currentProfilePath)) {
      const info = fs.lstatSync(currentProfilePath)
      if (info.isSymbolicLink()) {
        throw new Error(
          `Standalone userData path cannot traverse a link or junction: ${currentProfilePath}`
        )
      }
    } else {
      fs.mkdirSync(currentProfilePath)
    }
  }
  fs.mkdirSync(toolUserDataPath, { recursive: true })
  const canonicalUserDataPath = fs.realpathSync(toolUserDataPath)
  const relativeUserDataPath = path.relative(appDataRoot, canonicalUserDataPath)
  if (
    relativeUserDataPath.startsWith('..') ||
    path.isAbsolute(relativeUserDataPath)
  ) {
    throw new Error('Standalone userData path escaped the application data root.')
  }
  app.setPath('userData', toolUserDataPath)
}

function validBackendPort(value) {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 1024 && parsed <= 65535
    ? parsed
    : 0
}

function resolveBackendPort() {
  const standalone =
    manifest.standalone && typeof manifest.standalone === 'object'
      ? manifest.standalone
      : {}
  if (app.isPackaged && standalone.isolated_backend !== false) {
    return validBackendPort(standalone.backend_port) || 8765
  }
  const configured = validBackendPort(process.env[backendPortEnv])
  if (configured) return configured
  if (managedBackendReuseRequested()) return 8765
  return validBackendPort(standalone.backend_port) || 8765
}

const backendPort = resolveBackendPort()
const hasSingleInstanceLock = app.requestSingleInstanceLock({
  background: startHidden,
})

if (!hasSingleInstanceLock) {
  writeRuntimeLog('singleInstance.lock.failed', { toolId })
  app.quit()
} else {
  app.on('second-instance', (_event, _argv, _cwd, additionalData) => {
    const background = additionalData?.background === true
    writeRuntimeLog('singleInstance.secondInstance', { toolId, background })
    if (background) {
      writeRuntimeLog('singleInstance.backgroundProbe', { toolId })
      return
    }
    requestMainWindowForeground('second-instance')
  })
}

function managedBackendReuseRequested() {
  const standalone =
    manifest.standalone && typeof manifest.standalone === 'object'
      ? manifest.standalone
      : {}
  if (standalone.isolated_backend !== false) return false
  return (
    app.isPackaged &&
    process.env[trustedManagedBackendReuseEnv] === '1'
  )
}

function inferProjectRoot() {
  if (governedChannelRuntimeRequested()) {
    const governedRoot = String(
      process.env.GPTBRIDGE_GOVERNANCE_PROJECT_ROOT || ''
    ).trim()
    if (governedRoot) return path.resolve(governedRoot)
  }
  const explicit = String(process.env.GPTBRIDGE_PROJECT_ROOT || '').trim()
  const packagedBackend = path.join(appRoot, 'src-core', 'main.py')
  const packagedToolManifest = path.join(
    appRoot,
    'platform_tools',
    toolId,
    'manifest.json'
  )
  const packagedBundleAvailable =
    fs.existsSync(packagedBackend) && fs.existsSync(packagedToolManifest)

  if (packagedBundleAvailable && app.isPackaged) {
    if (managedBackendReuseRequested() && explicit) {
      return path.resolve(explicit)
    }
    const safeToolId = toolId.replace(/[^0-9A-Za-z_.-]+/g, '_') || 'tool'
    if (process.platform === 'win32') {
      const localAppData = String(process.env.LOCALAPPDATA || '').trim()
      const base = localAppData || path.join(os.homedir(), 'AppData', 'Local')
      return path.resolve(base, 'GPTBridge', 'standalone', safeToolId)
    }
    const xdgStateHome = String(process.env.XDG_STATE_HOME || '').trim()
    const base = xdgStateHome || path.join(os.homedir(), '.local', 'state')
    return path.resolve(base, 'GPTBridge', 'standalone', safeToolId)
  }
  if (packagedBundleAvailable) return appRoot

  if (explicit) {
    const resolvedExplicit = path.resolve(explicit)
    if (!app.isPackaged || resolvedExplicit === path.resolve(appRoot)) {
      return resolvedExplicit
    }
    writeRuntimeLog('projectRoot.externalBlocked', {
      explicit: resolvedExplicit,
      reason: 'Packaged applications cannot use an external workspace by default',
    })
  }

  const exeDir = path.dirname(process.execPath)
  const candidates = [
    path.resolve(exeDir, '..', '..', '..'),
    path.resolve(exeDir, '..', '..', '..', '..'),
    path.resolve(process.cwd(), '..', '..'),
    process.cwd(),
  ]

  if (!app.isPackaged) {
    for (const candidate of candidates) {
      if (fs.existsSync(path.join(candidate, 'src-core', 'main.py'))) {
        return candidate
      }
    }
  }
  return appRoot
}

function migrateLegacyFileSorterProfiles(projectRoot) {
  if (toolId !== 'file-sorter') {
    return { migrated: false, reason: 'not-file-sorter' }
  }

  const localAppData = String(process.env.LOCALAPPDATA || '').trim()
  const stateBase = path.resolve(
    localAppData || path.join(os.homedir(), 'AppData', 'Local')
  )
  const legacyStateRoot = path.resolve(stateBase, 'GPTBridge', 'file-sorter')
  const legacyProfiles = path.join(legacyStateRoot, 'profiles')
  const currentStateRoot = path.join(
    path.resolve(projectRoot),
    'runtime',
    'file-sorter-state'
  )
  const currentProfiles = path.join(currentStateRoot, 'profiles')

  if (!fs.existsSync(legacyProfiles)) {
    return { migrated: false, reason: 'legacy-profiles-missing' }
  }
  if (fs.existsSync(currentProfiles)) {
    return { migrated: false, reason: 'current-profiles-exist' }
  }

  const sourceInventory = inventoryOwnedRuntimeTree(legacyProfiles)
  const stagingProfiles = path.join(
    currentStateRoot,
    `.legacy-profiles-${process.pid}-${crypto.randomBytes(8).toString('hex')}`
  )
  fs.mkdirSync(currentStateRoot, { recursive: true, mode: 0o700 })
  hardenPrivatePath(currentStateRoot, { directory: true })
  try {
    fs.cpSync(legacyProfiles, stagingProfiles, {
      recursive: true,
      errorOnExist: true,
    })
    const stagedInventory = inventoryOwnedRuntimeTree(stagingProfiles)
    if (stagedInventory.tree_digest !== sourceInventory.tree_digest) {
      throw new Error('Legacy file-sorter profile migration digest mismatch.')
    }
    fs.renameSync(stagingProfiles, currentProfiles)
    hardenPrivatePath(currentProfiles, { directory: true })
    writeRuntimeLog('fileSorter.legacyProfilesMigrated', {
      source: legacyProfiles,
      destination: currentProfiles,
      treeDigest: sourceInventory.tree_digest,
    })
    return {
      migrated: true,
      source: legacyProfiles,
      destination: currentProfiles,
      treeDigest: sourceInventory.tree_digest,
    }
  } catch (error) {
    if (
      fs.existsSync(stagingProfiles) &&
      isPathInside(currentStateRoot, stagingProfiles) &&
      path.basename(stagingProfiles).startsWith('.legacy-profiles-')
    ) {
      fs.rmSync(stagingProfiles, { recursive: true, force: true })
    }
    throw error
  }
}

function applyDeclaredEnvironmentBindings(environment) {
  const declaration =
    manifest.environment && typeof manifest.environment === 'object'
      ? manifest.environment
      : {}
  const bindings =
    declaration.bindings && typeof declaration.bindings === 'object'
      ? declaration.bindings
      : {}
  for (const [rawKey, source] of Object.entries(bindings)) {
    const key = String(rawKey || '').trim().toUpperCase()
    if (!/^[A-Z][A-Z0-9_]{1,127}$/.test(key)) continue
    if (source !== 'project_root' && source !== 'tool_root') continue
    const configured = String(process.env[key] || '').trim()
    if (!configured) {
      delete environment[key]
      continue
    }
    try {
      const canonical = fs.realpathSync.native(path.resolve(configured))
      const expectedRoot = canonical
      const expectedManifest =
        source === 'project_root'
          ? path.join(canonical, 'platform_tools', toolId, 'manifest.json')
          : path.join(expectedRoot, 'manifest.json')
      if (
        !fs.existsSync(path.join(expectedRoot, 'package.json')) &&
        source === 'project_root'
      ) {
        delete environment[key]
        continue
      }
      if (!fs.existsSync(expectedManifest)) {
        delete environment[key]
        continue
      }
      environment[key] = canonical
    } catch {
      delete environment[key]
    }
  }
}

function sha256RegularFile(filePath) {
  const digest = crypto.createHash('sha256')
  const descriptor = fs.openSync(filePath, 'r')
  const buffer = Buffer.allocUnsafe(1024 * 1024)
  try {
    while (true) {
      const bytesRead = fs.readSync(
        descriptor,
        buffer,
        0,
        buffer.length,
        null
      )
      if (bytesRead === 0) break
      digest.update(buffer.subarray(0, bytesRead))
    }
  } finally {
    fs.closeSync(descriptor)
  }
  return digest.digest('hex')
}

function inventoryOwnedRuntimeTree(rootPath) {
  const entries = []
  const visit = (currentPath) => {
    const currentStat = fs.lstatSync(currentPath)
    if (currentStat.isSymbolicLink()) {
      throw new Error(
        `Runtime recovery inventory cannot include links or junctions: ${currentPath}`
      )
    }
    const relativePath = path
      .relative(rootPath, currentPath)
      .split(path.sep)
      .join('/')
    if (currentStat.isDirectory()) {
      if (relativePath) {
        entries.push({
          path: relativePath,
          type: 'directory',
        })
      }
      for (const entryName of fs.readdirSync(currentPath).sort()) {
        visit(path.join(currentPath, entryName))
      }
      return
    }
    if (!currentStat.isFile()) {
      throw new Error(
        `Runtime recovery inventory requires regular files: ${currentPath}`
      )
    }
    entries.push({
      path: relativePath,
      type: 'file',
      size: Number(currentStat.size),
      sha256: sha256RegularFile(currentPath),
    })
  }

  visit(rootPath)
  const canonicalEntries = JSON.stringify(entries)
  return {
    entries,
    tree_digest: crypto
      .createHash('sha256')
      .update(canonicalEntries)
      .digest('hex'),
  }
}

function persistRuntimeRecoveryDocument(recoveryRoot, fileName, payload) {
  if (!/^[0-9A-Za-z_.-]+\.json$/.test(fileName)) {
    throw new Error(`Unsafe runtime recovery document name: ${fileName}`)
  }
  fs.mkdirSync(recoveryRoot, { recursive: true, mode: 0o700 })
  hardenPrivatePath(recoveryRoot, { directory: true })
  const document = {
    ...payload,
    manifest_digest: crypto
      .createHash('sha256')
      .update(JSON.stringify(payload))
      .digest('hex'),
  }
  const documentPath = path.join(recoveryRoot, fileName)
  const temporaryPath = path.join(
    recoveryRoot,
    `.${fileName}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`
  )
  let descriptor = null
  try {
    descriptor = fs.openSync(
      temporaryPath,
      fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY,
      0o600
    )
    fs.writeFileSync(
      descriptor,
      `${JSON.stringify(document, null, 2)}\n`,
      'utf-8'
    )
    fs.fsyncSync(descriptor)
    fs.closeSync(descriptor)
    descriptor = null
    fs.renameSync(temporaryPath, documentPath)
    hardenPrivatePath(documentPath)
  } finally {
    if (descriptor !== null) fs.closeSync(descriptor)
  }
  const persisted = readJson(documentPath)
  if (
    String(persisted.manifest_digest || '') !== document.manifest_digest
  ) {
    throw new Error(
      `Runtime recovery document verification failed: ${documentPath}`
    )
  }
  return {
    document,
    documentPath,
  }
}

function processIsAlive(processId) {
  if (!Number.isInteger(processId) || processId <= 0) return false
  try {
    process.kill(processId, 0)
    return true
  } catch (error) {
    return error && error.code === 'EPERM'
  }
}

function acquireRuntimePackageLock(projectRoot, operationId) {
  const lockPath = path.join(
    projectRoot,
    `.package-install-${toolId.replace(/[^0-9A-Za-z_.-]+/g, '_')}.lock`
  )
  const token = crypto.randomBytes(32).toString('hex')
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      fs.mkdirSync(lockPath, { mode: 0o700 })
      hardenPrivatePath(lockPath, { directory: true })
      const ownerPath = path.join(lockPath, 'owner.json')
      const descriptor = fs.openSync(
        ownerPath,
        fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY,
        0o600
      )
      try {
        fs.writeFileSync(
          descriptor,
          `${JSON.stringify(
            {
              format_version: 1,
              tool_id: toolId,
              project_root: projectRoot,
              operation_id: operationId,
              pid: process.pid,
              token,
              acquired_at: new Date().toISOString(),
            },
            null,
            2
          )}\n`,
          'utf-8'
        )
        fs.fsyncSync(descriptor)
      } finally {
        fs.closeSync(descriptor)
      }
      hardenPrivatePath(ownerPath)
      return { lockPath, ownerPath, token }
    } catch (error) {
      if (!error || error.code !== 'EEXIST' || attempt > 0) throw error
      const lockStat = fs.lstatSync(lockPath)
      const canonicalLockPath = fs.realpathSync.native(lockPath)
      if (
        lockStat.isSymbolicLink() ||
        !lockStat.isDirectory() ||
        !isPathInside(projectRoot, canonicalLockPath)
      ) {
        throw new Error(
          `Runtime package lock is unsafe and cannot be recovered: ${lockPath}`
        )
      }
      const ownerPath = path.join(lockPath, 'owner.json')
      const lockAgeMilliseconds = Math.max(
        0,
        Date.now() - Number(lockStat.mtimeMs || 0)
      )
      if (!fs.existsSync(ownerPath)) {
        if (lockAgeMilliseconds < 30000) {
          throw new Error(
            `Another runtime package update is initializing for ${toolId}`
          )
        }
      } else {
        const ownerStat = fs.lstatSync(ownerPath)
        if (ownerStat.isSymbolicLink() || !ownerStat.isFile()) {
          throw new Error(
            `Runtime package lock owner is unsafe: ${ownerPath}`
          )
        }
      }
      const owner = fs.existsSync(ownerPath) ? readJson(ownerPath) : {}
      const ownerMatches =
        String(owner.tool_id || '') === toolId &&
        String(owner.project_root || '') === projectRoot &&
        /^[0-9a-f]{64}$/.test(String(owner.token || ''))
      if (!ownerMatches && lockAgeMilliseconds < 30000) {
        throw new Error(
          `Another runtime package update has an incomplete active lock for ${toolId}`
        )
      }
      if (ownerMatches && processIsAlive(Number(owner.pid || 0))) {
        throw new Error(
          `Another runtime package update is active for ${toolId}`
        )
      }
      const stalePath = path.join(
        projectRoot,
        `.package-lock-recovery-${operationId}`
      )
      fs.renameSync(lockPath, stalePath)
      writeRuntimeLog('backend.packageLockRecovered', {
        stalePath,
        previousOwner: ownerMatches ? owner : {},
      })
    }
  }
  throw new Error(`Could not acquire runtime package lock for ${toolId}`)
}

function releaseRuntimePackageLock(packageLock) {
  if (!packageLock || typeof packageLock !== 'object') return
  const { lockPath, ownerPath, token } = packageLock
  try {
    const lockStat = fs.lstatSync(lockPath)
    const ownerStat = fs.lstatSync(ownerPath)
    if (
      lockStat.isSymbolicLink() ||
      !lockStat.isDirectory() ||
      ownerStat.isSymbolicLink() ||
      !ownerStat.isFile()
    ) {
      throw new Error('Runtime package lock identity changed before release')
    }
    const owner = readJson(ownerPath)
    if (
      String(owner.tool_id || '') !== toolId ||
      String(owner.token || '') !== token ||
      Number(owner.pid || 0) !== process.pid
    ) {
      throw new Error('Runtime package lock ownership changed before release')
    }
    fs.unlinkSync(ownerPath)
    fs.rmdirSync(lockPath)
  } catch (error) {
    writeRuntimeLog('backend.packageLockReleaseFailed', {
      lockPath,
      message: String(error?.message || error),
    })
  }
}

function runtimeRecoveryDocumentValid(document) {
  if (!document || typeof document !== 'object') return false
  const expectedDigest = String(document.manifest_digest || '')
  if (!/^[0-9a-f]{64}$/.test(expectedDigest)) return false
  const payload = { ...document }
  delete payload.manifest_digest
  const actualDigest = crypto
    .createHash('sha256')
    .update(JSON.stringify(payload))
    .digest('hex')
  return actualDigest === expectedDigest
}

function reconcileInterruptedRuntimeRecoveries(projectRoot) {
  for (const entryName of fs.readdirSync(projectRoot).sort()) {
    if (!entryName.startsWith('.package-recovery-')) continue
    const recoveryRoot = path.join(projectRoot, entryName)
    const recoveryStat = fs.lstatSync(recoveryRoot)
    const canonicalRecoveryRoot = fs.realpathSync.native(recoveryRoot)
    if (
      recoveryStat.isSymbolicLink() ||
      !recoveryStat.isDirectory() ||
      !isPathInside(projectRoot, canonicalRecoveryRoot)
    ) {
      throw new Error(`Runtime recovery root is unsafe: ${recoveryRoot}`)
    }
    const manifestPath = path.join(recoveryRoot, 'recovery-manifest.json')
    if (!fs.existsSync(manifestPath)) continue
    const terminalNames = [
      'recovery-retained.json',
      'package-update-aborted.json',
      'package-update-reconciled.json',
    ]
    if (
      terminalNames.some((fileName) => {
        const documentPath = path.join(recoveryRoot, fileName)
        if (!fs.existsSync(documentPath)) return false
        const documentStat = fs.lstatSync(documentPath)
        return (
          !documentStat.isSymbolicLink() &&
          documentStat.isFile() &&
          runtimeRecoveryDocumentValid(readJson(documentPath))
        )
      })
    ) {
      continue
    }
    const manifestStat = fs.lstatSync(manifestPath)
    const manifest = readJson(manifestPath)
    if (
      manifestStat.isSymbolicLink() ||
      !manifestStat.isFile() ||
      !runtimeRecoveryDocumentValid(manifest) ||
      String(manifest.tool_id || '') !== toolId ||
      String(manifest.project_root || '') !== projectRoot ||
      !Array.isArray(manifest.trees)
    ) {
      throw new Error(
        `Interrupted runtime recovery manifest is invalid: ${manifestPath}`
      )
    }
    const locations = []
    for (const snapshot of manifest.trees) {
      const treeName = String(snapshot.tree_name || '')
      if (!['src-core', 'platform_tools'].includes(treeName)) {
        throw new Error(
          `Interrupted runtime recovery has an unsafe tree: ${treeName}`
        )
      }
      const expectedOriginal = path.join(projectRoot, treeName)
      if (
        path.resolve(String(snapshot.original_path || '')) !==
          path.resolve(expectedOriginal) ||
        String(snapshot.recovery_relative_path || '') !== treeName ||
        !Array.isArray(snapshot.entries) ||
        !/^[0-9a-f]{64}$/.test(String(snapshot.tree_digest || ''))
      ) {
        throw new Error(
          `Interrupted runtime recovery tree metadata is invalid: ${treeName}`
        )
      }
      const candidates = [
        path.join(recoveryRoot, treeName),
        expectedOriginal,
      ]
      let verifiedLocation = ''
      for (const candidate of candidates) {
        if (!fs.existsSync(candidate)) continue
        try {
          const inventory = inventoryOwnedRuntimeTree(candidate)
          if (
            inventory.tree_digest === snapshot.tree_digest &&
            JSON.stringify(inventory.entries) ===
              JSON.stringify(snapshot.entries)
          ) {
            verifiedLocation = candidate
            break
          }
        } catch {}
      }
      if (!verifiedLocation) {
        throw new Error(
          `Interrupted runtime update requires manual recovery at ${recoveryRoot}`
        )
      }
      locations.push({
        tree_name: treeName,
        verified_location: verifiedLocation,
        tree_digest: snapshot.tree_digest,
      })
    }
    persistRuntimeRecoveryDocument(
      recoveryRoot,
      'package-update-reconciled.json',
      {
        format_version: 1,
        status: 'reconciled',
        tool_id: toolId,
        reconciled_at: new Date().toISOString(),
        recovery_root: recoveryRoot,
        trees: locations,
      }
    )
    writeRuntimeLog('backend.packageCrashReconciled', {
      recoveryRoot,
      locations,
    })
  }
}

function preparePackagedBackendRuntime(projectRoot, verification) {
  if (!app.isPackaged) return
  fs.mkdirSync(projectRoot, { recursive: true, mode: 0o700 })
  hardenPrivatePath(projectRoot, { directory: true })
  const projectRootStat = fs.lstatSync(projectRoot)
  const canonicalProjectRoot = fs.realpathSync.native(projectRoot)
  if (
    projectRootStat.isSymbolicLink() ||
    !projectRootStat.isDirectory() ||
    path.resolve(canonicalProjectRoot) !== path.resolve(projectRoot)
  ) {
    throw new Error(
      `Standalone runtime root must be a canonical private directory: ${projectRoot}`
    )
  }

  const assertOwnedTree = (targetPath, label) => {
    if (!isPathInside(projectRoot, targetPath)) {
      throw new Error(`${label} escaped the standalone runtime root: ${targetPath}`)
    }
    const targetStat = fs.lstatSync(targetPath)
    if (targetStat.isSymbolicLink()) {
      throw new Error(`${label} cannot contain links or junctions: ${targetPath}`)
    }
    const canonicalTarget = fs.realpathSync.native(targetPath)
    if (!isPathInside(canonicalProjectRoot, canonicalTarget)) {
      throw new Error(`${label} escaped the standalone runtime root: ${targetPath}`)
    }
    if (!targetStat.isDirectory()) return
    for (const entryName of fs.readdirSync(targetPath)) {
      assertOwnedTree(path.join(targetPath, entryName), label)
    }
  }

  const markerPath = path.join(projectRoot, '.backend-package.json')
  const installedMarker = readJson(markerPath)
  const backendEntry = path.join(projectRoot, 'src-core', 'main.py')
  const liveToolsRoot = path.join(projectRoot, 'platform_tools')
  const liveCore = path.join(projectRoot, 'src-core')
  const liveToolRoot = path.join(liveToolsRoot, toolId)
  const toolEntry = path.join(
    liveToolsRoot,
    toolId,
    'src',
    'main.py'
  )
  let isolatedToolRuntime = false
  let isolatedBackendRuntime = false
  if (fs.existsSync(liveCore)) {
    try {
      assertOwnedTree(liveCore, 'Installed backend runtime')
      isolatedBackendRuntime = true
    } catch {
      isolatedBackendRuntime = false
    }
  }
  if (fs.existsSync(liveToolsRoot)) {
    try {
      assertOwnedTree(liveToolsRoot, 'Installed tool runtime')
      isolatedToolRuntime = fs
        .readdirSync(liveToolsRoot)
        .every((entryName) => entryName === toolId)
    } catch {
      isolatedToolRuntime = false
    }
  }
  const operationId = `${process.pid}-${crypto.randomBytes(8).toString('hex')}`
  const packageLock = acquireRuntimePackageLock(projectRoot, operationId)
  try {
    reconcileInterruptedRuntimeRecoveries(projectRoot)
  } catch (error) {
    releaseRuntimePackageLock(packageLock)
    throw error
  }
  if (
    (
      preparedBackendPackageDigest === String(verification.packageDigest || '') ||
      (
        String(installedMarker.tool_id || '') === toolId &&
        String(installedMarker.package_digest || '') ===
          String(verification.packageDigest || '') &&
        Number(installedMarker.protocol_version || 0) ===
          Number(verification.protocolVersion || 0) &&
        String(installedMarker.backend_version || '') ===
          String(verification.backendVersion || '')
      )
    ) &&
    fs.existsSync(backendEntry) &&
    fs.existsSync(toolEntry) &&
    isolatedBackendRuntime &&
    isolatedToolRuntime
  ) {
    preparedBackendPackageDigest = String(verification.packageDigest || '')
    releaseRuntimePackageLock(packageLock)
    return
  }
  const stagingRoot = path.join(projectRoot, `.package-staging-${operationId}`)
  const backupRoot = path.join(projectRoot, `.package-recovery-${operationId}`)
  const stagedCore = path.join(stagingRoot, 'src-core')
  const stagedToolsRoot = path.join(stagingRoot, 'platform_tools')
  const stagedToolRoot = path.join(stagedToolsRoot, toolId)
  const stagedToolSource = path.join(stagedToolRoot, 'src')
  const stagedToolManifest = path.join(stagedToolRoot, 'manifest.json')
  const installed = []
  const moved = []
  const recoverySnapshots = []
  let retainBackupRoot = false

  const removeInternalPath = (targetPath) => {
    if (!isPathInside(projectRoot, targetPath) || targetPath === projectRoot) {
      throw new Error(`Refusing to remove unsafe runtime path: ${targetPath}`)
    }
    fs.rmSync(targetPath, { recursive: true, force: true })
  }
  const installItem = (stagedPath, livePath, backupName) => {
    fs.mkdirSync(path.dirname(livePath), { recursive: true })
    if (fs.existsSync(livePath)) {
      const backupPath = path.join(backupRoot, backupName)
      fs.mkdirSync(path.dirname(backupPath), { recursive: true })
      fs.renameSync(livePath, backupPath)
      moved.push({ backupPath, livePath })
    }
    fs.renameSync(stagedPath, livePath)
    installed.push(livePath)
  }

  try {
    fs.mkdirSync(stagingRoot)
    fs.cpSync(path.join(appRoot, 'src-core'), stagedCore, {
      recursive: true,
      errorOnExist: true,
    })
    fs.cpSync(
      path.join(appRoot, 'platform_tools', toolId, 'src'),
      stagedToolSource,
      { recursive: true, errorOnExist: true }
    )
    fs.copyFileSync(
      path.join(appRoot, 'platform_tools', toolId, 'manifest.json'),
      stagedToolManifest
    )
    assertOwnedTree(stagedCore, 'Packaged backend source')
    assertOwnedTree(stagedToolsRoot, 'Packaged tool source')

    const legacyCoreBackups = path.join(liveCore, 'backups')
    if (fs.existsSync(legacyCoreBackups)) {
      assertOwnedTree(legacyCoreBackups, 'Legacy backup state')
      fs.cpSync(legacyCoreBackups, path.join(stagedCore, 'backups'), {
        recursive: true,
      })
    }
    const mutableToolRuntime = path.join(liveToolRoot, 'runtime')
    if (fs.existsSync(mutableToolRuntime)) {
      assertOwnedTree(mutableToolRuntime, 'Mutable tool state')
      fs.cpSync(mutableToolRuntime, path.join(stagedToolRoot, 'runtime'), {
        recursive: true,
      })
    }
    if (toolId === 'file-sorter') {
      const legacyRuleNames = [
        'keyword_rules.json',
        'keyword_rules.py',
        '.file-sorter-rules.json',
      ]
      const migrationInbox = path.join(
        stagedToolRoot,
        'runtime',
        'legacy-rule-migration-inbox'
      )
      for (const ruleName of legacyRuleNames) {
        const legacyRulePath = path.join(liveToolRoot, 'src', ruleName)
        if (!fs.existsSync(legacyRulePath)) continue
        assertOwnedTree(legacyRulePath, 'Legacy file-sorter rules')
        const legacyStat = fs.lstatSync(legacyRulePath)
        if (!legacyStat.isFile() || legacyStat.size > 8 * 1024 * 1024) {
          throw new Error(
            `Legacy file-sorter rules are not a bounded regular file: ${legacyRulePath}`
          )
        }
        const payload = fs.readFileSync(legacyRulePath)
        const digest = crypto.createHash('sha256').update(payload).digest('hex')
        const safeRuleName = ruleName.replace(/[^0-9A-Za-z_.-]+/g, '_')
        const inboxPath = path.join(
          migrationInbox,
          `${digest}-${safeRuleName}`
        )
        fs.mkdirSync(migrationInbox, { recursive: true, mode: 0o700 })
        if (!fs.existsSync(inboxPath)) {
          fs.copyFileSync(
            legacyRulePath,
            inboxPath,
            fs.constants.COPYFILE_EXCL
          )
          hardenPrivatePath(inboxPath)
        } else {
          assertOwnedTree(inboxPath, 'Legacy rule migration inbox')
          const existingDigest = crypto
            .createHash('sha256')
            .update(fs.readFileSync(inboxPath))
            .digest('hex')
          if (existingDigest !== digest) {
            throw new Error(
              `Legacy rule migration inbox digest mismatch: ${inboxPath}`
            )
          }
        }
      }
    }

    if (fs.existsSync(liveCore)) {
      assertOwnedTree(liveCore, 'Installed backend runtime')
    }
    if (fs.existsSync(liveToolsRoot)) {
      assertOwnedTree(liveToolsRoot, 'Installed tool runtime')
    }
    assertOwnedTree(stagedToolsRoot, 'Staged tool runtime')

    for (const [treeName, treePath] of [
      ['src-core', liveCore],
      ['platform_tools', liveToolsRoot],
    ]) {
      if (!fs.existsSync(treePath)) continue
      const inventory = inventoryOwnedRuntimeTree(treePath)
      recoverySnapshots.push({
        tree_name: treeName,
        original_path: treePath,
        recovery_relative_path: treeName,
        entries: inventory.entries,
        tree_digest: inventory.tree_digest,
      })
    }
    if (recoverySnapshots.length > 0) {
      retainBackupRoot = true
      const recoveryManifestPublication = persistRuntimeRecoveryDocument(
        backupRoot,
        'recovery-manifest.json',
        {
          format_version: 1,
          status: 'prepared',
          reason: 'standalone-runtime-upgrade-preservation',
          tool_id: toolId,
          package_digest: String(verification.packageDigest || ''),
          created_at: new Date().toISOString(),
          project_root: projectRoot,
          recovery_root: backupRoot,
          trees: recoverySnapshots,
        }
      )
      persistRuntimeRecoveryDocument(
        backupRoot,
        'package-update-journal.json',
        {
          format_version: 1,
          status: 'prepared',
          tool_id: toolId,
          operation_id: operationId,
          package_digest: String(verification.packageDigest || ''),
          prepared_at: new Date().toISOString(),
          recovery_root: backupRoot,
          recovery_manifest_digest:
            recoveryManifestPublication.document.manifest_digest,
        }
      )
    }

    installItem(stagedCore, liveCore, 'src-core')
    installItem(stagedToolsRoot, liveToolsRoot, 'platform_tools')

    if (recoverySnapshots.length > 0) {
      const verifiedTrees = recoverySnapshots.map((snapshot) => {
        const recoveredPath = path.join(
          backupRoot,
          snapshot.recovery_relative_path
        )
        const recovered = inventoryOwnedRuntimeTree(recoveredPath)
        if (
          recovered.tree_digest !== snapshot.tree_digest ||
          JSON.stringify(recovered.entries) !== JSON.stringify(snapshot.entries)
        ) {
          throw new Error(
            `Retained runtime recovery verification failed: ${recoveredPath}`
          )
        }
        return {
          tree_name: snapshot.tree_name,
          recovery_relative_path: snapshot.recovery_relative_path,
          tree_digest: recovered.tree_digest,
        }
      })
      const preparedManifest = readJson(
        path.join(backupRoot, 'recovery-manifest.json')
      )
      persistRuntimeRecoveryDocument(
        backupRoot,
        'recovery-retained.json',
        {
          format_version: 1,
          status: 'retained',
          tool_id: toolId,
          package_digest: String(verification.packageDigest || ''),
          retained_at: new Date().toISOString(),
          recovery_root: backupRoot,
          prepared_manifest_digest: String(
            preparedManifest.manifest_digest || ''
          ),
          trees: verifiedTrees,
        }
      )
      persistRuntimeRecoveryDocument(
        backupRoot,
        'package-update-complete.json',
        {
          format_version: 1,
          status: 'complete',
          tool_id: toolId,
          operation_id: operationId,
          package_digest: String(verification.packageDigest || ''),
          completed_at: new Date().toISOString(),
          recovery_root: backupRoot,
          trees: verifiedTrees,
        }
      )
      writeRuntimeLog('backend.packageRecoveryRetained', {
        recoveryRoot: backupRoot,
        treeCount: verifiedTrees.length,
        treeDigests: verifiedTrees.map((tree) => tree.tree_digest),
      })
    }

    const markerTemporaryPath = `${markerPath}.${operationId}.tmp`
    fs.writeFileSync(
      markerTemporaryPath,
      `${JSON.stringify(
        {
          tool_id: toolId,
          package_digest: String(verification.packageDigest || ''),
          protocol_version: Number(verification.protocolVersion || 0),
          backend_version: String(verification.backendVersion || ''),
          installed_at: new Date().toISOString(),
        },
        null,
        2
      )}\n`,
      { encoding: 'utf-8', mode: 0o600 }
    )
    if (fs.existsSync(markerPath)) {
      const previousMarker = inventoryOwnedRuntimeTree(markerPath)
      retainBackupRoot = true
      persistRuntimeRecoveryDocument(
        backupRoot,
        'previous-backend-package-marker.json',
        {
          format_version: 1,
          status: 'retained',
          reason: 'standalone-runtime-marker-replacement',
          tool_id: toolId,
          operation_id: operationId,
          retained_at: new Date().toISOString(),
          original_path: markerPath,
          recovery_relative_path: 'backend-package-marker.json',
          tree_digest: previousMarker.tree_digest,
          entries: previousMarker.entries,
        }
      )
    }
    installItem(
      markerTemporaryPath,
      markerPath,
      'backend-package-marker.json'
    )
    hardenPrivatePath(markerPath)
    preparedBackendPackageDigest = String(verification.packageDigest || '')
  } catch (error) {
    const rollbackErrors = []
    for (const livePath of installed.reverse()) {
      try {
        removeInternalPath(livePath)
      } catch (rollbackError) {
        rollbackErrors.push(
          `remove ${livePath}: ${String(rollbackError?.message || rollbackError)}`
        )
      }
    }
    for (const item of moved.reverse()) {
      try {
        fs.mkdirSync(path.dirname(item.livePath), { recursive: true })
        fs.renameSync(item.backupPath, item.livePath)
      } catch (rollbackError) {
        rollbackErrors.push(
          `restore ${item.livePath}: ${String(
            rollbackError?.message || rollbackError
          )}`
        )
      }
    }
    if (recoverySnapshots.length > 0 && rollbackErrors.length === 0) {
      try {
        persistRuntimeRecoveryDocument(
          backupRoot,
          'package-update-aborted.json',
          {
            format_version: 1,
            status: 'aborted-and-rolled-back',
            tool_id: toolId,
            operation_id: operationId,
            package_digest: String(verification.packageDigest || ''),
            aborted_at: new Date().toISOString(),
            recovery_root: backupRoot,
            error: String(error?.message || error),
          }
        )
      } catch (journalError) {
        writeRuntimeLog('backend.packageAbortJournalFailed', {
          backupRoot,
          message: String(journalError?.message || journalError),
        })
      }
    }
    if (rollbackErrors.length > 0) {
      retainBackupRoot = true
      writeRuntimeLog('backend.packageRollbackIncomplete', {
        backupRoot,
        originalError: String(error?.message || error),
        rollbackErrors,
      })
      throw new Error(
        `Standalone runtime update failed and rollback was incomplete. ` +
          `Original data was retained at ${backupRoot}. ` +
          `${rollbackErrors.join('; ')}`,
        { cause: error }
      )
    }
    throw error
  } finally {
    try {
      if (fs.existsSync(stagingRoot)) removeInternalPath(stagingRoot)
      if (!retainBackupRoot && fs.existsSync(backupRoot)) {
        removeInternalPath(backupRoot)
      }
    } finally {
      releaseRuntimePackageLock(packageLock)
    }
  }
  writeRuntimeLog('backend.packageInstalled', {
    projectRoot,
    packageDigest: verification.packageDigest,
    recoveryRoot: retainBackupRoot ? backupRoot : '',
  })
}

function legacyIpcStateRoot() {
  if (process.platform === 'win32') {
    const localAppData = String(process.env.LOCALAPPDATA || '').trim()
    const base = localAppData || path.join(os.homedir(), 'AppData', 'Local')
    return path.resolve(base, 'GPTBridge', 'ipc')
  }
  const xdgStateHome = String(process.env.XDG_STATE_HOME || '').trim()
  const base = xdgStateHome || path.join(os.homedir(), '.local', 'state')
  return path.resolve(base, 'GPTBridge', 'ipc')
}

function ipcStateRoot() {
  if (governedChannelRuntimeRequested()) {
    const toolDataRoot = String(
      process.env.GPTBRIDGE_TOOL_DATA_ROOT || ''
    ).trim()
    if (toolDataRoot) return path.resolve(toolDataRoot, 'ipc')
  }
  if (app.isPackaged && !managedBackendReuseRequested()) {
    return path.join(inferProjectRoot(), 'runtime', 'ipc')
  }
  const configured = String(process.env.GPTBRIDGE_IPC_STATE_ROOT || '').trim()
  if (configured) return path.resolve(configured)
  return legacyIpcStateRoot()
}

function sleepSync(milliseconds) {
  Atomics.wait(
    new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT)),
    0,
    0,
    milliseconds
  )
}

function hardenPrivatePath(targetPath, { directory = false } = {}) {
  try {
    fs.chmodSync(targetPath, directory ? 0o700 : 0o600)
  } catch {}
  if (process.platform !== 'win32') return
  const username = String(process.env.USERNAME || '').trim()
  if (!username) return
  const userPermission = directory ? '(OI)(CI)(F)' : '(R,W)'
  const systemPermission = directory ? '(OI)(CI)(F)' : '(F)'
  try {
    childProcess.spawnSync(
      'icacls.exe',
      [
        targetPath,
        '/inheritance:r',
        '/grant:r',
        `${username}:${userPermission}`,
        '/grant:r',
        `*S-1-5-18:${systemPermission}`,
      ],
      { windowsHide: true, stdio: 'ignore' }
    )
  } catch {}
}

function normalizePackageRelativePath(relativePath) {
  return String(relativePath || '').replace(/\\/g, '/').replace(/^\.\/+/, '')
}

function packagePathIsMutable(relativePath) {
  const normalized = normalizePackageRelativePath(relativePath)
  const mutablePrefixes = [
    '.GPTBridge_RuntimeSandbox/',
    'backups/',
    'config/',
    'runtime/',
    'logs/',
    'src-core/backups/',
    `platform_tools/${toolId}/runtime/`,
  ]
  return mutablePrefixes.some((prefix) => normalized.startsWith(prefix))
}

function pruneGeneratedPythonBytecode(expectedPaths) {
  const pythonRoot = path.join(appRoot, 'python')
  if (!fs.existsSync(pythonRoot)) return []

  let canonicalPythonRoot
  try {
    canonicalPythonRoot = fs.realpathSync(pythonRoot)
    if (!isPathInside(appRoot, canonicalPythonRoot)) return []
  } catch {
    return []
  }

  const removed = []
  const visit = (directory) => {
    let canonicalDirectory
    try {
      canonicalDirectory = fs.realpathSync(directory)
    } catch {
      return
    }
    if (!isPathInside(canonicalPythonRoot, canonicalDirectory)) return

    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) continue
      const absolutePath = path.join(directory, entry.name)
      if (!entry.isDirectory()) continue
      if (entry.name !== '__pycache__') {
        visit(absolutePath)
        continue
      }

      let canonicalCache
      let cacheEntries
      try {
        canonicalCache = fs.realpathSync(absolutePath)
        cacheEntries = fs.readdirSync(absolutePath, { withFileTypes: true })
      } catch {
        continue
      }
      if (!isPathInside(canonicalPythonRoot, canonicalCache)) continue
      const removable = cacheEntries.every((cacheEntry) => {
        if (
          cacheEntry.isSymbolicLink() ||
          !cacheEntry.isFile() ||
          !cacheEntry.name.endsWith('.pyc')
        ) {
          return false
        }
        const relativePath = normalizePackageRelativePath(
          path.relative(appRoot, path.join(absolutePath, cacheEntry.name))
        )
        return !expectedPaths.has(relativePath)
      })
      if (!removable) continue
      try {
        fs.rmSync(absolutePath, { recursive: true, force: false })
        removed.push(
          normalizePackageRelativePath(path.relative(appRoot, absolutePath))
        )
      } catch (error) {
        writeRuntimeLog('package.bytecodeRepairFailed', {
          path: normalizePackageRelativePath(
            path.relative(appRoot, absolutePath)
          ),
          message: String(error?.message || error),
        })
      }
    }
  }

  visit(pythonRoot)
  if (removed.length > 0) {
    writeRuntimeLog('package.bytecodeRepaired', {
      removedCount: removed.length,
      paths: removed.slice(0, 20),
    })
  }
  return removed
}

function sha256File(filePath) {
  const digest = crypto.createHash('sha256')
  const descriptor = fs.openSync(filePath, 'r')
  const buffer = Buffer.allocUnsafe(1024 * 1024)
  try {
    let bytesRead = 0
    do {
      bytesRead = fs.readSync(descriptor, buffer, 0, buffer.length, null)
      if (bytesRead > 0) digest.update(buffer.subarray(0, bytesRead))
    } while (bytesRead > 0)
  } finally {
    fs.closeSync(descriptor)
  }
  return digest.digest('hex')
}

function payloadSnapshotDigest(files) {
  const digest = crypto.createHash('sha256')
  for (const relativePath of Object.keys(files).sort()) {
    digest.update(relativePath, 'utf-8')
    digest.update('\0')
    digest.update(String(files[relativePath]), 'ascii')
    digest.update('\n')
  }
  return digest.digest('hex')
}

function collectUnexpectedPayloadFiles(expectedPaths) {
  const unexpected = []
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolutePath = path.join(directory, entry.name)
      const relativePath = normalizePackageRelativePath(
        path.relative(appRoot, absolutePath)
      )
      if (relativePath === '.gptbridge-package.json') continue
      if (entry.isSymbolicLink()) {
        unexpected.push(`${relativePath} (symbolic link)`)
        continue
      }
      if (entry.isDirectory()) {
        if (packagePathIsMutable(`${relativePath}/`)) continue
        visit(absolutePath)
        continue
      }
      if (
        entry.isFile() &&
        !expectedPaths.has(relativePath) &&
        !packagePathIsMutable(relativePath)
      ) {
        unexpected.push(relativePath)
      }
    }
  }
  visit(appRoot)
  return unexpected
}

function verifyPackagedPayload() {
  if (packageVerification) return packageVerification

  const fail = (message) => {
    packageVerification = { ok: false, message }
    writeRuntimeLog('package.verificationFailed', { message })
    return packageVerification
  }
  if (!packageMetadata || typeof packageMetadata !== 'object') {
    return fail('Package metadata is unavailable')
  }
  if (String(packageMetadata.tool_id || '') !== toolId) {
    return fail('Package tool identity does not match the application manifest')
  }
  if (Number(packageMetadata.protocol_version || 0) !== 1) {
    return fail('Unsupported standalone backend protocol version')
  }
  const standalone =
    manifest.standalone && typeof manifest.standalone === 'object'
      ? manifest.standalone
      : {}
  if (
    !validBackendPort(packageMetadata.backend_port) ||
    Number(packageMetadata.backend_port) !== Number(standalone.backend_port)
  ) {
    return fail('Package backend port identity does not match the application manifest')
  }
  if (
    packageMetadata.isolated_backend !== true ||
    standalone.isolated_backend !== true
  ) {
    return fail('Package must use an isolated standalone backend')
  }
  if (
    String(packageMetadata.tool_version || '') !==
    String(manifest.version || '')
  ) {
    return fail('Package and application versions do not match')
  }
  const expectedFiles = packageMetadata.payload_files
  if (!expectedFiles || typeof expectedFiles !== 'object') {
    return fail('Package payload fingerprint is missing')
  }

  const actualFiles = {}
  for (const [rawRelativePath, expectedDigest] of Object.entries(expectedFiles)) {
    const relativePath = normalizePackageRelativePath(rawRelativePath)
    const absolutePath = path.resolve(appRoot, relativePath)
    if (
      !relativePath ||
      !isPathInside(appRoot, absolutePath) ||
      typeof expectedDigest !== 'string'
    ) {
      return fail(`Unsafe package payload path: ${relativePath || '<empty>'}`)
    }
    let stat
    try {
      stat = fs.lstatSync(absolutePath)
    } catch {
      return fail(`Packaged file is missing: ${relativePath}`)
    }
    if (!stat.isFile() || stat.isSymbolicLink()) {
      return fail(`Packaged path is not a regular file: ${relativePath}`)
    }
    const actualDigest = sha256File(absolutePath)
    if (actualDigest !== expectedDigest) {
      return fail(`Packaged file fingerprint mismatch: ${relativePath}`)
    }
    actualFiles[relativePath] = actualDigest
  }
  const actualPayloadDigest = payloadSnapshotDigest(actualFiles)
  if (actualPayloadDigest !== String(packageMetadata.payload_digest || '')) {
    return fail('Package payload digest does not match its manifest')
  }
  const expectedPaths = new Set(Object.keys(actualFiles))
  pruneGeneratedPythonBytecode(expectedPaths)
  const unexpectedFiles = collectUnexpectedPayloadFiles(expectedPaths)
  if (unexpectedFiles.length > 0) {
    return fail(
      `Unexpected executable package content: ${unexpectedFiles
        .slice(0, 3)
        .join(', ')}`
    )
  }

  packageVerification = {
    ok: true,
    packageDigest: actualPayloadDigest,
    protocolVersion: Number(packageMetadata.protocol_version || 0),
    backendVersion: String(packageMetadata.backend_service_version || ''),
  }
  return packageVerification
}

function backendOwnerPath() {
  const safeToolId = toolId.replace(/[^0-9A-Za-z_.-]+/g, '_') || 'tool'
  return path.join(ipcStateRoot(), `standalone-${safeToolId}-backend.json`)
}

function legacyBackendOwnerPath() {
  const safeToolId = toolId.replace(/[^0-9A-Za-z_.-]+/g, '_') || 'tool'
  return path.join(
    legacyIpcStateRoot(),
    `standalone-${safeToolId}-backend.json`
  )
}

function readBackendOwner() {
  return readJson(backendOwnerPath())
}

function backendOwnerMatches(owner, projectRoot, verification) {
  return (
    owner &&
    typeof owner === 'object' &&
    String(owner.project_root || '') === path.resolve(projectRoot) &&
    String(owner.workspace_instance_id || '') === workspaceInstanceId(projectRoot) &&
    String(owner.package_digest || '') === String(verification.packageDigest || '') &&
    Number(owner.protocol_version || 0) === Number(verification.protocolVersion || 0) &&
    String(owner.backend_version || '') === String(verification.backendVersion || '') &&
    Number(owner.backend_port || 8765) === backendPort
  )
}

function persistBackendOwner(owner) {
  const targetPath = backendOwnerPath()
  fs.mkdirSync(path.dirname(targetPath), { recursive: true, mode: 0o700 })
  hardenPrivatePath(path.dirname(targetPath), { directory: true })
  const temporaryPath = `${targetPath}.${process.pid}.${crypto
    .randomBytes(6)
    .toString('hex')}.tmp`
  fs.writeFileSync(temporaryPath, `${JSON.stringify(owner, null, 2)}\n`, {
    encoding: 'utf-8',
    mode: 0o600,
  })
  fs.renameSync(temporaryPath, targetPath)
  hardenPrivatePath(targetPath)
}

function backendSessionToken() {
  const tokenDir = ipcStateRoot()
  const tokenPath = path.join(tokenDir, 'session-token')
  const validToken = (value) => /^[a-f0-9]{64}$/.test(String(value || '').trim())
  const configured = String(
    process.env.GPTBRIDGE_IPC_SESSION_TOKEN || ''
  ).trim().toLowerCase()
  if (validToken(configured)) return configured
  const readToken = () => {
    try {
      const token = fs.readFileSync(tokenPath, 'utf-8').trim().toLowerCase()
      return validToken(token) ? token : ''
    } catch {
      return ''
    }
  }

  const existing = readToken()
  if (existing) {
    hardenPrivatePath(tokenDir, { directory: true })
    hardenPrivatePath(tokenPath)
    return existing
  }
  fs.mkdirSync(tokenDir, { recursive: true, mode: 0o700 })
  hardenPrivatePath(tokenDir, { directory: true })
  const lockPath = path.join(tokenDir, '.session-token.lock')
  const deadline = Date.now() + 10000
  let ownsLock = false
  let ownerNonce = ''

  while (!ownsLock) {
    const racedToken = readToken()
    if (racedToken) return racedToken
    let createdLock = false
    try {
      fs.mkdirSync(lockPath, { mode: 0o700 })
      createdLock = true
    } catch (error) {
      if (error.code !== 'EEXIST') throw error
    }
    if (createdLock) {
      try {
        ownerNonce = crypto.randomBytes(16).toString('hex')
        fs.writeFileSync(path.join(lockPath, 'owner'), `${ownerNonce}\n`, {
          encoding: 'ascii',
          flag: 'wx',
          mode: 0o600,
        })
        ownsLock = true
        break
      } catch (error) {
        try {
          fs.unlinkSync(path.join(lockPath, 'owner'))
        } catch {}
        try {
          fs.rmdirSync(lockPath)
        } catch {}
        throw error
      }
    }
    try {
      if (Date.now() - fs.lstatSync(lockPath).mtimeMs >= 5000) {
        const stalePath = `${lockPath}.stale-${process.pid}-${crypto
          .randomBytes(6)
          .toString('hex')}`
        fs.renameSync(lockPath, stalePath)
        try {
          fs.rmdirSync(stalePath)
        } catch {}
      }
    } catch {}
    if (Date.now() >= deadline) {
      const finalToken = readToken()
      if (finalToken) return finalToken
      throw new Error(`Timed out acquiring IPC token lock: ${lockPath}`)
    }
    sleepSync(25)
  }

  const stillOwnsLock = () => {
    if (!ownsLock || !ownerNonce) return false
    try {
      return (
        fs.readFileSync(path.join(lockPath, 'owner'), 'ascii').trim() ===
        ownerNonce
      )
    } catch {
      return false
    }
  }

  let temporaryPath = ''
  try {
    const racedToken = readToken()
    if (racedToken) return racedToken
    if (!stillOwnsLock()) throw new Error('Lost IPC token repair lock')
    fs.utimesSync(lockPath, new Date(), new Date())

    try {
      fs.lstatSync(tokenPath)
      const quarantinePath = path.join(
        tokenDir,
        `session-token.invalid-${Date.now()}-${process.pid}-${crypto
          .randomBytes(6)
          .toString('hex')}`
      )
      fs.renameSync(tokenPath, quarantinePath)
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }

    const generated = crypto.randomBytes(32).toString('hex')
    temporaryPath = path.join(
      tokenDir,
      `.session-token.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`
    )
    const descriptor = fs.openSync(temporaryPath, 'wx', 0o600)
    try {
      fs.writeFileSync(descriptor, `${generated}\n`, 'utf-8')
      fs.fsyncSync(descriptor)
    } finally {
      fs.closeSync(descriptor)
    }
    fs.renameSync(temporaryPath, tokenPath)
    temporaryPath = ''
    hardenPrivatePath(tokenPath)
    const persisted = readToken()
    if (!persisted) throw new Error('IPC session token write verification failed')
    return persisted
  } finally {
    if (temporaryPath) {
      try {
        fs.unlinkSync(temporaryPath)
      } catch {}
    }
    if (stillOwnsLock()) {
      try {
        fs.unlinkSync(path.join(lockPath, 'owner'))
        fs.rmdirSync(lockPath)
      } catch {}
    }
  }
}

function backendSessionDescriptor() {
  const token = backendSessionToken()
  const projectRoot = inferProjectRoot()
  const instanceId = workspaceInstanceId(projectRoot)
  const verification = verifyPackagedPayload()
  return {
    token,
    workspaceInstanceId: instanceId,
    packageDigest: String(verification.packageDigest || ''),
    protocolVersion: Number(verification.protocolVersion || 0),
    backendVersion: String(verification.backendVersion || ''),
    startupError: backendLastFailure,
    backendPort,
    websocketUrl:
      `ws://127.0.0.1:${backendPort}/?token=${encodeURIComponent(token)}` +
      `&instance=${encodeURIComponent(instanceId)}`,
  }
}

function isPathInside(basePath, targetPath) {
  const relative = path.relative(basePath, targetPath)
  return (
    relative === '' ||
    (relative && !relative.startsWith('..') && !path.isAbsolute(relative))
  )
}

function backendListening(timeoutMs = 750, port = backendPort) {
  return new Promise((resolve) => {
    const probe = net.createServer()
    let settled = false
    const finish = (listening) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      probe.removeAllListeners()
      if (probe.listening) {
        probe.close(() => resolve(listening))
        return
      }
      resolve(listening)
    }
    const timer = setTimeout(() => finish(true), timeoutMs)
    probe.once('error', () => finish(true))
    probe.once('listening', () => finish(false))
    try {
      probe.listen({
        host: '127.0.0.1',
        port,
        exclusive: true,
      })
    } catch {
      finish(true)
    }
  })
}

function normalizedPathKey(value) {
  const normalized = path.normalize(path.resolve(value))
  return process.platform === 'win32' ? normalized.toLowerCase() : normalized
}

function registerOpenPathCapability(targetPath, allowDescendants) {
  if (!targetPath) return
  let canonical = path.resolve(targetPath)
  try {
    canonical = fs.realpathSync(canonical)
  } catch {
    // Save-dialog targets can be intentionally absent until the renderer
    // publishes the selected file.
  }
  oneTimeOpenPathCapabilities.set(normalizedPathKey(canonical), {
    path: canonical,
    allowDescendants: Boolean(allowDescendants),
  })
}

function consumeOpenPathCapability(targetPath) {
  const targetKey = normalizedPathKey(targetPath)
  for (const [key, capability] of oneTimeOpenPathCapabilities.entries()) {
    const matches =
      targetKey === key ||
      (capability.allowDescendants &&
        isPathInside(capability.path, targetPath))
    if (matches) {
      oneTimeOpenPathCapabilities.delete(key)
      return true
    }
  }
  return false
}

function backendHealth(timeoutMs = 1200, port = backendPort) {
  return new Promise((resolve) => {
    const request = http.get(
      {
        host: '127.0.0.1',
        port,
        path: '/health',
        timeout: timeoutMs,
      },
      (response) => {
        let body = ''
        response.setEncoding('utf8')
        response.on('data', (chunk) => {
          body += chunk
        })
        response.on('end', () => {
          try {
            resolve(JSON.parse(body))
          } catch {
            resolve(null)
          }
        })
      }
    )
    request.on('timeout', () => {
      request.destroy()
      resolve(null)
    })
    request.on('error', () => resolve(null))
  })
}

function requestBackendShutdown(
  shutdownToken,
  timeoutMs = 3000,
  port = backendPort
) {
  return new Promise((resolve) => {
    if (!shutdownToken) {
      resolve(false)
      return
    }
    const request = http.get(
      {
        host: '127.0.0.1',
        port,
        path: '/shutdown',
        timeout: timeoutMs,
        headers: {
          'X-GPTBridge-Shutdown-Token': shutdownToken,
        },
      },
      (response) => {
        response.resume()
        response.on('end', () => resolve(response.statusCode === 200))
      }
    )
    request.on('timeout', () => {
      request.destroy()
      resolve(false)
    })
    request.on('error', () => resolve(false))
  })
}

async function waitForBackendToStop(
  timeoutMs = 10000,
  port = backendPort
) {
  const startedAt = Date.now()
  while (Date.now() - startedAt < timeoutMs) {
    if (!(await backendListening(300, port))) return true
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  return false
}

async function shutdownOwnedBackendBeforeExit() {
  if (managedBackendReuseRequested()) {
    writeRuntimeLog('backend.shutdownDelegatedToMainSystem', { toolId })
    return true
  }
  const projectRoot = inferProjectRoot()
  const verification = packageVerification || verifyPackagedPayload()
  const owner = readBackendOwner()
  if (
    !verification.ok ||
    !backendOwnerMatches(owner, projectRoot, verification) ||
    !String(owner.shutdown_token || '')
  ) {
    writeRuntimeLog('backend.shutdownSkippedUnowned', { toolId })
    return true
  }
  const recordedPort = validBackendPort(owner.backend_port) || backendPort
  const requested = await requestBackendShutdown(
    String(owner.shutdown_token),
    3000,
    recordedPort
  )
  let stopped = requested && (await waitForBackendToStop(5000, recordedPort))
  if (!stopped) {
    stopped = await forceStopVerifiedUnresponsiveBackend(
      owner,
      projectRoot,
      recordedPort
    )
  }
  if (stopped) {
    try {
      fs.unlinkSync(backendOwnerPath())
    } catch (error) {
      if (!error || error.code !== 'ENOENT') throw error
    }
  }
  writeRuntimeLog('backend.shutdownOnWindowClose', {
    toolId,
    requested,
    stopped,
    recordedPort,
  })
  return stopped
}

function windowsProcessOwnsBackend(processId, port, executablePath) {
  if (
    process.platform !== 'win32' ||
    !Number.isInteger(processId) ||
    processId <= 0 ||
    !validBackendPort(port)
  ) {
    return false
  }
  const env = {
    ...process.env,
    GPTBRIDGE_EXPECTED_PROCESS_ID: String(processId),
    GPTBRIDGE_EXPECTED_LISTEN_PORT: String(port),
    GPTBRIDGE_EXPECTED_EXECUTABLE_PATH: path.resolve(executablePath),
  }
  const script = [
    '$expectedPid = [int]$env:GPTBRIDGE_EXPECTED_PROCESS_ID',
    '$expectedPort = [int]$env:GPTBRIDGE_EXPECTED_LISTEN_PORT',
    '$expectedExe = [System.IO.Path]::GetFullPath($env:GPTBRIDGE_EXPECTED_EXECUTABLE_PATH)',
    '$candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$expectedPid" -ErrorAction SilentlyContinue',
    '$listener = Get-NetTCPConnection -State Listen -LocalPort $expectedPort -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -eq $expectedPid } | Select-Object -First 1',
    'if ($null -ne $candidate -and $candidate.ExecutablePath -and $null -ne $listener -and ([System.IO.Path]::GetFullPath($candidate.ExecutablePath)).Equals($expectedExe, [System.StringComparison]::OrdinalIgnoreCase)) { "verified" }',
  ].join(';')
  try {
    const result = childProcess.spawnSync(
      'powershell.exe',
      [
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        script,
      ],
      {
        env,
        encoding: 'utf-8',
        windowsHide: true,
        timeout: 10000,
      }
    )
    return result.status === 0 && String(result.stdout || '').trim() === 'verified'
  } catch {
    return false
  }
}

function windowsProcessOwnsPort(processId, port) {
  if (
    process.platform !== 'win32' ||
    !Number.isInteger(processId) ||
    processId <= 0 ||
    !validBackendPort(port)
  ) {
    return false
  }
  const env = {
    ...process.env,
    GPTBRIDGE_EXPECTED_PROCESS_ID: String(processId),
    GPTBRIDGE_EXPECTED_LISTEN_PORT: String(port),
  }
  const script = [
    '$expectedPid = [int]$env:GPTBRIDGE_EXPECTED_PROCESS_ID',
    '$expectedPort = [int]$env:GPTBRIDGE_EXPECTED_LISTEN_PORT',
    '$listener = Get-NetTCPConnection -State Listen -LocalPort $expectedPort -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -eq $expectedPid } | Select-Object -First 1',
    'if ($null -ne $listener) { "verified" }',
  ].join(';')
  try {
    const result = childProcess.spawnSync(
      'powershell.exe',
      [
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        script,
      ],
      {
        env,
        encoding: 'utf-8',
        windowsHide: true,
        timeout: 10000,
      }
    )
    return result.status === 0 && String(result.stdout || '').trim() === 'verified'
  } catch {
    return false
  }
}

async function forceStopVerifiedUnresponsiveBackend(
  owner,
  projectRoot,
  recordedPort
) {
  if (process.platform !== 'win32' || !app.isPackaged) return false
  const verification = verifyPackagedPayload()
  const processId = Number(owner.pid || 0)
  const runtimePath = path.join(appRoot, 'python', 'python.exe')
  if (
    !verification.ok ||
    !Number.isInteger(processId) ||
    processId <= 0 ||
    !/^[0-9a-f]{64}$/i.test(String(owner.shutdown_token || '')) ||
    String(owner.package_digest || '') !==
      String(verification.packageDigest || '') ||
    !fs.existsSync(runtimePath) ||
    fs.lstatSync(runtimePath).isSymbolicLink() ||
    !windowsProcessOwnsBackend(processId, recordedPort, runtimePath)
  ) {
    return false
  }

  let controlledRoot
  try {
    const rootStat = fs.lstatSync(projectRoot)
    controlledRoot = fs.realpathSync(projectRoot)
    if (
      !rootStat.isDirectory() ||
      rootStat.isSymbolicLink() ||
      normalizedPathKey(controlledRoot) !== normalizedPathKey(projectRoot)
    ) {
      return false
    }
  } catch {
    return false
  }
  let auditRoot = controlledRoot
  for (const segment of ['runtime', 'backend-handoff-recovery']) {
    auditRoot = path.join(auditRoot, segment)
    if (!fs.existsSync(auditRoot)) {
      fs.mkdirSync(auditRoot, { mode: 0o700 })
    }
    const auditStat = fs.lstatSync(auditRoot)
    const canonicalAuditPath = fs.realpathSync(auditRoot)
    if (
      !auditStat.isDirectory() ||
      auditStat.isSymbolicLink() ||
      !isPathInside(controlledRoot, canonicalAuditPath)
    ) {
      return false
    }
    auditRoot = canonicalAuditPath
  }
  const operationId = `${Date.now()}-${crypto.randomBytes(16).toString('hex')}`
  const requested = persistRuntimeRecoveryDocument(
    auditRoot,
    `${operationId}.requested.json`,
    {
      format_version: 1,
      status: 'force-stop-requested',
      reason: 'verified-packaged-backend-health-timeout',
      tool_id: toolId,
      process_id: processId,
      backend_port: recordedPort,
      project_root: projectRoot,
      runtime_executable: runtimePath,
      runtime_sha256: sha256File(runtimePath),
      package_digest: String(verification.packageDigest || ''),
      requested_at: new Date().toISOString(),
    }
  )
  const result = childProcess.spawnSync(
    'taskkill.exe',
    ['/PID', String(processId), '/T', '/F'],
    {
      encoding: 'utf-8',
      windowsHide: true,
      timeout: 15000,
    }
  )
  const deadline = Date.now() + 10000
  while (Date.now() < deadline) {
    if (
      !processIsAlive(processId) &&
      !(await backendListening(300, recordedPort))
    ) {
      persistRuntimeRecoveryDocument(
        auditRoot,
        `${operationId}.completed.json`,
        {
          format_version: 1,
          status: 'force-stop-completed',
          reason: 'verified-packaged-backend-health-timeout',
          tool_id: toolId,
          process_id: processId,
          backend_port: recordedPort,
          project_root: projectRoot,
          package_digest: String(verification.packageDigest || ''),
          request_journal: requested.documentPath,
          taskkill_exit_code: Number(result.status ?? -1),
          completed_at: new Date().toISOString(),
        }
      )
      writeRuntimeLog('backend.verifiedHungProcessStopped', {
        previousPort: recordedPort,
        previousPid: processId,
        recoveryRoot: auditRoot,
      })
      return true
    }
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  throw new Error(
    `Verified backend PID ${processId} did not stop; ` +
      `recovery journal: ${requested.documentPath}`
  )
}

async function stopRecordedLegacyBackend(projectRoot) {
  const expectedRoot = path.resolve(projectRoot)
  const expectedWorkspace = workspaceInstanceId(projectRoot)
  const owners = [readBackendOwner(), readJson(legacyBackendOwnerPath())]
    .filter((owner) => owner && typeof owner === 'object')
    .filter(
      (owner) =>
        String(owner.tool_id || '') === toolId &&
        String(owner.project_root || '') === expectedRoot &&
        String(owner.workspace_instance_id || '') === expectedWorkspace &&
        Boolean(String(owner.shutdown_token || ''))
    )
  const seenPorts = new Set()
  let stopped = false
  for (const owner of owners) {
    const recordedPort = validBackendPort(owner.backend_port) || 8765
    if (recordedPort === backendPort || seenPorts.has(recordedPort)) continue
    seenPorts.add(recordedPort)
    if (!(await backendListening(500, recordedPort))) continue
    if (
      process.platform === 'win32' &&
      !windowsProcessOwnsPort(Number(owner.pid || 0), recordedPort)
    ) {
      writeRuntimeLog('backend.staleLegacyOwnerIgnored', {
        toolId,
        recordedPort,
        recordedProcessId: Number(owner.pid || 0),
      })
      continue
    }
    const health = await backendHealth(1000, recordedPort)
    if (!backendMatchesWorkspace(health, projectRoot)) {
      if (health) {
        writeRuntimeLog('backend.foreignLegacyPortIgnored', {
          toolId,
          recordedPort,
          recordedProcessId: Number(owner.pid || 0),
          reportedWorkspaceInstanceId: String(
            health.workspace_instance_id || ''
          ),
        })
        continue
      }
      if (
        !health &&
        (await forceStopVerifiedUnresponsiveBackend(
          owner,
          projectRoot,
          recordedPort
        ))
      ) {
        stopped = true
        continue
      }
      return {
        ok: false,
        stopped,
        message:
          `Recorded legacy backend port ${recordedPort} is active but its ` +
          'workspace identity cannot be verified.',
      }
    }
    const shutdownRequested = await requestBackendShutdown(
      String(owner.shutdown_token),
      3000,
      recordedPort
    )
    if (
      !shutdownRequested ||
      !(await waitForBackendToStop(10000, recordedPort))
    ) {
      return {
        ok: false,
        stopped,
        message: `Legacy backend on port ${recordedPort} did not stop cleanly.`,
      }
    }
    stopped = true
    writeRuntimeLog('backend.legacyPortStopped', {
      previousPort: recordedPort,
      currentPort: backendPort,
      previousPid: Number(owner.pid || 0),
    })
  }
  return { ok: true, stopped }
}

function backendLogPath() {
  const logDirectory = managedToolLogDirectory()
  fs.mkdirSync(logDirectory, { recursive: true })
  return path.join(logDirectory, 'backend.log')
}

function compactBackendLog() {
  rotateLogFile(backendLogPath(), backendLogMaxBytes)
}

function backendLogTail(maxBytes = 12000) {
  try {
    const logPath = backendLogPath()
    const stat = fs.statSync(logPath)
    const bytes = Math.min(stat.size, maxBytes)
    const descriptor = fs.openSync(logPath, 'r')
    const buffer = Buffer.allocUnsafe(bytes)
    try {
      fs.readSync(descriptor, buffer, 0, bytes, stat.size - bytes)
    } finally {
      fs.closeSync(descriptor)
    }
    return boundedLogValue(buffer.toString('utf-8').trim(), maxBytes)
  } catch {
    return ''
  }
}

function workspaceInstanceId(projectRoot) {
  let normalized = path.resolve(projectRoot).replace(/\\/g, '/')
  if (process.platform === 'win32') normalized = normalized.toLowerCase()
  return crypto
    .createHash('sha256')
    .update(normalized, 'utf-8')
    .digest('hex')
    .slice(0, 24)
}

function managedBackendCapability(projectRoot, verification) {
  const explicitRoot = String(
    process.env.GPTBRIDGE_PROJECT_ROOT || ''
  ).trim()
  if (!explicitRoot) {
    return { ok: false, errorCode: 'MANAGED_BACKEND_PROJECT_ROOT_MISSING' }
  }
  let canonicalRoot = ''
  try {
    canonicalRoot = fs.realpathSync(explicitRoot)
  } catch {
    return { ok: false, errorCode: 'MANAGED_BACKEND_PROJECT_ROOT_INVALID' }
  }
  const normalizePath = (value) => {
    const normalized = path.resolve(value).replace(/\\/g, '/')
    return process.platform === 'win32' ? normalized.toLowerCase() : normalized
  }
  if (normalizePath(canonicalRoot) !== normalizePath(projectRoot)) {
    return { ok: false, errorCode: 'MANAGED_BACKEND_PROJECT_ROOT_INVALID' }
  }
  const requiredCore = path.join(canonicalRoot, 'src-core', 'main.py')
  const requiredManifest = path.join(
    canonicalRoot,
    'platform_tools',
    toolId,
    'manifest.json'
  )
  if (!fs.existsSync(requiredCore) || !fs.existsSync(requiredManifest)) {
    return { ok: false, errorCode: 'MANAGED_BACKEND_CAPABILITY_MISSING' }
  }
  const workspaceManifest = readJson(requiredManifest)
  const requiredVersion = String(verification.backendVersion || '').trim()
  const markerToolId = String(process.env[managedBackendToolIdEnv] || '').trim()
  const markerWorkspaceId = String(
    process.env[managedBackendWorkspaceIdEnv] || ''
  ).trim()
  const markerVersion = String(
    process.env[managedBackendVersionEnv] || ''
  ).trim()
  if (
    !toolId ||
    !requiredVersion ||
    markerToolId !== toolId ||
    markerWorkspaceId !== workspaceInstanceId(canonicalRoot) ||
    markerVersion !== requiredVersion ||
    String(workspaceManifest.id || '').trim() !== toolId ||
    String(workspaceManifest.version || '').trim() !== requiredVersion
  ) {
    return { ok: false, errorCode: 'MANAGED_BACKEND_CAPABILITY_MISMATCH' }
  }
  return { ok: true, projectRoot: canonicalRoot }
}

function backendMatchesWorkspace(health, projectRoot) {
  return (
    health &&
    typeof health === 'object' &&
    String(health.workspace_instance_id || '') === workspaceInstanceId(projectRoot)
  )
}

function backendHasCurrentTool(
  health,
  projectRoot,
  verification = verifyPackagedPayload()
) {
  if (!health || typeof health !== 'object' || health.ok !== true) return false
  if (!backendMatchesWorkspace(health, projectRoot)) return false
  if (!toolId) return true
  if (governedChannelRuntimeRequested()) {
    return (
      String(health.tool_id || '') === toolId &&
      String(health.version || '') ===
        String(verification.backendVersion || manifest.version || '') &&
      health.governance_ready === true &&
      String(health.channel || '') === 'shared-layer'
    )
  }
  const services = health && typeof health === 'object' ? health.services : null
  const capabilities =
    health && typeof health === 'object' ? health.capabilities : null
  const serviceId = String(
    (manifest.backend && manifest.backend.service_id) || ''
  ).trim()
  const currentVersion = String(
    verification.backendVersion || manifest.version || ''
  ).trim()
  if (!currentVersion) return false
  if (serviceId) {
    const service =
      services && typeof services === 'object' ? services[serviceId] : null
    if (!service || typeof service !== 'object') return false
    return String(service.version || '').trim() === currentVersion
  }
  const toolbox =
    capabilities && typeof capabilities === 'object'
      ? capabilities.toolbox
      : null
  if (!toolbox || typeof toolbox !== 'object') return false
  const commands = Array.isArray(toolbox.commands) ? toolbox.commands : []
  return (
    String(toolbox.tool_id || '').trim() === toolId &&
    String(toolbox.tool_version || '').trim() === currentVersion &&
    commands.includes('toolbox_run_tool') &&
    commands.includes('toolbox_cancel_tool_run')
  )
}

function resolvePython(projectRoot) {
  const configured = String(process.env.GPTBRIDGE_PYTHON || '').trim()
  const declaredRuntime = String(
    packageMetadata.python_runtime ||
      (manifest.standalone && manifest.standalone.python_runtime) ||
      ''
  ).trim()
  const candidates = []
  if (app.isPackaged && declaredRuntime) {
    candidates.push({
      command: path.resolve(appRoot, declaredRuntime),
      prefixArgs: [],
      source: 'packaged',
    })
  }
  if (app.isPackaged && process.platform === 'win32') {
    candidates.push(
      {
        command: path.join(appRoot, 'python', 'python.exe'),
        prefixArgs: [],
        source: 'packaged',
      }
    )
  } else if (app.isPackaged) {
    candidates.push({
      command: path.join(appRoot, 'python', 'bin', 'python3'),
      prefixArgs: [],
      source: 'packaged',
    })
  }

  if (!app.isPackaged) {
    if (configured) {
      candidates.push({
        command: path.resolve(configured),
        prefixArgs: [],
        source: 'configured',
      })
    }
    if (process.platform === 'win32') {
      candidates.push({
        command: path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
        prefixArgs: [],
        source: 'project',
      })
    }
    candidates.push(
      ...(process.platform === 'win32'
        ? [
            { command: 'py', prefixArgs: ['-3'], source: 'system' },
            { command: 'python', prefixArgs: [], source: 'system' },
          ]
        : [{ command: 'python3', prefixArgs: [], source: 'system' }])
    )
  }

  for (const candidate of candidates) {
    if (app.isPackaged) {
      if (!path.isAbsolute(candidate.command)) continue
      const absoluteCandidate = path.resolve(candidate.command)
      if (!isPathInside(appRoot, absoluteCandidate)) continue
      try {
        const canonicalCandidate = fs.realpathSync(absoluteCandidate)
        const canonicalAppRoot = fs.realpathSync(appRoot)
        if (!isPathInside(canonicalAppRoot, canonicalCandidate)) continue
      } catch {
        continue
      }
    }
    if (
      path.isAbsolute(candidate.command) &&
      !fs.existsSync(candidate.command)
    ) {
      continue
    }
    const requiredImports = ['websockets']
    if (toolId === 'file-sorter') {
      requiredImports.push('PIL', 'imageio_ffmpeg')
    } else if (toolId === 'vaultly') {
      requiredImports.push('imageio_ffmpeg')
    }
    const probe = childProcess.spawnSync(
      candidate.command,
      [
        ...candidate.prefixArgs,
        '-B',
        '-s',
        '-E',
        '-X',
        'utf8',
        '-c',
        [
          'import importlib,json,sys',
          '[importlib.import_module(name) for name in json.loads(sys.argv[1])]',
          'print(sys.version_info[0], sys.version_info[1])',
        ].join(';'),
        JSON.stringify(requiredImports),
      ],
      {
        cwd: projectRoot,
        env: {
          ...process.env,
          PYTHONDONTWRITEBYTECODE: '1',
          PYTHONNOUSERSITE: '1',
        },
        windowsHide: true,
        timeout: 10000,
        encoding: 'utf-8',
      }
    )
    if (probe.status === 0) return candidate
    writeRuntimeLog('python.probeFailed', {
      source: candidate.source,
      command: candidate.command,
      status: probe.status,
      error: probe.error ? String(probe.error.message || probe.error) : '',
      stderr: boundedLogValue(probe.stderr || '', 2000),
    })
  }
  return null
}

async function ensureGovernedChannelBackendStarted(verification) {
  const projectRoot = inferProjectRoot()
  const toolRoot = path.resolve(
    String(process.env.GPTBRIDGE_TOOL_DIR || path.join(projectRoot, toolId))
  )
  const declaredEntry = String(
    packageMetadata.backend_entry ||
      (manifest.standalone && manifest.standalone.backend_entry) ||
      ''
  ).trim()
  const backendEntry = path.resolve(appRoot, declaredEntry)
  if (
    !declaredEntry ||
    !isPathInside(appRoot, backendEntry) ||
    !fs.existsSync(backendEntry)
  ) {
    return {
      ok: false,
      errorCode: 'BACKEND_ENTRY_MISSING',
      message: 'Governed channel runtime is missing from the verified package.',
    }
  }
  if (!String(process.env.GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP || '').trim()) {
    return {
      ok: false,
      errorCode: 'PERMISSION_DENIED',
      message: 'PERMISSION_DENIED',
    }
  }

  const currentHealth = await backendHealth()
  const currentOwner = readBackendOwner()
  if (
    backendHasCurrentTool(currentHealth, projectRoot, verification) &&
    backendOwnerMatches(currentOwner, projectRoot, verification)
  ) {
    return {
      ok: true,
      alreadyRunning: true,
      packageDigest: verification.packageDigest,
    }
  }
  if (await backendListening()) {
    return {
      ok: false,
      errorCode: 'BACKEND_PORT_OWNED_BY_OTHER_RUNTIME',
      message: `Port ${backendPort} is already occupied by another runtime.`,
    }
  }

  const python = resolvePython(toolRoot)
  if (!python) {
    return {
      ok: false,
      errorCode: 'PYTHON_RUNTIME_UNAVAILABLE',
      message: 'The packaged Python runtime is unavailable.',
    }
  }
  const sessionToken = backendSessionToken()
  const shutdownToken = crypto.randomBytes(32).toString('hex')
  const environment = {
    ...process.env,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONNOUSERSITE: '1',
    GPTBRIDGE_GOVERNANCE_PROJECT_ROOT: projectRoot,
    GPTBRIDGE_TOOL_DIR: toolRoot,
    GPTBRIDGE_TOOL_DATA_ROOT: path.join(toolRoot, 'runtime'),
    GPTBRIDGE_IPC_STATE_ROOT: ipcStateRoot(),
    GPTBRIDGE_IPC_SESSION_TOKEN: sessionToken,
    GPTBRIDGE_SHUTDOWN_TOKEN: shutdownToken,
    [backendPortEnv]: String(backendPort),
  }
  delete environment.GPTBRIDGE_PYTHON
  delete environment.GPTBRIDGE_ALLOW_SYSTEM_PYTHON
  backendProcess = childProcess.spawn(
    python.command,
    [
      ...python.prefixArgs,
      '-B',
      '-s',
      '-E',
      '-X',
      'utf8',
      backendEntry,
    ],
    {
      cwd: toolRoot,
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
      env: environment,
    }
  )
  backendProcess.once('error', (error) => {
    backendLastFailure = `Backend process failed to start: ${error.message}`
  })
  backendProcess.once('exit', (code, signal) => {
    if (code !== null || signal) {
      backendLastFailure =
        `Backend process exited before becoming ready (code=${String(code)}, ` +
        `signal=${String(signal || '')})`
    }
  })
  persistBackendOwner({
    tool_id: toolId,
    pid: backendProcess.pid,
    project_root: path.resolve(projectRoot),
    workspace_instance_id: workspaceInstanceId(projectRoot),
    package_digest: String(verification.packageDigest || ''),
    protocol_version: Number(verification.protocolVersion || 0),
    backend_version: String(verification.backendVersion || ''),
    backend_port: backendPort,
    shutdown_token: shutdownToken,
    started_at: new Date().toISOString(),
  })
  backendProcess.unref()

  const startedAt = Date.now()
  while (Date.now() - startedAt < 30000) {
    if (backendLastFailure) {
      return {
        ok: false,
        errorCode: 'BACKEND_PROCESS_EXITED',
        message: backendLastFailure,
      }
    }
    const health = await backendHealth(500)
    if (backendHasCurrentTool(health, projectRoot, verification)) {
      return {
        ok: true,
        started: true,
        packageDigest: verification.packageDigest,
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  return {
    ok: false,
    errorCode: 'BACKEND_STARTUP_TIMEOUT',
    message: 'Governed channel runtime did not become ready in time.',
  }
}

async function ensureBackendStarted() {
  backendLastFailure = ''
  const verification = verifyPackagedPayload()
  if (!verification.ok) {
    backendLastFailure = String(
      verification.message || 'Packaged application verification failed'
    )
    return {
      ok: false,
      errorCode: 'PACKAGE_VERIFICATION_FAILED',
      message: backendLastFailure,
    }
  }
  if (governedChannelRuntimeRequested()) {
    return ensureGovernedChannelBackendStarted(verification)
  }
  const projectRoot = inferProjectRoot()
  let portAlreadyInUse = await backendListening()
  if (!managedBackendReuseRequested() && portAlreadyInUse) {
    const currentHealth = await backendHealth()
    const currentOwner = readBackendOwner()
    if (
      backendHasCurrentTool(currentHealth, projectRoot, verification) &&
      backendOwnerMatches(currentOwner, projectRoot, verification)
    ) {
      writeRuntimeLog('backend.currentReuseBeforeLegacyMigration', {
        toolId,
        backendPort,
        packageDigest: verification.packageDigest,
      })
      return {
        ok: true,
        alreadyRunning: true,
        packageDigest: verification.packageDigest,
      }
    }
  }
  if (!managedBackendReuseRequested()) {
    const legacyBackend = await stopRecordedLegacyBackend(projectRoot)
    if (!legacyBackend.ok) {
      backendLastFailure = String(
        legacyBackend.message || 'A legacy standalone backend could not be stopped.'
      )
      return {
        ok: false,
        errorCode: 'LEGACY_BACKEND_MIGRATION_FAILED',
        message: backendLastFailure,
      }
    }
  }

  portAlreadyInUse = await backendListening()
  if (managedBackendReuseRequested()) {
    const capability = managedBackendCapability(projectRoot, verification)
    if (!capability.ok) {
      backendLastFailure =
        'Managed backend reuse capability is missing or does not match this tool and workspace.'
      return {
        ok: false,
        errorCode: capability.errorCode,
        message: backendLastFailure,
      }
    }
    if (!portAlreadyInUse) {
      backendLastFailure =
        'The GPTBridge managed backend is not running. Start GPTBridge before opening this tool.'
      return {
        ok: false,
        errorCode: 'MANAGED_BACKEND_UNAVAILABLE',
        message: backendLastFailure,
      }
    }
    const health = await backendHealth()
    if (!backendMatchesWorkspace(health, projectRoot)) {
      backendLastFailure =
        `Port ${backendPort} belongs to a different GPTBridge workspace.`
      return {
        ok: false,
        errorCode: 'BACKEND_PORT_OWNED_BY_OTHER_RUNTIME',
        message: backendLastFailure,
      }
    }
    if (!backendHasCurrentTool(health, projectRoot, verification)) {
      backendLastFailure =
        'The running GPTBridge backend does not provide the required tool service version. Restart GPTBridge after updating it.'
      return {
        ok: false,
        errorCode: 'MANAGED_BACKEND_SERVICE_VERSION_MISMATCH',
        message: backendLastFailure,
      }
    }
    writeRuntimeLog('backend.managedReuse', {
      toolId,
      projectRoot: path.resolve(projectRoot),
      expectedVersion: verification.backendVersion,
    })
    return {
      ok: true,
      alreadyRunning: true,
      managedBackend: true,
      packageDigest: verification.packageDigest,
    }
  }
  const sessionToken = backendSessionToken()
  if (portAlreadyInUse) {
    const health = await backendHealth()
    if (!backendMatchesWorkspace(health, projectRoot)) {
      backendLastFailure =
        `Port ${backendPort} belongs to a different GPTBridge runtime. Close that runtime before starting this application.`
      return {
        ok: false,
        errorCode: 'BACKEND_PORT_OWNED_BY_OTHER_RUNTIME',
        message: backendLastFailure,
      }
    }
    const owner = readBackendOwner()
    if (
      backendHasCurrentTool(health, projectRoot, verification) &&
      backendOwnerMatches(owner, projectRoot, verification)
    ) {
      return {
        ok: true,
        alreadyRunning: true,
        packageDigest: verification.packageDigest,
      }
    }
    writeRuntimeLog('backend.staleToolService', {
      toolId,
      expectedVersion: verification.backendVersion,
      expectedPackageDigest: verification.packageDigest,
      ownerPackageDigest:
        owner && typeof owner === 'object'
          ? String(owner.package_digest || '')
          : '',
      health,
    })
    const ownerIsThisRuntime =
      owner &&
      String(owner.project_root || '') === path.resolve(projectRoot) &&
      String(owner.workspace_instance_id || '') === workspaceInstanceId(projectRoot)
    if (!ownerIsThisRuntime || !String(owner.shutdown_token || '')) {
      backendLastFailure =
        'An unverified backend is already active for this application. Stop it before retrying; it was not started with a verifiable package identity.'
      return {
        ok: false,
        errorCode: 'BACKEND_IDENTITY_MISMATCH',
        message: backendLastFailure,
      }
    }
    const shutdownRequested = await requestBackendShutdown(
      String(owner.shutdown_token)
    )
    if (!shutdownRequested || !(await waitForBackendToStop())) {
      backendLastFailure =
        'The previous backend did not stop cleanly. Close it before retrying.'
      return {
        ok: false,
        errorCode: 'BACKEND_RESTART_FAILED',
        message: backendLastFailure,
      }
    }
    portAlreadyInUse = false
    writeRuntimeLog('backend.previousPackageStopped', {
      previousPid: Number(owner.pid || 0),
      previousPackageDigest: String(owner.package_digest || ''),
    })
  }

  try {
    migrateLegacyFileSorterProfiles(projectRoot)
  } catch (error) {
    backendLastFailure = `Could not migrate the legacy file-sorter profiles: ${
      error instanceof Error ? error.message : String(error)
    }`
    return {
      ok: false,
      errorCode: 'FILE_SORTER_STATE_MIGRATION_FAILED',
      message: backendLastFailure,
    }
  }

  try {
    preparePackagedBackendRuntime(projectRoot, verification)
  } catch (error) {
    backendLastFailure = `Could not install the verified backend package: ${
      error instanceof Error ? error.message : String(error)
    }`
    return {
      ok: false,
      errorCode: 'BACKEND_PACKAGE_INSTALL_FAILED',
      message: backendLastFailure,
    }
  }
  const backendEntry = path.join(projectRoot, 'src-core', 'main.py')
  if (!fs.existsSync(backendEntry)) {
    backendLastFailure = `Packaged backend entry not found: ${backendEntry}`
    return {
      ok: false,
      errorCode: 'BACKEND_ENTRY_MISSING',
      message: backendLastFailure,
    }
  }

  if (!backendProcess || backendProcess.exitCode !== null) {
    const python = resolvePython(projectRoot)
    if (!python) {
      backendLastFailure =
        'The packaged Python runtime is missing or cannot import required backend modules.'
      return {
        ok: false,
        errorCode: 'PYTHON_RUNTIME_UNAVAILABLE',
        message: backendLastFailure,
      }
    }
    const args = [backendEntry, '--serve']
    const shutdownToken = crypto.randomBytes(32).toString('hex')
    compactBackendLog()
    const backendLogDescriptor = fs.openSync(backendLogPath(), 'a')
    const backendEnvironment = {
      ...process.env,
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONDONTWRITEBYTECODE: '1',
      PYTHONNOUSERSITE: '1',
      GPTBRIDGE_PROJECT_ROOT: projectRoot,
      GPTBRIDGE_IPC_STATE_ROOT: ipcStateRoot(),
      GPTBRIDGE_STANDALONE_TOOL_ID: toolId,
      [backendPortEnv]: String(backendPort),
      GPTBRIDGE_IPC_SESSION_TOKEN: sessionToken,
      GPTBRIDGE_SHUTDOWN_TOKEN: shutdownToken,
      GPTBRIDGE_PACKAGE_DIGEST: String(verification.packageDigest || ''),
      GPTBRIDGE_PACKAGE_PROTOCOL_VERSION: String(
        verification.protocolVersion || ''
      ),
    }
    applyDeclaredEnvironmentBindings(backendEnvironment)
    if (app.isPackaged) {
      delete backendEnvironment.GPTBRIDGE_PYTHON
      delete backendEnvironment.GPTBRIDGE_ALLOW_SYSTEM_PYTHON
    }
    try {
      backendProcess = childProcess.spawn(
        python.command,
        [
          ...python.prefixArgs,
          '-B',
          '-s',
          '-E',
          '-X',
          'utf8',
          ...args,
        ],
        {
          cwd: projectRoot,
          detached: true,
          stdio: ['ignore', backendLogDescriptor, backendLogDescriptor],
          windowsHide: true,
          env: backendEnvironment,
        }
      )
    } finally {
      fs.closeSync(backendLogDescriptor)
    }
    backendProcess.once('error', (error) => {
      backendLastFailure = `Backend process failed to start: ${error.message}`
      writeRuntimeLog('backend.processError', {
        message: error.message,
        stack: error.stack,
      })
    })
    backendProcess.once('exit', (code, signal) => {
      if (code !== null || signal) {
        backendLastFailure =
          `Backend process exited before becoming ready (code=${String(
            code
          )}, signal=${String(signal || '')})`
      }
      writeRuntimeLog('backend.processExit', {
        code,
        signal,
        pid: backendProcess ? backendProcess.pid : 0,
        logTail: backendLogTail(4000),
      })
    })
    persistBackendOwner({
      tool_id: toolId,
      pid: backendProcess.pid,
      project_root: path.resolve(projectRoot),
      workspace_instance_id: workspaceInstanceId(projectRoot),
      package_digest: String(verification.packageDigest || ''),
      protocol_version: Number(verification.protocolVersion || 0),
      backend_version: String(verification.backendVersion || ''),
      backend_port: backendPort,
      shutdown_token: shutdownToken,
      started_at: new Date().toISOString(),
    })
    backendProcess.unref()
    writeRuntimeLog('backend.spawned', {
      hotReloadEnabled,
      entry: args[0],
      pythonSource: python.source,
      pid: backendProcess.pid,
      packageDigest: verification.packageDigest,
    })
  }

  const startedAt = Date.now()
  while (Date.now() - startedAt < 30000) {
    if (backendLastFailure) {
      return {
        ok: false,
        errorCode: 'BACKEND_PROCESS_EXITED',
        message: `${backendLastFailure}${
          backendLogTail() ? `\n${backendLogTail()}` : ''
        }`,
      }
    }
    const health = await backendHealth(500)
    if (backendHasCurrentTool(health, projectRoot, verification)) {
      return {
        ok: true,
        started: true,
        packageDigest: verification.packageDigest,
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 500))
  }

  const logTail = backendLogTail()
  backendLastFailure = `Backend startup timed out before the expected service version became ready${
    logTail ? `\n${logTail}` : ''
  }`
  writeRuntimeLog('backend.startupTimeout', {
    packageDigest: verification.packageDigest,
    logTail,
  })
  return {
    ok: false,
    errorCode: 'BACKEND_STARTUP_TIMEOUT',
    message: backendLastFailure,
  }
}

function revealMainWindow(reason) {
  if (!mainWindow || mainWindow.isDestroyed()) return false
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
  writeRuntimeLog('window.foregroundShown', { toolId, reason })
  return true
}

function requestMainWindowForeground(reason) {
  foregroundRequested = true
  if (!mainWindow || mainWindow.isDestroyed()) {
    writeRuntimeLog('window.foregroundDeferred', {
      toolId,
      reason,
      state: 'window-missing',
    })
    createWindow()
    return
  }
  if (!mainWindowReady) {
    writeRuntimeLog('window.foregroundDeferred', {
      toolId,
      reason,
      state: 'renderer-loading',
    })
    return
  }
  revealMainWindow(reason)
}

function resolveHotUpdateRenderer() {
  const projectRoot = inferProjectRoot()
  const rendererRoot = path.join(
    projectRoot,
    'runtime',
    'hot-update',
    'renderer'
  )
  const markerPath = path.join(rendererRoot, 'update.json')
  const indexPath = path.join(rendererRoot, 'index.html')
  try {
    if (!fs.existsSync(markerPath) || !fs.existsSync(indexPath)) return ''
    if (!isPathInside(projectRoot, rendererRoot)) return ''
    const marker = readJson(markerPath)
    const files = marker.files
    if (
      Number(marker.format_version || 0) !== 1 ||
      String(marker.tool_id || '') !== toolId ||
      String(marker.tool_version || '') !== String(manifest.version || '') ||
      !files ||
      typeof files !== 'object' ||
      Array.isArray(files) ||
      !Object.prototype.hasOwnProperty.call(files, 'index.html') ||
      Object.keys(files).length > 2000
    ) {
      return ''
    }
    for (const [relativePath, expectedDigest] of Object.entries(files)) {
      if (
        typeof relativePath !== 'string' ||
        !relativePath ||
        path.isAbsolute(relativePath) ||
        relativePath.includes('..') ||
        !/^[0-9a-f]{64}$/.test(String(expectedDigest || ''))
      ) {
        return ''
      }
      const candidate = path.resolve(rendererRoot, relativePath)
      if (!isPathInside(rendererRoot, candidate)) return ''
      const metadata = fs.lstatSync(candidate)
      if (!metadata.isFile() || metadata.isSymbolicLink()) return ''
      const digest = crypto
        .createHash('sha256')
        .update(fs.readFileSync(candidate))
        .digest('hex')
      if (digest !== expectedDigest) return ''
    }
    writeRuntimeLog('renderer.hotUpdateSelected', {
      toolId,
      rendererRoot,
      fileCount: Object.keys(files).length,
    })
    return indexPath
  } catch (error) {
    writeRuntimeLog('renderer.hotUpdateRejected', {
      toolId,
      message: error instanceof Error ? error.message : String(error),
    })
    return ''
  }
}

function startRendererWatch(rendererPath) {
  if (!fs.existsSync(rendererPath)) return
  if (rendererWatchedPath && rendererWatchedPath !== rendererPath) {
    fs.unwatchFile(rendererWatchedPath)
  }
  rendererWatchedPath = rendererPath
  fs.watchFile(rendererPath, { interval: 500 }, () => {
    if (!mainWindow || mainWindow.isDestroyed()) return
    if (rendererReloadTimer) {
      clearTimeout(rendererReloadTimer)
      rendererReloadTimer = null
    }
    rendererReloadTimer = setTimeout(() => {
      if (!mainWindow || mainWindow.isDestroyed()) return
      mainWindow.webContents.reloadIgnoringCache()
    }, 500)
  })
}

function stopRendererWatch() {
  if (rendererReloadTimer) {
    clearTimeout(rendererReloadTimer)
    rendererReloadTimer = null
  }
  if (rendererWatchedPath) {
    fs.unwatchFile(rendererWatchedPath)
    rendererWatchedPath = ''
  }
}

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (foregroundRequested && mainWindowReady) {
      revealMainWindow('create-window-existing')
    }
    return
  }

  const windowConfig =
    manifest.window && typeof manifest.window === 'object' ? manifest.window : {}

  mainWindowReady = false
  mainWindow = new BrowserWindow({
    width: Number(windowConfig.width) || 1180,
    height: Number(windowConfig.height) || 820,
    minWidth: Number(windowConfig.minWidth) || 900,
    minHeight: Number(windowConfig.minHeight) || 620,
    show: false,
    backgroundColor: '#0b0f17',
    title: String(windowConfig.title || toolName),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(appRoot, 'preload.cjs'),
    },
  })

  mainWindow.once('ready-to-show', () => {
    mainWindowReady = true
    if (!foregroundRequested) {
      writeRuntimeLog('window.backgroundStartReady', { toolId })
      return
    }
    revealMainWindow(startHidden ? 'foreground-requested' : 'normal-start')
  })

  mainWindow.on('closed', () => {
    writeRuntimeLog('window.closed', { toolId })
    mainWindow = null
    mainWindowReady = false
  })

  mainWindow.on('unresponsive', () => {
    writeRuntimeLog('window.unresponsive', { toolId })
  })

  mainWindow.on('responsive', () => {
    writeRuntimeLog('window.responsive', { toolId })
  })

  mainWindow.webContents.on('did-fail-load', (_event, code, desc) => {
    writeRuntimeLog('window.didFailLoad', { code, desc })
    console.error('[GPTBridge Tool] Renderer load failed:', code, desc)
  })

  mainWindow.webContents.on('did-finish-load', () => {
    writeRuntimeLog('window.didFinishLoad', { toolId })
    mainWindow.webContents
      .executeJavaScript(
        `({
          electronInvoke: typeof window.electron?.invoke === 'function',
          standaloneTool: window.gptBridge?.standaloneTool === true,
          rootPresent: Boolean(document.getElementById('root'))
        })`
      )
      .then((status) => writeRuntimeLog('window.bridgeStatus', status))
      .catch((error) =>
        writeRuntimeLog('window.bridgeStatusFailed', {
          message: error instanceof Error ? error.message : String(error),
        })
      )
  })

  mainWindow.webContents.on('console-message', (_event, details) => {
    const level =
      details && typeof details === 'object'
        ? String(details.level || '').toLowerCase()
        : ''
    if (!['error', 'warning', '2', '3'].includes(level)) return
    writeRuntimeLog('renderer.console', {
      toolId,
      level,
      message: boundedLogValue(
        details && typeof details === 'object' ? details.message || '' : '',
        4000
      ),
      lineNumber:
        details && typeof details === 'object'
          ? Number(details.lineNumber || 0)
          : 0,
      sourceId: boundedLogValue(
        details && typeof details === 'object' ? details.sourceId || '' : '',
        1000
      ),
    })
  })

  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    writeRuntimeLog('renderer.processGone', details)
    console.error('[GPTBridge Tool] Renderer process gone:', details)
  })

  const packagedRendererPath = path.join(appRoot, 'renderer', 'index.html')
  const rendererPath = resolveHotUpdateRenderer() || packagedRendererPath
  mainWindow.loadFile(rendererPath)
    .then(() => {
      startRendererWatch(rendererPath)
    })
    .catch((error) => writeRuntimeLog('window.loadFile.failed', {
      rendererPath,
      message: error instanceof Error ? error.message : String(error),
      stack: error instanceof Error ? error.stack : undefined,
    }))
}

ipcMain.handle('app:ensure-backend-started', async () => {
  writeRuntimeLog('backend.ensureRequested', { toolId })
  try {
    const result = await ensureBackendStarted()
    writeRuntimeLog('backend.ensureCompleted', {
      toolId,
      ok: result?.ok === true,
      errorCode: String(result?.errorCode || ''),
      alreadyRunning: result?.alreadyRunning === true,
      started: result?.started === true,
      message: boundedLogValue(result?.message || '', 2000),
    })
    return result
  } catch (error) {
    backendLastFailure =
      error instanceof Error ? error.message : String(error)
    writeRuntimeLog('backend.ensureFailed', {
      message: backendLastFailure,
      stack: error instanceof Error ? error.stack : undefined,
    })
    return {
      ok: false,
      errorCode: 'BACKEND_ENSURE_FAILED',
      message: backendLastFailure,
    }
  }
})
ipcMain.handle('app:get-backend-session', async () => backendSessionDescriptor())

ipcMain.handle('dialog:select-folder', async () => {
  const result = await dialog.showOpenDialog({ properties: ['openDirectory'] })
  const selected = result.canceled ? '' : result.filePaths[0] || ''
  registerOpenPathCapability(selected, true)
  return selected
})

ipcMain.handle('dialog:create-file', async (_event, defaultPath = '') => {
  const result = await dialog.showSaveDialog({
    defaultPath: String(defaultPath || ''),
  })
  const selected = result.canceled ? '' : result.filePath || ''
  registerOpenPathCapability(selected, false)
  return selected
})

ipcMain.handle('dialog:open-file', async (_event, defaultPath = '') => {
  const result = await dialog.showOpenDialog({
    defaultPath: String(defaultPath || ''),
    properties: ['openFile'],
  })
  const selected = result.canceled ? '' : result.filePaths[0] || ''
  registerOpenPathCapability(selected, false)
  return selected
})

ipcMain.handle('app:open-path', async (_event, payload = {}) => {
  const projectRoot = inferProjectRoot()
  const rawPath = String(payload.path || '').trim()
  const basePath = String(payload.basePath || projectRoot).trim()
  const relativePath = String(payload.relativePath || '').trim()
  const mode = String(payload.mode || 'open')
  const targetPath = rawPath
    ? path.resolve(rawPath)
    : path.resolve(basePath, relativePath)

  let canonicalProjectRoot = path.resolve(projectRoot)
  let canonicalTargetPath = targetPath
  try {
    canonicalProjectRoot = fs.realpathSync(projectRoot)
    canonicalTargetPath = fs.realpathSync(targetPath)
  } catch {
    // shell.openPath cannot use an absent target, but an exact save-dialog
    // capability may still authorize the path once it appears.
  }
  const insideWorkspace = isPathInside(
    canonicalProjectRoot,
    canonicalTargetPath
  )
  if (
    !insideWorkspace &&
    !consumeOpenPathCapability(canonicalTargetPath)
  ) {
    return { ok: false, message: 'Path is outside the application workspace' }
  }

  try {
    if (mode === 'reveal') {
      shell.showItemInFolder(targetPath)
    } else {
      await shell.openPath(targetPath)
    }
    return { ok: true, path: targetPath }
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : String(error),
    }
  }
})

if (hasSingleInstanceLock) {
  app.whenReady().then(() => {
    Menu.setApplicationMenu(null)
    const backendLogTimer = setInterval(compactBackendLog, 60000)
    backendLogTimer.unref()
    createWindow()
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })
}

app.on('window-all-closed', () => {
  stopRendererWatch()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', (event) => {
  stopRendererWatch()
  if (quitAfterBackendShutdown) return
  event.preventDefault()
  quitAfterBackendShutdown = true
  void shutdownOwnedBackendBeforeExit()
    .catch((error) => {
      writeRuntimeLog('backend.shutdownOnWindowCloseFailed', {
        toolId,
        message: error instanceof Error ? error.message : String(error),
      })
    })
    .finally(() => app.quit())
})

if (process.env.GPTBRIDGE_TEMPLATE_TEST_MODE === '1') {
  module.exports = {
    acquireRuntimePackageLock,
    inventoryOwnedRuntimeTree,
    migrateLegacyFileSorterProfiles,
    preparePackagedBackendRuntime,
    reconcileInterruptedRuntimeRecoveries,
    releaseRuntimePackageLock,
  }
}
