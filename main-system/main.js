"use strict";
const electron = require("electron");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const node_child_process = require("node:child_process");
const child_process = require("child_process");
const http = require("node:http");
const MIN_VIEWPORT_SCALE = 0.72;
const MAX_VIEWPORT_SCALE = 1.5;
const MIN_EFFECTIVE_ZOOM = 0.62;
const MAX_EFFECTIVE_ZOOM = 1.6;
const RESIZE_DEBOUNCE_MS = 60;
function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}
function calculateAdaptiveZoomFactor(input) {
  const width = Math.max(1, input.width);
  const height = Math.max(1, input.height);
  const referenceWidth = Math.max(1, input.referenceWidth);
  const referenceHeight = Math.max(1, input.referenceHeight);
  const preferredFactor = Number.isFinite(input.preferredFactor) && input.preferredFactor > 0 ? input.preferredFactor : 1;
  const viewportScale = clamp(
    Math.min(width / referenceWidth, height / referenceHeight),
    MIN_VIEWPORT_SCALE,
    MAX_VIEWPORT_SCALE
  );
  const effectiveFactor = clamp(
    viewportScale * preferredFactor,
    MIN_EFFECTIVE_ZOOM,
    MAX_EFFECTIVE_ZOOM
  );
  return Math.round(effectiveFactor * 1e3) / 1e3;
}
class AdaptiveZoomController {
  constructor(getPreferredFactor) {
    this.getPreferredFactor = getPreferredFactor;
  }
  getPreferredFactor;
  profiles = /* @__PURE__ */ new Map();
  register(window) {
    const [referenceWidth, referenceHeight] = window.getContentSize();
    this.profiles.set(window.id, {
      window,
      referenceWidth: Math.max(1, referenceWidth),
      referenceHeight: Math.max(1, referenceHeight),
      lastAppliedFactor: 0,
      resizeTimer: null
    });
    const scheduleUpdate = () => this.schedule(window);
    window.on("resize", scheduleUpdate);
    window.on("maximize", scheduleUpdate);
    window.on("unmaximize", scheduleUpdate);
    window.on("enter-full-screen", scheduleUpdate);
    window.on("leave-full-screen", scheduleUpdate);
    window.webContents.on("did-finish-load", scheduleUpdate);
    window.on("closed", () => this.unregister(window.id));
    this.apply(window);
  }
  applyAll() {
    for (const profile of this.profiles.values()) {
      this.apply(profile.window);
    }
  }
  schedule(window) {
    const profile = this.profiles.get(window.id);
    if (!profile) return;
    if (profile.resizeTimer) clearTimeout(profile.resizeTimer);
    profile.resizeTimer = setTimeout(() => {
      profile.resizeTimer = null;
      this.apply(window);
    }, RESIZE_DEBOUNCE_MS);
  }
  apply(window) {
    const profile = this.profiles.get(window.id);
    if (!profile || window.isDestroyed() || window.webContents.isDestroyed()) return;
    const [width, height] = window.getContentSize();
    const factor = calculateAdaptiveZoomFactor({
      width,
      height,
      referenceWidth: profile.referenceWidth,
      referenceHeight: profile.referenceHeight,
      preferredFactor: this.getPreferredFactor()
    });
    if (Math.abs(profile.lastAppliedFactor - factor) < 5e-3) return;
    profile.lastAppliedFactor = factor;
    window.webContents.setZoomFactor(factor);
  }
  unregister(windowId) {
    const profile = this.profiles.get(windowId);
    if (profile?.resizeTimer) clearTimeout(profile.resizeTimer);
    this.profiles.delete(windowId);
  }
}
function toAbsolute(p) {
  return path.resolve(p);
}
function firstExisting(candidates) {
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}
function hasWorkspaceMarkers(candidate) {
  return fs.existsSync(path.join(candidate, "governance_rule")) && fs.existsSync(path.join(candidate, "governance_rule", "permission_directory")) && fs.existsSync(path.join(candidate, "main-system", "src-core"));
}
function findPackagedWorkspaceRoot(executableDir, resourcesRoot) {
  const candidates = [
    resourcesRoot,
    executableDir
  ].map(toAbsolute);
  const workspaceRoot = candidates.find(hasWorkspaceMarkers);
  return workspaceRoot ?? resourcesRoot;
}
function pythonExecutableCandidatesFor(root) {
  if (process.platform === "win32") {
    return [
      toAbsolute(path.join(root, ".venv", "Scripts", "pythonw.exe")),
      toAbsolute(path.join(root, ".venv", "Scripts", "python.exe"))
    ];
  }
  return [toAbsolute(path.join(root, ".venv", "bin", "python"))];
}
function resolveFromPath(name) {
  try {
    const command = process.platform === "win32" ? `where.exe ${name}` : `which ${name}`;
    const output = node_child_process.execSync(command, { encoding: "utf-8", timeout: 3e3 }).split(/\r?\n/).map((line) => line.trim()).filter(Boolean)[0];
    return output && fs.existsSync(output) ? output : null;
  } catch {
    return null;
  }
}
function getRuntimePathLibrary() {
  const mode = electron.app.isPackaged ? "packaged" : "source-production";
  const executableDir = toAbsolute(path.dirname(electron.app.getPath("exe")));
  const packagedResourcesRoot = toAbsolute(path.join(executableDir, "resources"));
  const workspaceRoot = electron.app.isPackaged ? findPackagedWorkspaceRoot(executableDir, packagedResourcesRoot) : toAbsolute("E:\\GPTBridge");
  const resourcesRoot = electron.app.isPackaged ? packagedResourcesRoot : toAbsolute(path.join(workspaceRoot, "main-system", "resources"));
  const appRoot = electron.app.isPackaged ? toAbsolute(path.join(resourcesRoot, "app")) : toAbsolute(path.join(workspaceRoot, "main-system"));
  const unpackedRoot = electron.app.isPackaged ? toAbsolute(path.join(resourcesRoot, "app.asar.unpacked")) : toAbsolute(path.join(workspaceRoot, "main-system"));
  const pathPython = resolveFromPath(process.platform === "win32" ? "python" : "python3");
  const pathPythonw = process.platform === "win32" ? resolveFromPath("pythonw") : null;
  const pathPythonCandidates = (electron.app.isPackaged ? [pathPythonw, pathPython] : [pathPython, pathPythonw]).filter((c) => typeof c === "string" && c.length > 0);
  const pythonExecutableCandidates = [
    ...pythonExecutableCandidatesFor(resourcesRoot),
    ...pythonExecutableCandidatesFor(unpackedRoot),
    ...pythonExecutableCandidatesFor(appRoot),
    ...pythonExecutableCandidatesFor(workspaceRoot),
    ...pathPythonCandidates
  ];
  const pythonEntryCandidates = [
    toAbsolute(path.join(resourcesRoot, "src-core", "main.py")),
    toAbsolute(path.join(unpackedRoot, "src-core", "main.py")),
    toAbsolute(path.join(appRoot, "src-core", "main.py")),
    toAbsolute(path.join(workspaceRoot, "src-core", "main.py"))
  ];
  const bootCoreEntryCandidates = [
    toAbsolute(path.join(resourcesRoot, "src-core", "boot_core.py")),
    toAbsolute(path.join(unpackedRoot, "src-core", "boot_core.py")),
    toAbsolute(path.join(appRoot, "src-core", "boot_core.py")),
    toAbsolute(path.join(workspaceRoot, "src-core", "boot_core.py"))
  ];
  const pythonSourceRepairEntryCandidates = [
    toAbsolute(path.join(resourcesRoot, "src-core", "tasks", "source_repair.py")),
    toAbsolute(path.join(unpackedRoot, "src-core", "tasks", "source_repair.py")),
    toAbsolute(path.join(appRoot, "src-core", "tasks", "source_repair.py")),
    toAbsolute(path.join(workspaceRoot, "src-core", "tasks", "source_repair.py"))
  ];
  let pythonExecutable = firstExisting(pythonExecutableCandidates) ?? pythonExecutableCandidates[0];
  if (process.platform === "win32" && pythonExecutable.toLowerCase().endsWith("python.exe")) {
    const pythonw = pythonExecutable.replace(/\\python\.exe$/i, "\\pythonw.exe");
    if (fs.existsSync(pythonw)) {
      pythonExecutable = pythonw;
    }
  }
  const pythonEntry = firstExisting(pythonEntryCandidates) ?? pythonEntryCandidates[0];
  const bootCoreEntry = firstExisting(bootCoreEntryCandidates) ?? pythonEntry;
  const pythonSourceRepairEntry = firstExisting(pythonSourceRepairEntryCandidates) ?? pythonSourceRepairEntryCandidates[0];
  return {
    mode,
    executableDir,
    workspaceRoot,
    resourcesRoot,
    appRoot,
    unpackedRoot,
    preloadEntry: toAbsolute(path.join(__dirname, "preload.js")),
    rendererEntryHtml: toAbsolute(path.join(__dirname, "../renderer/index.html")),
    pythonExecutable,
    pythonEntry,
    bootCoreEntry,
    pythonSourceRepairEntry,
    pythonExecutableCandidates,
    pythonEntryCandidates,
    bootCoreEntryCandidates,
    pythonSourceRepairEntryCandidates
  };
}
var define_process_env_default$1 = {};
const TOKEN_PATTERN = /^[a-f0-9]{64}$/;
const TOKEN_FILE_NAME = "session-token";
const TOKEN_LOCK_NAME = ".session-token.lock";
const LOCK_WAIT_MS = 1e4;
const STALE_LOCK_MS = 5e3;
let cachedToken = "";
function sleepSync(milliseconds) {
  Atomics.wait(
    new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT)),
    0,
    0,
    milliseconds
  );
}
function hardenPrivatePath(targetPath, { directory = false } = {}) {
  try {
    fs.chmodSync(targetPath, directory ? 448 : 384);
  } catch {
  }
  if (process.platform !== "win32") return;
  const username = String(define_process_env_default$1.USERNAME || "").trim();
  if (!username) return;
  const userPermission = directory ? "(OI)(CI)(F)" : "(R,W)";
  const systemPermission = directory ? "(OI)(CI)(F)" : "(F)";
  try {
    node_child_process.spawnSync(
      "icacls.exe",
      [
        targetPath,
        "/inheritance:r",
        "/grant:r",
        `${username}:${userPermission}`,
        "/grant:r",
        `*S-1-5-18:${systemPermission}`
      ],
      { windowsHide: true, stdio: "ignore" }
    );
  } catch {
  }
}
function getIpcStateRoot() {
  const configured = String(define_process_env_default$1.GPTBRIDGE_IPC_STATE_ROOT || "").trim();
  if (configured) return path.resolve(configured);
  if (process.platform === "win32") {
    const localAppData = String(define_process_env_default$1.LOCALAPPDATA || "").trim();
    const base2 = localAppData || path.join(os.homedir(), "AppData", "Local");
    return path.resolve(base2, "GPTBridge", "ipc");
  }
  const xdgStateHome = String(define_process_env_default$1.XDG_STATE_HOME || "").trim();
  const base = xdgStateHome || path.join(os.homedir(), ".local", "state");
  return path.resolve(base, "GPTBridge", "ipc");
}
function tokenFilePath() {
  return path.join(getIpcStateRoot(), TOKEN_FILE_NAME);
}
function readToken(filePath) {
  try {
    const token = fs.readFileSync(filePath, "utf-8").trim().toLowerCase();
    return TOKEN_PATTERN.test(token) ? token : "";
  } catch {
    return "";
  }
}
function breakStaleLock(lockPath) {
  let ageMs = 0;
  try {
    ageMs = Date.now() - fs.lstatSync(lockPath).mtimeMs;
  } catch {
    return;
  }
  if (ageMs < STALE_LOCK_MS) return;
  const stalePath = `${lockPath}.stale-${process.pid}-${crypto.randomBytes(6).toString("hex")}`;
  try {
    fs.renameSync(lockPath, stalePath);
  } catch {
    return;
  }
  try {
    fs.rmdirSync(stalePath);
  } catch {
  }
}
function writeTokenAtomically(filePath, token) {
  const temporaryPath = path.join(
    path.dirname(filePath),
    `.${TOKEN_FILE_NAME}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`
  );
  let descriptor = null;
  try {
    descriptor = fs.openSync(temporaryPath, "wx", 384);
    fs.writeFileSync(descriptor, `${token}
`, "utf-8");
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = null;
    fs.renameSync(temporaryPath, filePath);
  } finally {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor);
      } catch {
      }
    }
    try {
      fs.unlinkSync(temporaryPath);
    } catch {
    }
  }
}
function repairOrCreateToken(filePath) {
  const stateRoot = path.dirname(filePath);
  fs.mkdirSync(stateRoot, { recursive: true, mode: 448 });
  hardenPrivatePath(stateRoot, { directory: true });
  const lockPath = path.join(stateRoot, TOKEN_LOCK_NAME);
  const deadline = Date.now() + LOCK_WAIT_MS;
  let ownsLock = false;
  let ownerNonce = "";
  while (!ownsLock) {
    const racedToken = readToken(filePath);
    if (racedToken) return racedToken;
    let createdLock = false;
    try {
      fs.mkdirSync(lockPath, { mode: 448 });
      createdLock = true;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
    }
    if (createdLock) {
      try {
        ownerNonce = crypto.randomBytes(16).toString("hex");
        fs.writeFileSync(path.join(lockPath, "owner"), `${ownerNonce}
`, {
          encoding: "ascii",
          flag: "wx",
          mode: 384
        });
        ownsLock = true;
        break;
      } catch (error) {
        try {
          fs.unlinkSync(path.join(lockPath, "owner"));
        } catch {
        }
        try {
          fs.rmdirSync(lockPath);
        } catch {
        }
        throw error;
      }
    }
    breakStaleLock(lockPath);
    if (Date.now() >= deadline) {
      const finalToken = readToken(filePath);
      if (finalToken) return finalToken;
      throw new Error(`Timed out acquiring IPC token lock: ${lockPath}`);
    }
    sleepSync(25);
  }
  const stillOwnsLock = () => {
    if (!ownsLock || !ownerNonce) return false;
    try {
      return fs.readFileSync(path.join(lockPath, "owner"), "ascii").trim() === ownerNonce;
    } catch {
      return false;
    }
  };
  try {
    const racedToken = readToken(filePath);
    if (racedToken) return racedToken;
    if (!stillOwnsLock()) throw new Error("Lost IPC token repair lock");
    fs.utimesSync(lockPath, /* @__PURE__ */ new Date(), /* @__PURE__ */ new Date());
    try {
      fs.lstatSync(filePath);
      const quarantinePath = path.join(
        stateRoot,
        `${TOKEN_FILE_NAME}.invalid-${Date.now()}-${process.pid}-${crypto.randomBytes(6).toString("hex")}`
      );
      fs.renameSync(filePath, quarantinePath);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    const generated = crypto.randomBytes(32).toString("hex");
    writeTokenAtomically(filePath, generated);
    hardenPrivatePath(filePath);
    const persisted = readToken(filePath);
    if (!persisted) throw new Error("IPC session token write verification failed");
    return persisted;
  } finally {
    if (stillOwnsLock()) {
      try {
        fs.unlinkSync(path.join(lockPath, "owner"));
        fs.rmdirSync(lockPath);
      } catch {
      }
    }
  }
}
function getBackendSessionToken() {
  if (cachedToken) return cachedToken;
  const configured = String(
    define_process_env_default$1.GPTBRIDGE_IPC_SESSION_TOKEN || ""
  ).trim().toLowerCase();
  if (TOKEN_PATTERN.test(configured)) {
    cachedToken = configured;
    return cachedToken;
  }
  const filePath = tokenFilePath();
  const existing = readToken(filePath);
  if (existing) {
    hardenPrivatePath(path.dirname(filePath), { directory: true });
    hardenPrivatePath(filePath);
    cachedToken = existing;
    return cachedToken;
  }
  cachedToken = repairOrCreateToken(filePath);
  return cachedToken;
}
function getWorkspaceInstanceId() {
  let normalized = path.resolve(getRuntimePathLibrary().workspaceRoot).replace(/\\/g, "/");
  if (process.platform === "win32") normalized = normalized.toLowerCase();
  return crypto.createHash("sha256").update(normalized, "utf-8").digest("hex").slice(0, 24);
}
function getBackendSessionDescriptor() {
  const token = getBackendSessionToken();
  const workspaceInstanceId = getWorkspaceInstanceId();
  return {
    token,
    workspaceInstanceId,
    websocketUrl: `ws://127.0.0.1:8765/?token=${encodeURIComponent(token)}&instance=${encodeURIComponent(workspaceInstanceId)}`
  };
}
function getRuntimeEnv(name) {
  return process["env"][name];
}
function getRuntimeEnvMap() {
  return process["env"];
}
const version = "1.0.0";
const packageMetadata = {
  version
};
const bundledVersion = String(packageMetadata.version).trim();
const LOCKED_PACKAGE_VERSION = "1.0.0";
if (bundledVersion !== LOCKED_PACKAGE_VERSION) {
  throw new Error(
    `GPTBridge package version is locked to ${LOCKED_PACKAGE_VERSION}; found ${bundledVersion || "missing"}`
  );
}
const PRODUCT_VERSION = bundledVersion;
PRODUCT_VERSION.split(".").slice(0, 2).join(".");
let pythonProcess = null;
let backendStatus = "idle";
let backendStartedAt = null;
let backendReadyAt = null;
let backendMessage = "backend idle";
let backendLastError = "";
const AUTO_RESTART_MAX_ATTEMPTS = 5;
const AUTO_RESTART_BASE_DELAY_MS = 2e3;
const AUTO_RESTART_MAX_DELAY_MS = 3e4;
let autoRestartAttempts = 0;
let autoRestartTimer = null;
let manualShutdown = false;
let shutdownToken = "";
function probeExistingBackend() {
  return new Promise((resolve) => {
    const request = http.get(
      { host: "127.0.0.1", port: 8765, path: "/health?brief=1", timeout: 8e3 },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
        response.on("end", () => {
          try {
            const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            resolve(
              payload.workspace_instance_id === getWorkspaceInstanceId() && payload.version === PRODUCT_VERSION && payload.backend_runtime_ready === true
            );
          } catch {
            resolve(false);
          }
        });
      }
    );
    request.on("timeout", () => request.destroy());
    request.on("error", () => resolve(false));
  });
}
function hasLiveSupervisor(paths) {
  const statePath = path.join(
    paths.workspaceRoot,
    "main-system",
    "runtime",
    "state",
    "boot-core.json"
  );
  try {
    const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
    const pid = Number(state.pid);
    if (pid <= 0 || state.status === "stopped") return false;
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}
function requestGracefulBackendShutdown() {
  if (!shutdownToken) return Promise.resolve(false);
  return new Promise((resolve) => {
    const request = http.request(
      {
        method: "GET",
        host: "127.0.0.1",
        port: 8765,
        path: "/shutdown",
        timeout: 1500,
        headers: {
          "X-GPTBridge-Shutdown-Token": shutdownToken,
          "X-GPTBridge-Shutdown-Reason": "hot-update"
        }
      },
      (response) => {
        response.resume();
        response.on("end", () => resolve(response.statusCode === 200));
      }
    );
    request.on("timeout", () => request.destroy());
    request.on("error", () => resolve(false));
    request.end();
  });
}
function getBackendStatus() {
  return backendStatus;
}
function getBackendRuntimeInfo() {
  return {
    status: backendStatus,
    ready: backendStatus === "running",
    startedAt: backendStartedAt,
    readyAt: backendReadyAt,
    startupMs: backendStartedAt && backendReadyAt ? backendReadyAt - backendStartedAt : null,
    message: backendMessage,
    lastError: backendLastError
  };
}
function spawnBootCore(paths, autoKillBackendPort = false) {
  backendMessage = "spawning boot_core (startup core)";
  backendLastError = "";
  console.log("[Python Backend Manager] Spawning boot_core...");
  try {
    const backendArgs = ["-u", paths.bootCoreEntry, "--serve"];
    if (autoKillBackendPort) backendArgs.push("--auto-kill-backend-port");
    const runtimeEnvironment = getRuntimeEnvMap();
    shutdownToken = crypto.randomBytes(32).toString("hex");
    pythonProcess = child_process.spawn(
      paths.pythonExecutable,
      backendArgs,
      {
        cwd: paths.workspaceRoot,
        env: {
          ...runtimeEnvironment,
          GPTBRIDGE_PROJECT_ROOT: paths.workspaceRoot,
          GPTBRIDGE_APP_VERSION: PRODUCT_VERSION,
          GPTBRIDGE_IPC_STATE_ROOT: getIpcStateRoot(),
          GPTBRIDGE_IPC_SESSION_TOKEN: getBackendSessionToken(),
          GPTBRIDGE_SHUTDOWN_TOKEN: shutdownToken
        },
        detached: true,
        stdio: "ignore",
        windowsHide: true
      }
    );
    pythonProcess.on("exit", (code, signal) => {
      console.log(
        `[Python Backend Manager] boot_core exited with code ${code} and signal ${signal}`
      );
      pythonProcess = null;
      if (signal === "SIGTERM" || code === 0 || manualShutdown) {
        backendStatus = "idle";
        backendMessage = "backend stopped";
        autoRestartAttempts = 0;
        manualShutdown = false;
        return;
      }
      backendStatus = "error";
      backendMessage = `boot_core exited unexpectedly (code ${code}), auto-restarting...`;
      scheduleAutoRestart();
    });
    pythonProcess.on("error", (err) => {
      console.error("[Python Backend Manager] Failed to spawn boot_core:", err);
      pythonProcess = null;
      backendStatus = "error";
      backendMessage = `boot_core spawn failed: ${err.message}`;
      scheduleAutoRestart();
    });
    backendStatus = "running";
    backendReadyAt = Date.now();
    backendMessage = "boot_core supervising backend";
    autoRestartAttempts = 0;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("[Python Backend Manager] Failed to spawn boot_core:", err);
    pythonProcess = null;
    backendStatus = "error";
    backendMessage = `boot_core spawn failed: ${message}`;
    scheduleAutoRestart();
  }
}
function scheduleAutoRestart() {
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer);
    autoRestartTimer = null;
  }
  if (autoRestartAttempts >= AUTO_RESTART_MAX_ATTEMPTS) {
    backendMessage = `boot_core auto-restart exhausted (${AUTO_RESTART_MAX_ATTEMPTS} attempts), giving up`;
    console.error(`[Python Backend Manager] ${backendMessage}`);
    return;
  }
  autoRestartAttempts++;
  const delay = Math.min(
    AUTO_RESTART_MAX_DELAY_MS,
    AUTO_RESTART_BASE_DELAY_MS * Math.pow(2, autoRestartAttempts - 1)
  );
  console.log(
    `[Python Backend Manager] Auto-restart attempt ${autoRestartAttempts}/${AUTO_RESTART_MAX_ATTEMPTS} in ${delay}ms`
  );
  autoRestartTimer = setTimeout(() => {
    autoRestartTimer = null;
    if (manualShutdown) return;
    console.log("[Python Backend Manager] Auto-restarting boot_core...");
    void startBackend();
  }, delay);
}
async function startBackend(forceReplacement = false) {
  if (backendStatus === "running") {
    console.warn("[Python Backend Manager] boot_core already running.");
    return;
  }
  if (pythonProcess) {
    console.warn("[Python Backend Manager] boot_core process already exists.");
    return;
  }
  const paths = getRuntimePathLibrary();
  if (!forceReplacement && await probeExistingBackend() && hasLiveSupervisor(paths)) {
    backendStatus = "running";
    backendReadyAt = Date.now();
    backendMessage = "attached to existing governed backend";
    return;
  }
  if (!fs.existsSync(paths.pythonExecutable)) {
    backendStatus = "error";
    backendMessage = `Python executable not found: ${paths.pythonExecutable}`;
    console.error(
      `[Python Backend Manager] Python executable not found: ${paths.pythonExecutable}`
    );
    return;
  }
  if (!fs.existsSync(paths.bootCoreEntry)) {
    backendStatus = "error";
    backendMessage = `boot_core entry not found: ${paths.bootCoreEntry}`;
    console.error(
      `[Python Backend Manager] boot_core entry not found: ${paths.bootCoreEntry}`
    );
    return;
  }
  if (!fs.existsSync(paths.pythonEntry)) {
    backendStatus = "error";
    backendMessage = `Python entry not found: ${paths.pythonEntry}`;
    console.error(
      `[Python Backend Manager] Python entry not found: ${paths.pythonEntry}`
    );
    return;
  }
  backendStatus = "starting";
  backendStartedAt = Date.now();
  backendReadyAt = null;
  backendMessage = "spawning boot_core";
  spawnBootCore(paths, true);
}
async function ensureBackendStarted() {
  if (backendStatus === "error") {
    backendStatus = "idle";
  }
  if (!pythonProcess && backendStatus !== "running" && backendStatus !== "starting") {
    await startBackend();
  }
  return backendStatus;
}
async function stopBackend() {
  manualShutdown = true;
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer);
    autoRestartTimer = null;
  }
  if (!pythonProcess) {
    if (backendStatus === "starting") {
      backendStatus = "idle";
      backendMessage = "backend start cancelled";
    }
    return;
  }
  backendStatus = "stopping";
  backendMessage = "stopping boot_core";
  console.log("[Python Backend Manager] Stopping boot_core...");
  const processToStop = pythonProcess;
  const processId = processToStop.pid;
  await requestGracefulBackendShutdown();
  const exitedGracefully = await new Promise((resolve) => {
    if (processToStop.exitCode !== null || processToStop.signalCode !== null) {
      resolve(true);
      return;
    }
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    processToStop.once("exit", () => finish(true));
    const timer = setTimeout(() => finish(false), 1500);
  });
  if (!exitedGracefully) {
    if (process.platform === "win32" && processId) {
      await new Promise((resolve) => {
        const terminator = child_process.spawn(
          "taskkill.exe",
          ["/PID", String(processId), "/F"],
          { windowsHide: true, stdio: "ignore" }
        );
        terminator.once("exit", () => resolve());
        terminator.once("error", () => resolve());
      });
      try {
        const paths = getRuntimePathLibrary();
        const statePath = path.join(
          paths.workspaceRoot,
          "main-system",
          "runtime",
          "state",
          "boot-core.json"
        );
        if (fs.existsSync(statePath)) {
          const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
          const backendPid = Number(state.backend_pid);
          if (backendPid > 0 && backendPid !== processId) {
            await new Promise((resolve) => {
              const terminator2 = child_process.spawn(
                "taskkill.exe",
                ["/PID", String(backendPid), "/F"],
                { windowsHide: true, stdio: "ignore" }
              );
              terminator2.once("exit", () => resolve());
              terminator2.once("error", () => resolve());
            });
          }
        }
      } catch {
      }
    } else {
      processToStop.kill("SIGTERM");
    }
  }
  if (pythonProcess === processToStop) pythonProcess = null;
  shutdownToken = "";
  backendStatus = "idle";
  backendMessage = exitedGracefully ? "backend stopped gracefully" : "boot_core process tree stopped after graceful timeout";
}
async function restartBackend() {
  manualShutdown = false;
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer);
    autoRestartTimer = null;
  }
  if (pythonProcess) {
    await stopBackend();
  }
  backendStatus = "idle";
  manualShutdown = false;
  await startBackend(true);
  return getBackendStatus();
}
const sessions = /* @__PURE__ */ new Map();
let mainWindowRef = null;
function registerEmbeddedBrowser(mainWindow2) {
  mainWindowRef = mainWindow2;
}
function createSession(id, ownerModule, url, bounds) {
  if (!mainWindowRef || mainWindowRef.isDestroyed()) {
    return { ok: false, id, url, message: "MAIN_WINDOW_NOT_AVAILABLE" };
  }
  const existing = sessions.get(id);
  if (existing) {
    existing.view.webContents.loadURL(url).catch(() => {
    });
    existing.url = url;
    return { ok: true, id, url };
  }
  const view = new electron.BrowserView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  mainWindowRef.addBrowserView(view);
  const contentBounds = mainWindowRef.getContentBounds();
  const viewBounds = bounds ?? {
    x: 0,
    y: 35,
    width: contentBounds.width,
    height: contentBounds.height - 35
  };
  view.setBounds(viewBounds);
  view.webContents.loadURL(url).catch(() => {
  });
  sessions.set(id, {
    id,
    view,
    ownerModule,
    url,
    createdAt: Date.now()
  });
  return { ok: true, id, url };
}
function navigateSession(id, url) {
  const session = sessions.get(id);
  if (!session) {
    return { ok: false, message: "SESSION_NOT_FOUND" };
  }
  session.url = url;
  session.view.webContents.loadURL(url).catch(() => {
  });
  return { ok: true };
}
function getSessionUrl(id) {
  const session = sessions.get(id);
  if (!session) {
    return null;
  }
  return session.view.webContents.getURL() || session.url;
}
async function executeScript(id, script) {
  const session = sessions.get(id);
  if (!session) {
    return { ok: false, message: "SESSION_NOT_FOUND" };
  }
  try {
    const result = await session.view.webContents.executeJavaScript(script);
    return { ok: true, result };
  } catch (error) {
    return { ok: false, message: String(error) };
  }
}
function resizeSession(id, bounds) {
  const session = sessions.get(id);
  if (!session) {
    return { ok: false, message: "SESSION_NOT_FOUND" };
  }
  session.view.setBounds(bounds);
  return { ok: true };
}
function showSession(id) {
  const session = sessions.get(id);
  if (!session) {
    return { ok: false, message: "SESSION_NOT_FOUND" };
  }
  if (mainWindowRef && !mainWindowRef.isDestroyed()) {
    mainWindowRef.setTopBrowserView(session.view);
    session.view.webContents.focus();
  }
  return { ok: true };
}
function hideSession(id) {
  const session = sessions.get(id);
  if (!session) {
    return { ok: false, message: "SESSION_NOT_FOUND" };
  }
  if (mainWindowRef && !mainWindowRef.isDestroyed()) {
    mainWindowRef.removeBrowserView(session.view);
  }
  return { ok: true };
}
function closeSession(id) {
  const session = sessions.get(id);
  if (!session) {
    return { ok: false, message: "SESSION_NOT_FOUND" };
  }
  if (mainWindowRef && !mainWindowRef.isDestroyed()) {
    mainWindowRef.removeBrowserView(session.view);
  }
  session.view.webContents.destroy?.();
  sessions.delete(id);
  return { ok: true };
}
function listSessions() {
  return Array.from(sessions.values()).map((s) => ({
    id: s.id,
    ownerModule: s.ownerModule,
    url: s.url,
    createdAt: s.createdAt
  }));
}
function closeModuleSessions(ownerModule) {
  let count = 0;
  for (const [id, session] of sessions) {
    if (session.ownerModule === ownerModule) {
      closeSession(id);
      count++;
    }
  }
  return count;
}
function closeAllSessions() {
  for (const id of sessions.keys()) {
    closeSession(id);
  }
}
function registerEmbeddedBrowserIpc() {
  electron.ipcMain.handle("embedded-browser:create", (_event, args) => {
    return createSession(args.id, args.ownerModule, args.url, args.bounds);
  });
  electron.ipcMain.handle("embedded-browser:navigate", (_event, args) => {
    return navigateSession(args.id, args.url);
  });
  electron.ipcMain.handle("embedded-browser:execute", async (_event, args) => {
    return executeScript(args.id, args.script);
  });
  electron.ipcMain.handle("embedded-browser:show", (_event, args) => {
    return showSession(args.id);
  });
  electron.ipcMain.handle("embedded-browser:hide", (_event, args) => {
    return hideSession(args.id);
  });
  electron.ipcMain.handle("embedded-browser:close", (_event, args) => {
    return closeSession(args.id);
  });
  electron.ipcMain.handle("embedded-browser:resize", (_event, args) => {
    return resizeSession(args.id, args.bounds);
  });
  electron.ipcMain.handle("embedded-browser:list", () => {
    return listSessions();
  });
  electron.ipcMain.handle("embedded-browser:url", (_event, args) => {
    const url = getSessionUrl(args.id);
    return { ok: url !== null, url };
  });
  electron.ipcMain.handle("embedded-browser:close-module", (_event, args) => {
    return { closed: closeModuleSessions(args.ownerModule) };
  });
}
const CACHE_TTL_MS = 3e4;
const YIELD_EVERY_ENTRIES = 256;
const MAIN_SYSTEM_DEPENDENCY_DIRECTORIES = /* @__PURE__ */ new Set([".venv", "node_modules"]);
const TOOL_RUNTIME_ROOTS = /* @__PURE__ */ new Set([
  ".venv",
  "build",
  "dist",
  "env",
  "node_modules",
  "release",
  "venv"
]);
const TOOL_CACHE_SEGMENTS = /* @__PURE__ */ new Set([
  ".cache",
  ".pytest_cache",
  ".ruff_cache",
  "__pycache__",
  "browser-profile",
  "browser-profiles",
  "cache",
  "caches",
  "code cache",
  "edge-profile",
  "electron-user-data",
  "gpu cache",
  "temp",
  "tmp"
]);
const TOOL_USER_DATA_RUNTIME_ROOTS = /* @__PURE__ */ new Set([
  "data",
  "recovery",
  "settings",
  "state"
]);
let cachedInventory = null;
let inventoryPromise = null;
let cachedMainSystemSize = null;
let mainSystemSizePromise = null;
let cachedWorkspaceSize = null;
let workspaceSizePromise = null;
let cachedSharedLayerSize = null;
let sharedLayerSizePromise = null;
function isPathInside$1(basePath, targetPath) {
  const relative = path.relative(basePath, targetPath);
  return relative === "" || !!relative && !relative.startsWith("..") && !path.isAbsolute(relative);
}
async function yieldToEventLoop() {
  await new Promise((resolve) => setImmediate(resolve));
}
function emptyToolSizeBreakdown() {
  return {
    program: { size_bytes: 0, file_count: 0 },
    runtime: { size_bytes: 0, file_count: 0 },
    user_data: { size_bytes: 0, file_count: 0 },
    cache: { size_bytes: 0, file_count: 0 },
    backups: { size_bytes: 0, file_count: 0 }
  };
}
function classifyToolFile(relativePath) {
  const segments = relativePath.split(/[\\/]+/).filter(Boolean).map((segment) => segment.toLowerCase());
  const fileName = segments.at(-1) ?? "";
  const root = segments[0] ?? "";
  const runtimeSection = root === "runtime" ? segments[1] ?? "" : "";
  if (segments.some((segment) => segment === "backup" || segment === "backups") || segments.some((segment) => segment.endsWith("_backups")) || /\.(?:bak|backup)$/.test(fileName)) {
    return "backups";
  }
  if (segments.some((segment) => TOOL_CACHE_SEGMENTS.has(segment))) {
    return "cache";
  }
  if (root === "data" || root === "runtime" && (TOOL_USER_DATA_RUNTIME_ROOTS.has(runtimeSection) || runtimeSection.startsWith("test-self-training"))) {
    return "user_data";
  }
  if (TOOL_RUNTIME_ROOTS.has(root) || root === "runtime") {
    return "runtime";
  }
  return "program";
}
async function folderSize(folderPath, options = {}) {
  const pending = [folderPath];
  const resolvedRoot = path.resolve(folderPath);
  let bytes = 0;
  let fileCount = 0;
  let visitedEntries = 0;
  const breakdown = options.includeToolBreakdown ? emptyToolSizeBreakdown() : void 0;
  while (pending.length > 0) {
    const current = pending.pop();
    if (!current) continue;
    let entries;
    try {
      entries = await fs.promises.readdir(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      visitedEntries += 1;
      if (visitedEntries % YIELD_EVERY_ENTRIES === 0) {
        await yieldToEventLoop();
      }
      if (entry.isSymbolicLink()) continue;
      const entryPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        if (path.resolve(current) === resolvedRoot && options.excludedRootDirectories?.has(entry.name)) {
          continue;
        }
        pending.push(entryPath);
        continue;
      }
      if (!entry.isFile()) continue;
      try {
        const stat = await fs.promises.lstat(entryPath);
        if (!stat.isFile() || stat.isSymbolicLink()) continue;
        bytes += stat.size;
        fileCount += 1;
        if (breakdown) {
          const category = classifyToolFile(path.relative(resolvedRoot, entryPath));
          breakdown[category].size_bytes += stat.size;
          breakdown[category].file_count += 1;
        }
      } catch {
      }
    }
  }
  return {
    bytes: Math.max(0, Math.min(bytes, Number.MAX_SAFE_INTEGER)),
    fileCount,
    breakdown
  };
}
async function buildInventory(workspaceRoot) {
  const resolvedRoot = path.resolve(workspaceRoot);
  let entries;
  try {
    entries = await fs.promises.readdir(resolvedRoot, { withFileTypes: true });
  } catch {
    return [];
  }
  const tools = [];
  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
    const folderPath = path.resolve(resolvedRoot, entry.name);
    if (!isPathInside$1(resolvedRoot, folderPath)) continue;
    const manifestPath = path.join(folderPath, "manifest.json");
    let manifest;
    try {
      manifest = JSON.parse(
        await fs.promises.readFile(manifestPath, "utf-8")
      );
    } catch {
      continue;
    }
    const id = String(manifest.id ?? "").trim();
    if (!/^[a-z0-9_-]+$/.test(id) || id !== entry.name) continue;
    const runtime = manifest.runtime && typeof manifest.runtime === "object" ? manifest.runtime : {};
    const rawEntry = String(runtime.entry ?? manifest.entry ?? "").trim();
    const codePath = rawEntry ? path.resolve(folderPath, rawEntry) : folderPath;
    const safeCodePath = isPathInside$1(folderPath, codePath) ? codePath : folderPath;
    const size = await folderSize(folderPath, { includeToolBreakdown: true });
    tools.push({
      id,
      folder_path: folderPath,
      manifest_path: manifestPath,
      code_path: safeCodePath,
      project_size_bytes: size.bytes,
      file_count: size.fileCount,
      size_breakdown: size.breakdown ?? emptyToolSizeBreakdown()
    });
  }
  return tools;
}
async function getPlatformToolSizes(workspaceRoot, forceRefresh = false) {
  const resolvedRoot = path.resolve(workspaceRoot);
  const now = Date.now();
  if (!forceRefresh && cachedInventory && cachedInventory.workspaceRoot === resolvedRoot && cachedInventory.expiresAt > now) {
    return cachedInventory.tools.map((tool) => ({ ...tool }));
  }
  if (inventoryPromise) return inventoryPromise;
  inventoryPromise = buildInventory(resolvedRoot);
  try {
    const tools = await inventoryPromise;
    cachedInventory = {
      workspaceRoot: resolvedRoot,
      expiresAt: Date.now() + CACHE_TTL_MS,
      tools
    };
    return tools.map((tool) => ({ ...tool }));
  } finally {
    inventoryPromise = null;
  }
}
async function getMainSystemSize(workspaceRoot, forceRefresh = false) {
  const resolvedRoot = path.resolve(workspaceRoot);
  const mainSystemRoot = path.resolve(resolvedRoot, "main-system");
  if (!isPathInside$1(resolvedRoot, mainSystemRoot)) {
    throw new Error("Main-system folder escaped workspace boundary");
  }
  const now = Date.now();
  if (!forceRefresh && cachedMainSystemSize && cachedMainSystemSize.workspaceRoot === resolvedRoot && cachedMainSystemSize.expiresAt > now) {
    return {
      folder_path: cachedMainSystemSize.folder_path,
      project_size_bytes: cachedMainSystemSize.project_size_bytes,
      file_count: cachedMainSystemSize.file_count,
      dependency_size_bytes: cachedMainSystemSize.dependency_size_bytes,
      dependency_file_count: cachedMainSystemSize.dependency_file_count,
      total_size_bytes: cachedMainSystemSize.total_size_bytes,
      total_file_count: cachedMainSystemSize.total_file_count
    };
  }
  if (mainSystemSizePromise) return mainSystemSizePromise;
  mainSystemSizePromise = (async () => {
    const size = await folderSize(mainSystemRoot, {
      excludedRootDirectories: MAIN_SYSTEM_DEPENDENCY_DIRECTORIES
    });
    let dependencySizeBytes = 0;
    let dependencyFileCount = 0;
    for (const dependencyDirectory of MAIN_SYSTEM_DEPENDENCY_DIRECTORIES) {
      const dependencySize = await folderSize(
        path.join(mainSystemRoot, dependencyDirectory)
      );
      dependencySizeBytes += dependencySize.bytes;
      dependencyFileCount += dependencySize.fileCount;
    }
    const result = {
      folder_path: mainSystemRoot,
      project_size_bytes: size.bytes,
      file_count: size.fileCount,
      dependency_size_bytes: dependencySizeBytes,
      dependency_file_count: dependencyFileCount,
      total_size_bytes: size.bytes + dependencySizeBytes,
      total_file_count: size.fileCount + dependencyFileCount
    };
    cachedMainSystemSize = {
      ...result,
      workspaceRoot: resolvedRoot,
      expiresAt: Date.now() + CACHE_TTL_MS
    };
    return result;
  })();
  try {
    return await mainSystemSizePromise;
  } finally {
    mainSystemSizePromise = null;
  }
}
async function getSharedLayerSize(workspaceRoot, forceRefresh = false) {
  const resolvedRoot = path.resolve(workspaceRoot);
  const sharedLayerRoot = path.resolve(resolvedRoot, "shared-layer");
  if (!isPathInside$1(resolvedRoot, sharedLayerRoot)) {
    throw new Error("Shared-layer folder escaped workspace boundary");
  }
  const now = Date.now();
  if (!forceRefresh && cachedSharedLayerSize && cachedSharedLayerSize.workspaceRoot === resolvedRoot && cachedSharedLayerSize.expiresAt > now) {
    return {
      folder_path: cachedSharedLayerSize.folder_path,
      project_size_bytes: cachedSharedLayerSize.project_size_bytes,
      file_count: cachedSharedLayerSize.file_count
    };
  }
  if (sharedLayerSizePromise) return sharedLayerSizePromise;
  sharedLayerSizePromise = (async () => {
    const size = await folderSize(sharedLayerRoot);
    const result = {
      folder_path: sharedLayerRoot,
      project_size_bytes: size.bytes,
      file_count: size.fileCount
    };
    cachedSharedLayerSize = {
      ...result,
      workspaceRoot: resolvedRoot,
      expiresAt: Date.now() + CACHE_TTL_MS
    };
    return result;
  })();
  try {
    return await sharedLayerSizePromise;
  } finally {
    sharedLayerSizePromise = null;
  }
}
async function getWorkspaceSize(workspaceRoot, tools, mainSystem, sharedLayer, forceRefresh = false) {
  const resolvedRoot = path.resolve(workspaceRoot);
  const now = Date.now();
  if (!forceRefresh && cachedWorkspaceSize && cachedWorkspaceSize.workspaceRoot === resolvedRoot && cachedWorkspaceSize.expiresAt > now) {
    return {
      folder_path: cachedWorkspaceSize.folder_path,
      project_size_bytes: cachedWorkspaceSize.project_size_bytes,
      file_count: cachedWorkspaceSize.file_count
    };
  }
  if (workspaceSizePromise) return workspaceSizePromise;
  workspaceSizePromise = (async () => {
    const measuredRoots = /* @__PURE__ */ new Map();
    measuredRoots.set(path.resolve(mainSystem.folder_path), {
      bytes: mainSystem.total_size_bytes,
      fileCount: mainSystem.total_file_count
    });
    measuredRoots.set(path.resolve(sharedLayer.folder_path), {
      bytes: sharedLayer.project_size_bytes,
      fileCount: sharedLayer.file_count
    });
    for (const tool of tools) {
      measuredRoots.set(path.resolve(tool.folder_path), {
        bytes: tool.project_size_bytes,
        fileCount: tool.file_count
      });
    }
    let bytes = 0;
    let fileCount = 0;
    let entries = [];
    try {
      entries = await fs.promises.readdir(resolvedRoot, { withFileTypes: true });
    } catch {
      throw new Error("Workspace folder cannot be read");
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const entryPath = path.resolve(resolvedRoot, entry.name);
      if (!isPathInside$1(resolvedRoot, entryPath)) continue;
      if (entry.isDirectory()) {
        const known = measuredRoots.get(entryPath);
        const size = known ?? await folderSize(entryPath);
        bytes += size.bytes;
        fileCount += size.fileCount;
      } else if (entry.isFile()) {
        try {
          const stat = await fs.promises.lstat(entryPath);
          if (!stat.isSymbolicLink() && stat.isFile()) {
            bytes += stat.size;
            fileCount += 1;
          }
        } catch {
        }
      }
    }
    if (fileCount <= 0 || bytes <= 0) {
      throw new Error("Workspace size inventory is empty");
    }
    const result = {
      folder_path: resolvedRoot,
      project_size_bytes: Math.min(bytes, Number.MAX_SAFE_INTEGER),
      file_count: fileCount
    };
    cachedWorkspaceSize = {
      ...result,
      workspaceRoot: resolvedRoot,
      expiresAt: Date.now() + CACHE_TTL_MS
    };
    return result;
  })();
  try {
    return await workspaceSizePromise;
  } finally {
    workspaceSizePromise = null;
  }
}
var define_process_env_default = {};
let mainWindow = null;
let lastCpuSnapshot = null;
let currentUiZoom = 1;
let mainRendererReloadTimer = null;
const sourceProduction = !electron.app.isPackaged;
if (sourceProduction) {
  electron.app.setName("GPTBridge");
  electron.app.setPath(
    "userData",
    path.join(electron.app.getPath("appData"), "gptbridge-auto-agent-ide")
  );
}
const shouldManageBackend = getRuntimeEnv("GPTBRIDGE_MANAGE_BACKEND") === "1";
const MIN_UI_ZOOM = 0.85;
const MAX_UI_ZOOM = 1.3;
const adaptiveZoomController = new AdaptiveZoomController(() => currentUiZoom);
const isPathInside = (basePath, targetPath) => {
  const relative = path.relative(basePath, targetPath);
  return relative === "" || !!relative && !relative.startsWith("..") && !path.isAbsolute(relative);
};
function clampUiZoom(value) {
  return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value));
}
function reportRuntimeEvent(event, payload = {}) {
  console.info(`[Main System] ${event}`, payload);
}
process.on("uncaughtException", (error) => {
  reportRuntimeEvent("main.uncaughtException", {
    message: error.message,
    stack: error.stack
  });
});
process.on("unhandledRejection", (reason) => {
  reportRuntimeEvent("main.unhandledRejection", {
    message: reason instanceof Error ? reason.message : String(reason),
    stack: reason instanceof Error ? reason.stack : void 0
  });
});
function readCpuSnapshot() {
  let idle = 0;
  let total = 0;
  for (const cpu of os.cpus()) {
    idle += cpu.times.idle;
    total += cpu.times.user + cpu.times.nice + cpu.times.sys + cpu.times.irq + cpu.times.idle;
  }
  return { idle, total };
}
function readCpuUsagePercent() {
  const current = readCpuSnapshot();
  if (!lastCpuSnapshot) {
    lastCpuSnapshot = current;
    return null;
  }
  const totalDiff = current.total - lastCpuSnapshot.total;
  const idleDiff = current.idle - lastCpuSnapshot.idle;
  lastCpuSnapshot = current;
  if (totalDiff <= 0) return null;
  const usage = (1 - idleDiff / totalDiff) * 100;
  return Math.max(0, Math.min(100, usage));
}
function readDiskMetrics(rootPath) {
  try {
    const stats = fs.statfsSync(rootPath);
    const blockSize = Number(stats.bsize ?? 0);
    const totalBlocks = Number(stats.blocks ?? 0);
    const freeBlocks = Number(
      stats.bavail ?? stats.bfree ?? 0
    );
    if (blockSize <= 0 || totalBlocks <= 0) return null;
    const totalBytes = blockSize * totalBlocks;
    const freeBytes = blockSize * freeBlocks;
    const usagePercent = (totalBytes - freeBytes) / totalBytes * 100;
    return {
      totalBytes,
      freeBytes,
      usagePercent: Math.max(0, Math.min(100, usagePercent))
    };
  } catch {
    return null;
  }
}
function resolveSystemDiskRoot() {
  if (process.platform !== "win32") return path.parse(os.homedir()).root || "/";
  const configuredDrive = (getRuntimeEnv("SystemDrive") || "").trim();
  if (/^[a-z]:$/i.test(configuredDrive)) return `${configuredDrive}\\`;
  if (configuredDrive) return path.parse(path.resolve(configuredDrive)).root;
  return path.parse(os.homedir()).root || path.parse(process.cwd()).root;
}
function getSystemMetrics() {
  const totalMemBytes = os.totalmem();
  const freeMemBytes = os.freemem();
  const ramUsagePercent = totalMemBytes > 0 ? (totalMemBytes - freeMemBytes) / totalMemBytes * 100 : 0;
  const diskRoot = resolveSystemDiskRoot();
  const disk = readDiskMetrics(diskRoot);
  return {
    cpuUsagePercent: readCpuUsagePercent(),
    ramUsagePercent: Math.max(0, Math.min(100, ramUsagePercent)),
    ramTotalBytes: totalMemBytes,
    ramFreeBytes: freeMemBytes,
    diskUsagePercent: disk?.usagePercent ?? null,
    diskTotalBytes: disk?.totalBytes ?? null,
    diskFreeBytes: disk?.freeBytes ?? null,
    diskRoot,
    sampledAt: Date.now()
  };
}
async function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.focus();
    return;
  }
  const paths = getRuntimePathLibrary();
  reportRuntimeEvent("window.create.start", {
    isPackaged: electron.app.isPackaged,
    sourceProduction,
    shouldManageBackend,
    workspaceRoot: paths.workspaceRoot,
    resourcesRoot: paths.resourcesRoot,
    rendererEntryHtml: paths.rendererEntryHtml,
    pythonExecutable: paths.pythonExecutable,
    pythonEntry: paths.pythonEntry
  });
  mainWindow = new electron.BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1100,
    minHeight: 720,
    show: false,
    backgroundColor: "#1a1b1e",
    titleBarStyle: "hidden",
    titleBarOverlay: process.platform === "win32" ? {
      color: "#1a1b1e",
      symbolColor: "#ffffff",
      height: 35
    } : false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: paths.preloadEntry
    }
  });
  adaptiveZoomController.register(mainWindow);
  registerEmbeddedBrowser(mainWindow);
  mainWindow.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown") return;
    const key = input.key.toLowerCase();
    const reloadShortcut = key === "f5" || (input.control || input.meta) && key === "r";
    if (!reloadShortcut) return;
    event.preventDefault();
    if (input.shift) {
      mainWindow?.webContents.reloadIgnoringCache();
    } else {
      mainWindow?.webContents.reload();
    }
  });
  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
    mainWindow?.focus();
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  mainWindow.webContents.on("did-fail-load", (_event, code, desc) => {
    console.error("[BOOT] Renderer load failed:", code, desc);
    reportRuntimeEvent("window.renderer.did-fail-load", { code, desc });
  });
  const devServerUrl = getRuntimeEnv("GPTBRIDGE_RENDERER_DEV_URL");
  if (devServerUrl) {
    await mainWindow.loadURL(devServerUrl);
    mainWindow.webContents.openDevTools({ mode: "detach" });
    reportRuntimeEvent("window.load-url.ok", { devServerUrl });
  } else {
    await mainWindow.loadFile(paths.rendererEntryHtml);
    reportRuntimeEvent("window.load-file.ok", {
      rendererEntryHtml: paths.rendererEntryHtml
    });
  }
}
function startMainRendererWatch() {
  const paths = getRuntimePathLibrary();
  if (!paths.rendererEntryHtml || !fs.existsSync(paths.rendererEntryHtml)) return;
  fs.watchFile(paths.rendererEntryHtml, { interval: 500 }, () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainRendererReloadTimer) {
      clearTimeout(mainRendererReloadTimer);
      mainRendererReloadTimer = null;
    }
    mainRendererReloadTimer = setTimeout(() => {
      if (!mainWindow || mainWindow.isDestroyed()) return;
      mainWindow.webContents.reloadIgnoringCache();
    }, 500);
  });
}
function stopMainRendererWatch() {
  if (mainRendererReloadTimer) {
    clearTimeout(mainRendererReloadTimer);
    mainRendererReloadTimer = null;
  }
  const paths = getRuntimePathLibrary();
  if (paths.rendererEntryHtml) {
    fs.unwatchFile(paths.rendererEntryHtml);
  }
}
function registerIpcHandlers() {
  registerEmbeddedBrowserIpc();
  electron.ipcMain.handle("app:get-status", async () => {
    const backendRuntime = getBackendRuntimeInfo();
    return {
      isPackaged: electron.app.isPackaged,
      version: PRODUCT_VERSION,
      backendStatus: backendRuntime.status,
      backendManaged: shouldManageBackend,
      backendReady: backendRuntime.ready,
      backendStartupMs: backendRuntime.startupMs,
      backendMessage: backendRuntime.message,
      environment: electron.app.isPackaged || sourceProduction ? "production" : "source",
      sourceProduction,
      systemReady: shouldManageBackend ? backendRuntime.ready : backendRuntime.status !== "error",
      bootTimestamp: Date.now(),
      systemMetrics: getSystemMetrics()
    };
  });
  electron.ipcMain.handle("app:ensure-backend-started", async () => {
    if (!shouldManageBackend) {
      return {
        ok: false,
        managed: false,
        backendStatus: getBackendStatus(),
        message: "backend manager is disabled by GPTBRIDGE_MANAGE_BACKEND=0"
      };
    }
    const backendStatus2 = await ensureBackendStarted();
    return {
      ok: backendStatus2 !== "error",
      managed: true,
      backendStatus: backendStatus2
    };
  });
  electron.ipcMain.handle("app:get-backend-session", async () => {
    return getBackendSessionDescriptor();
  });
  electron.ipcMain.handle("app:restart-backend", async () => {
    if (!shouldManageBackend) {
      return {
        ok: false,
        managed: false,
        backendStatus: getBackendStatus(),
        message: "後端目前由外部 dev 腳本管理，無法由 Electron 單獨重啟。"
      };
    }
    const backendStatus2 = await restartBackend();
    return {
      ok: backendStatus2 !== "error",
      managed: true,
      backendStatus: backendStatus2
    };
  });
  electron.ipcMain.handle("app:restart", async () => {
    electron.app.relaunch();
    electron.app.quit();
  });
  electron.ipcMain.handle("app:get-platform-tool-sizes", async (_event, payload) => {
    const forceRefresh = Boolean(
      payload && typeof payload === "object" && payload.forceRefresh === true
    );
    const paths = getRuntimePathLibrary();
    const tools = await getPlatformToolSizes(paths.workspaceRoot, forceRefresh);
    const mainSystem = await getMainSystemSize(paths.workspaceRoot, forceRefresh);
    const sharedLayer = await getSharedLayerSize(paths.workspaceRoot, forceRefresh);
    const workspace = await getWorkspaceSize(
      paths.workspaceRoot,
      tools,
      mainSystem,
      sharedLayer,
      forceRefresh
    );
    return {
      ok: true,
      tools,
      main_system: mainSystem,
      shared_layer: sharedLayer,
      workspace,
      source: "governed-local-folder-inventory"
    };
  });
  electron.ipcMain.handle("app:reload-window", async () => {
    if (!mainWindow || mainWindow.isDestroyed()) return { ok: false };
    mainWindow.reload();
    return { ok: true };
  });
  electron.ipcMain.handle("app:reload-window-hard", async () => {
    if (!mainWindow || mainWindow.isDestroyed()) return { ok: false };
    mainWindow.webContents.reloadIgnoringCache();
    return { ok: true };
  });
  electron.ipcMain.handle("app:get-ui-zoom", async () => {
    return { ok: true, factor: currentUiZoom };
  });
  electron.ipcMain.handle(
    "app:set-ui-zoom",
    async (_event, payload) => {
      if (electron.BrowserWindow.getAllWindows().every((window) => window.isDestroyed())) {
        return { ok: false, message: "Window not ready" };
      }
      const target = clampUiZoom(Number(payload?.factor ?? 1));
      if (Number.isNaN(target) || target <= 0) {
        return { ok: false, message: "Invalid zoom factor" };
      }
      currentUiZoom = target;
      adaptiveZoomController.applyAll();
      return { ok: true, factor: currentUiZoom };
    }
  );
  electron.ipcMain.handle(
    "app:open-path",
    async (_event, payload) => {
      const rawPath = String(payload?.path || "").trim();
      const basePath = String(payload?.basePath || "").trim();
      const relativePath = String(payload?.relativePath || "").trim();
      const mode = payload?.mode === "reveal" ? "reveal" : "open";
      const workspaceRoot = path.resolve(getRuntimePathLibrary().workspaceRoot);
      let targetPath = rawPath ? path.resolve(rawPath) : "";
      if (basePath && relativePath) {
        const resolvedBase = path.resolve(basePath);
        const resolvedTarget = path.resolve(resolvedBase, relativePath);
        if (!isPathInside(resolvedBase, resolvedTarget)) {
          return { ok: false, message: "Path is outside the selected folder" };
        }
        targetPath = resolvedTarget;
      }
      if (!targetPath) return { ok: false, message: "Missing path" };
      if (!isPathInside(workspaceRoot, targetPath)) {
        return { ok: false, message: "Path is outside the project workspace" };
      }
      if (!fs.existsSync(targetPath)) {
        return { ok: false, message: "File no longer exists" };
      }
      if (mode === "reveal") {
        electron.shell.showItemInFolder(targetPath);
        return { ok: true };
      }
      const errorMessage = await electron.shell.openPath(targetPath);
      return errorMessage ? { ok: false, message: errorMessage } : { ok: true };
    }
  );
  electron.ipcMain.handle("dialog:select-folder", async (event) => {
    const window = electron.BrowserWindow.fromWebContents(event.sender) ?? mainWindow ?? void 0;
    const result = window ? await electron.dialog.showOpenDialog(window, {
      properties: ["openDirectory", "createDirectory"]
    }) : await electron.dialog.showOpenDialog({
      properties: ["openDirectory", "createDirectory"]
    });
    if (result.canceled || result.filePaths.length === 0) return "";
    return result.filePaths[0];
  });
  electron.ipcMain.handle("dialog:create-file", async (event, defaultPath) => {
    const window = electron.BrowserWindow.fromWebContents(event.sender) ?? mainWindow ?? void 0;
    const options = {
      defaultPath,
      filters: [
        { name: "Code", extensions: ["py", "json", "md", "txt"] },
        { name: "All Files", extensions: ["*"] }
      ]
    };
    const result = window ? await electron.dialog.showSaveDialog(window, options) : await electron.dialog.showSaveDialog(options);
    if (result.canceled || !result.filePath) return "";
    return result.filePath;
  });
  electron.ipcMain.handle("dialog:open-file", async (event, defaultPath) => {
    const window = electron.BrowserWindow.fromWebContents(event.sender) ?? mainWindow ?? void 0;
    const options = {
      defaultPath,
      properties: ["openFile"],
      filters: [
        { name: "Structured Data", extensions: ["csv", "tsv", "json", "xlsx", "xls"] },
        { name: "Code", extensions: ["py", "json", "md", "txt"] },
        { name: "All Files", extensions: ["*"] }
      ]
    };
    const result = window ? await electron.dialog.showOpenDialog(window, options) : await electron.dialog.showOpenDialog(options);
    if (result.canceled || result.filePaths.length === 0) return "";
    return result.filePaths[0];
  });
}
const hasSingleInstanceLock = electron.app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  reportRuntimeEvent("main.single-instance.exiting");
  electron.app.exit(0);
} else {
  electron.app.on("second-instance", () => {
    if (!mainWindow || mainWindow.isDestroyed()) {
      void createWindow();
      return;
    }
    if (mainWindow.isMinimized()) mainWindow.restore();
    if (!mainWindow.isVisible()) mainWindow.show();
    mainWindow.focus();
  });
  electron.app.whenReady().then(async () => {
    try {
      reportRuntimeEvent("bootstrap.start", {
        isPackaged: electron.app.isPackaged,
        sourceProduction,
        shouldManageBackend,
        cwd: process.cwd(),
        userData: electron.app.getPath("userData")
      });
      registerIpcHandlers();
      await createWindow();
      startMainRendererWatch();
      reportRuntimeEvent("window.ready");
      if (shouldManageBackend) {
        try {
          void startBackend();
        } catch (error) {
          reportRuntimeEvent("backend.start.failed", {
            message: error instanceof Error ? error.message : String(error)
          });
        }
      }
      reportRuntimeEvent("bootstrap.ready");
      electron.app.on("activate", () => {
        if (electron.BrowserWindow.getAllWindows().length === 0) {
          void createWindow();
        }
      });
    } catch (error) {
      reportRuntimeEvent("bootstrap.failed", {
        message: error instanceof Error ? error.message : String(error),
        stack: error instanceof Error ? error.stack : void 0
      });
    }
  });
}
if (define_process_env_default.GPTBRIDGE_RENDERER_DEV_URL) {
  process.on("SIGUSR2", () => {
    reportRuntimeEvent("main.relaunch.requested");
    electron.app.relaunch();
    electron.app.exit(0);
  });
}
electron.app.on("window-all-closed", () => {
  closeAllSessions();
  stopMainRendererWatch();
  if (process.platform !== "darwin") {
    electron.app.quit();
  }
});
electron.app.on("before-quit", () => {
  closeAllSessions();
  stopMainRendererWatch();
  reportRuntimeEvent("main.ui-detached");
});
