import AxeBuilder from '@axe-core/playwright'
import { expect, test, type TestInfo } from '@playwright/test'
import { spawnSync } from 'node:child_process'
import { promises as fs, readFileSync } from 'node:fs'
import { get as httpGet } from 'node:http'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { isAbsolute, join, relative, resolve, sep } from 'node:path'
import {
  _electron as electron,
  type ConsoleMessage,
  type ElectronApplication,
  type Page,
} from 'playwright'

const ROOT = resolve(__dirname, '..', '..')
const TOOL_ID = 'ai-assistant'
const EXE_PATH = join(
  ROOT,
  'platform_tools',
  TOOL_ID,
  'dist',
  `${TOOL_ID}.exe`
)
const PACKAGE_METADATA_PATH = join(
  ROOT,
  'platform_tools',
  TOOL_ID,
  'dist',
  'resources',
  'app',
  '.gptbridge-package.json'
)
const BACKEND_PORT = (() => {
  try {
    const metadata = JSON.parse(readFileSync(PACKAGE_METADATA_PATH, 'utf8'))
    const port = Number(metadata?.backend_port)
    if (Number.isInteger(port) && port >= 1024 && port <= 65535) return port
  } catch {
    // Package verification below reports missing or malformed metadata precisely.
  }
  return 8765
})()
const MANIFEST_PATH = join(
  ROOT,
  'platform_tools',
  TOOL_ID,
  'manifest.json'
)
const SANDBOX_PREFIX = 'gptbridge-ai-assistant-visual-'
const TAB_KEYS = [
  'overview',
  'ledger',
  'lab',
  'allocation',
  'events',
  'operations',
  'journal',
] as const
const AXE_TAGS = [
  'wcag2a',
  'wcag2aa',
  'wcag21a',
  'wcag21aa',
  'wcag22aa',
]

type PortProbe = {
  available: boolean
  reason: string
}

type RuntimeLog = {
  source: 'main' | 'renderer' | 'pageerror' | 'requestfailed' | 'crash'
  level: string
  message: string
  url?: string
}

type BackendOwner = {
  tool_id?: unknown
  pid?: unknown
  project_root?: unknown
  package_digest?: unknown
  backend_port?: unknown
  shutdown_token?: unknown
  started_at?: unknown
}

type CleanupReport = {
  ownerFound: boolean
  ownerVerified: boolean
  shutdownRequested: boolean
  backendStopped: boolean
  sandboxRemoved: boolean
  messages: string[]
}

function environmentCopy(): Record<string, string> {
  const output: Record<string, string> = {}
  for (const [key, value] of Object.entries(process.env)) {
    if (typeof value === 'string') output[key] = value
  }
  return output
}

function isPathInside(parent: string, candidate: string): boolean {
  const normalizedParent = resolve(parent)
  const normalizedCandidate = resolve(candidate)
  const relation = relative(normalizedParent, normalizedCandidate)
  return (
    relation === '' ||
    (!relation.startsWith(`..${sep}`) &&
      relation !== '..' &&
      !isAbsolute(relation))
  )
}

function pathsMatch(first: string, second: string): boolean {
  const normalizedFirst = resolve(first)
  const normalizedSecond = resolve(second)
  return process.platform === 'win32'
    ? normalizedFirst.toLowerCase() === normalizedSecond.toLowerCase()
    : normalizedFirst === normalizedSecond
}

async function probePort(port: number): Promise<PortProbe> {
  return await new Promise((done) => {
    const server = createServer()
    server.unref()
    let settled = false
    const finish = (result: PortProbe) => {
      if (settled) return
      settled = true
      done(result)
    }
    server.once('error', (error: NodeJS.ErrnoException) => {
      finish({
        available: false,
        reason: `${error.code || 'PORT_ERROR'}: ${error.message}`,
      })
    })
    server.listen(
      {
        host: '127.0.0.1',
        port,
        exclusive: true,
      },
      () => {
        server.close((error) => {
          finish({
            available: !error,
            reason: error ? error.message : 'available',
          })
        })
      }
    )
  })
}

function describeWindowsPortOwner(port: number): string {
  if (process.platform !== 'win32') return `127.0.0.1:${port} is occupied`
  const script = [
    `$connection = Get-NetTCPConnection -LocalPort ${port} -State Listen`,
    'if ($connection) {',
    '  $ownerProcess = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue',
    '  [pscustomobject]@{',
    '    address = $connection.LocalAddress',
    '    port = $connection.LocalPort',
    '    pid = $connection.OwningProcess',
    "    process = if ($ownerProcess) { $ownerProcess.ProcessName } else { '' }",
    "    path = if ($ownerProcess) { $ownerProcess.Path } else { '' }",
    '  } | ConvertTo-Json -Compress',
    '}',
  ].join('\n')
  const result = spawnSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-Command', script],
    {
      cwd: ROOT,
      encoding: 'utf8',
      windowsHide: true,
      timeout: 10_000,
    }
  )
  const details = String(result.stdout || '').trim()
  return details || `127.0.0.1:${port} is occupied`
}

async function attachJson(
  testInfo: TestInfo,
  name: string,
  value: unknown
): Promise<void> {
  await testInfo.attach(name, {
    body: Buffer.from(`${JSON.stringify(value, null, 2)}\n`, 'utf8'),
    contentType: 'application/json',
  })
}

function verifyPackagedApplication(): {
  status: number | null
  stdout: string
  stderr: string
} {
  const python =
    process.platform === 'win32'
      ? join(ROOT, '.venv', 'Scripts', 'python.exe')
      : join(ROOT, '.venv', 'bin', 'python')
  const result = spawnSync(
    python,
    [
      join(ROOT, 'scripts', 'package_platform_tools.py'),
      '--verify',
      '--json',
      TOOL_ID,
    ],
    {
      cwd: ROOT,
      encoding: 'utf8',
      windowsHide: true,
      timeout: 120_000,
    }
  )
  return {
    status: result.status,
    stdout: String(result.stdout || ''),
    stderr: String(result.stderr || ''),
  }
}

async function createIsolatedEnvironment(): Promise<{
  sandboxRoot: string
  localAppData: string
  ipcStateRoot: string
  launchEnvironment: Record<string, string>
}> {
  const sandboxRoot = await fs.mkdtemp(join(tmpdir(), SANDBOX_PREFIX))
  const appData = join(sandboxRoot, 'AppData', 'Roaming')
  const localAppData = join(sandboxRoot, 'AppData', 'Local')
  const temporaryDirectory = join(sandboxRoot, 'Temp')
  const investmentDataRoot = join(sandboxRoot, 'InvestmentData')
  const ipcStateRoot = join(
    localAppData,
    'GPTBridge',
    'standalone',
    TOOL_ID,
    'runtime',
    'ipc'
  )
  const chromiumProfile = join(sandboxRoot, 'ChromiumProfile')
  await Promise.all(
    [
      appData,
      localAppData,
      temporaryDirectory,
      investmentDataRoot,
      ipcStateRoot,
      chromiumProfile,
    ].map((directory) => fs.mkdir(directory, { recursive: true }))
  )

  const launchEnvironment = environmentCopy()
  for (const key of [
    'ALPHAVANTAGE_API_KEY',
    'ELECTRON_RUN_AS_NODE',
    'NODE_OPTIONS',
    'GPTBRIDGE_ALLOW_EXTERNAL_PROJECT_ROOT',
    'GPTBRIDGE_LOCAL_LLM_MODEL',
    'GPTBRIDGE_LOCAL_LLM_NUM_CTX',
    'GPTBRIDGE_LOCAL_LLM_NUM_PREDICT',
    'GPTBRIDGE_LOCAL_LLM_PROFILE',
    'GPTBRIDGE_LOCAL_LLM_URL',
    'GPTBRIDGE_OPEN_DEVTOOLS',
    'GPTBRIDGE_IPC_PORT',
    'GPTBRIDGE_PROJECT_ROOT',
    'GPTBRIDGE_PYTHON',
  ]) {
    delete launchEnvironment[key]
  }
  Object.assign(launchEnvironment, {
    APPDATA: appData,
    LOCALAPPDATA: localAppData,
    GPTBRIDGE_VISUAL_SMOKE_APP_DATA_ROOT: appData,
    TEMP: temporaryDirectory,
    TMP: temporaryDirectory,
    GPTBRIDGE_AI_ASSISTANT_DATA_ROOT: investmentDataRoot,
    GPTBRIDGE_AI_ASSISTANT_PROFILE: 'visual-smoke',
    GPTBRIDGE_DISABLE_LOCAL_LLM: '1',
    GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ALLOW_LAN: '0',
    GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ENABLED: '0',
    GPTBRIDGE_IPC_STATE_ROOT: ipcStateRoot,
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONNOUSERSITE: '1',
  })
  return {
    sandboxRoot,
    localAppData,
    ipcStateRoot,
    launchEnvironment,
  }
}

function requestBackendShutdown(token: string): Promise<boolean> {
  return new Promise((done) => {
    let settled = false
    const finish = (result: boolean) => {
      if (settled) return
      settled = true
      done(result)
    }
    const request = httpGet(
      {
        host: '127.0.0.1',
        port: BACKEND_PORT,
        path: '/shutdown',
        timeout: 3_000,
        headers: {
          'X-GPTBridge-Shutdown-Token': token,
        },
      },
      (response) => {
        response.resume()
        response.once('end', () => finish(response.statusCode === 200))
      }
    )
    request.once('timeout', () => {
      request.destroy()
      finish(false)
    })
    request.once('error', () => finish(false))
  })
}

async function waitForPortRelease(timeoutMs = 15_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if ((await probePort(BACKEND_PORT)).available) return true
    await new Promise((done) => setTimeout(done, 250))
  }
  return false
}

async function cleanupOwnedRuntime(
  sandboxRoot: string,
  localAppData: string,
  ipcStateRoot: string,
  removeSandbox = true
): Promise<CleanupReport> {
  const report: CleanupReport = {
    ownerFound: false,
    ownerVerified: false,
    shutdownRequested: false,
    backendStopped: false,
    sandboxRemoved: false,
    messages: [],
  }
  const ownerPath = join(
    ipcStateRoot,
    `standalone-${TOOL_ID}-backend.json`
  )
  let owner: BackendOwner | null = null
  try {
    owner = JSON.parse(await fs.readFile(ownerPath, 'utf8')) as BackendOwner
    report.ownerFound = true
  } catch (error) {
    const port = await probePort(BACKEND_PORT)
    if (!port.available) {
      report.messages.push(
        `No sandbox owner descriptor exists while ${BACKEND_PORT} remains occupied`
      )
      return report
    }
    report.backendStopped = true
  }

  if (owner) {
    const projectRoot = String(owner.project_root || '')
    const expectedProjectRoot = join(
      localAppData,
      'GPTBridge',
      'standalone',
      TOOL_ID
    )
    const token = String(owner.shutdown_token || '')
    const packageDigest = String(owner.package_digest || '')
    const startedAt = Date.parse(String(owner.started_at || ''))
    const verified =
      owner.tool_id === TOOL_ID &&
      isPathInside(sandboxRoot, projectRoot) &&
      pathsMatch(projectRoot, expectedProjectRoot) &&
      /^[a-f0-9]{64}$/i.test(packageDigest) &&
      Number(owner.backend_port) === BACKEND_PORT &&
      token.length >= 32 &&
      Number.isFinite(startedAt)
    report.ownerVerified = verified
    if (!verified) {
      report.messages.push(
        'Sandbox owner descriptor did not pass identity and path verification; no process was stopped'
      )
      return report
    }
    report.shutdownRequested = await requestBackendShutdown(token)
    if (!report.shutdownRequested) {
      report.messages.push(
        'Verified test backend rejected or did not answer the authenticated shutdown request'
      )
      return report
    }
    report.backendStopped = await waitForPortRelease()
    if (!report.backendStopped) {
      report.messages.push(
        `Verified test backend did not release port ${BACKEND_PORT} before the cleanup timeout`
      )
      return report
    }
  }

  if (!removeSandbox) return report
  report.sandboxRemoved = await removeOwnedSandbox(sandboxRoot)
  if (!report.sandboxRemoved) {
    report.messages.push(
      `Refused to remove an unexpected sandbox path: ${resolve(sandboxRoot)}`
    )
  }
  return report
}

async function removeOwnedSandbox(sandboxRoot: string): Promise<boolean> {
  const safeTemporaryRoot = resolve(tmpdir())
  const resolvedSandbox = resolve(sandboxRoot)
  if (
    !isPathInside(safeTemporaryRoot, resolvedSandbox) ||
    !resolvedSandbox.startsWith(
      join(safeTemporaryRoot, SANDBOX_PREFIX)
    )
  ) {
    return false
  }
  await fs.rm(resolvedSandbox, { recursive: true, force: true })
  return true
}

function serializeConsoleMessage(
  source: 'main' | 'renderer',
  message: ConsoleMessage
): RuntimeLog {
  const location = message.location()
  return {
    source,
    level: message.type(),
    message: message.text(),
    url: location.url || undefined,
  }
}

function observePage(
  page: Page,
  logs: RuntimeLog[],
  observedPages: Set<Page>
): void {
  if (observedPages.has(page)) return
  observedPages.add(page)
  page.on('console', (message) => {
    logs.push(serializeConsoleMessage('renderer', message))
  })
  page.on('pageerror', (error) => {
    logs.push({
      source: 'pageerror',
      level: 'error',
      message: error.stack || error.message,
    })
  })
  page.on('requestfailed', (request) => {
    logs.push({
      source: 'requestfailed',
      level: 'error',
      message: `${request.method()} ${request.url()}: ${
        request.failure()?.errorText || 'request failed'
      }`,
      url: request.url(),
    })
  })
  page.on('crash', () => {
    logs.push({
      source: 'crash',
      level: 'error',
      message: 'Electron renderer process crashed',
    })
  })
}

async function attachScreenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
  selector?: string
): Promise<void> {
  const screenshotPath = testInfo.outputPath(`${name}.png`)
  if (selector) {
    await page.locator(selector).screenshot({
      path: screenshotPath,
      animations: 'disabled',
      caret: 'hide',
      scale: 'css',
    })
  } else {
    await page.screenshot({
      path: screenshotPath,
      animations: 'disabled',
      caret: 'hide',
      fullPage: true,
      scale: 'css',
    })
  }
  await testInfo.attach(name, {
    path: screenshotPath,
    contentType: 'image/png',
  })
}

test.describe('AI Assistant packaged Electron visual smoke', () => {
  test('launches in an isolated profile and passes UI, console and accessibility smoke', async ({
  }, testInfo) => {
    test.skip(
      process.platform !== 'win32',
      'The packaged AI Assistant smoke currently targets the Windows EXE.'
    )

    const port = await probePort(BACKEND_PORT)
    if (!port.available) {
      const owner = describeWindowsPortOwner(BACKEND_PORT)
      const diagnostic = {
        port: BACKEND_PORT,
        probe: port.reason,
        owner,
        safety:
          'Skipped without stopping or attaching to the existing process.',
      }
      await attachJson(testInfo, 'occupied-backend-port', diagnostic)
      test.skip(
        true,
        `Safety skip: port ${BACKEND_PORT} is already occupied (${owner}). Close the owning GPTBridge runtime and retry.`
      )
      return
    }

    await expect(
      fs.access(EXE_PATH).then(
        () => true,
        () => false
      ),
      `Packaged executable is missing: ${EXE_PATH}`
    ).resolves.toBe(true)

    const verification = verifyPackagedApplication()
    await attachJson(testInfo, 'package-verification', verification)
    expect(
      verification.status,
      `Package verification failed.\n${verification.stdout}\n${verification.stderr}`
    ).toBe(0)

    const manifest = JSON.parse(
      await fs.readFile(MANIFEST_PATH, 'utf8')
    ) as { version?: unknown }
    const expectedVersion = String(manifest.version || '')
    expect(expectedVersion).not.toBe('')

    let electronApplication: ElectronApplication | undefined
    let sandbox:
      | Awaited<ReturnType<typeof createIsolatedEnvironment>>
      | undefined
    let primaryFailure: unknown
    let cleanupFailure: Error | undefined
    const runtimeLogs: RuntimeLog[] = []
    const accessibilityResults: Record<string, unknown> = {}
    const observedPages = new Set<Page>()

    try {
      sandbox = await createIsolatedEnvironment()
      const chromiumProfile = join(sandbox.sandboxRoot, 'ChromiumProfile')
      electronApplication = await electron.launch({
        executablePath: EXE_PATH,
        cwd: resolve(EXE_PATH, '..'),
        env: sandbox.launchEnvironment,
        args: [
          `--user-data-dir=${chromiumProfile}`,
          '--force-device-scale-factor=1',
          '--no-first-run',
        ],
        artifactsDir: testInfo.outputPath('playwright-artifacts'),
        bypassCSP: true,
        colorScheme: 'dark',
        locale: 'zh-TW',
        timeout: 90_000,
      })
      electronApplication.on('console', (message) => {
        runtimeLogs.push(serializeConsoleMessage('main', message))
      })
      electronApplication.on('window', (window) => {
        observePage(window, runtimeLogs, observedPages)
      })
      for (const window of electronApplication.windows()) {
        observePage(window, runtimeLogs, observedPages)
      }

      const page = await electronApplication.firstWindow({ timeout: 90_000 })
      observePage(page, runtimeLogs, observedPages)
      page.setDefaultTimeout(20_000)
      await page.waitForLoadState('domcontentloaded')
      await electronApplication.evaluate(({ BrowserWindow }) => {
        const window = BrowserWindow.getAllWindows()[0]
        if (window && !window.isDestroyed()) {
          window.setSize(1440, 920)
          window.setOpacity(0)
          window.setSkipTaskbar(true)
          window.showInactive()
        }
      })

      const runtime = await electronApplication.evaluate(({ app }) => ({
        isPackaged: app.isPackaged,
        version: app.getVersion(),
        userData: app.getPath('userData'),
        processType: process.type,
      }))
      await attachJson(testInfo, 'electron-runtime', runtime)
      expect(runtime.isPackaged).toBe(true)
      expect(runtime.version).toBe(expectedVersion)
      expect(runtime.processType).toBe('browser')
      expect(isPathInside(sandbox.sandboxRoot, runtime.userData)).toBe(true)

      const rendererBoundary = await page.evaluate(() => {
        const globalWindow = globalThis as typeof globalThis & {
          electron?: { invoke?: unknown }
          gptBridge?: { standaloneTool?: unknown }
          process?: unknown
          require?: unknown
        }
        return {
          standalone: globalWindow.gptBridge?.standaloneTool === true,
          invokeType: typeof globalWindow.electron?.invoke,
          processType: typeof globalWindow.process,
          requireType: typeof globalWindow.require,
        }
      })
      await attachJson(testInfo, 'renderer-boundary', rendererBoundary)
      expect(rendererBoundary).toEqual({
        standalone: true,
        invokeType: 'function',
        processType: 'undefined',
        requireType: 'undefined',
      })

      await expect(
        page.getByRole('heading', { level: 1, name: 'AI投資管家' })
      ).toBeVisible()
      await expect(
        page
          .locator('.nexus-top-meta > span')
          .filter({ hasText: /^即時 ·/ })
      ).toHaveCount(1, { timeout: 90_000 })
      await attachScreenshot(page, testInfo, 'packaged-overview-full')

      const tabList = page.getByRole('tablist', {
        name: '分析工作台分頁',
      })
      await expect(tabList).toBeVisible()
      await expect(tabList.getByRole('tab')).toHaveCount(TAB_KEYS.length)

      const overviewTab = page.locator('#investment-tab-overview')
      await overviewTab.focus()
      await overviewTab.press('ArrowRight')
      await expect(page.locator('#investment-tab-ledger')).toHaveAttribute(
        'aria-selected',
        'true'
      )
      await page.locator('#investment-tab-ledger').press('End')
      await expect(page.locator('#investment-tab-journal')).toHaveAttribute(
        'aria-selected',
        'true'
      )
      await page.locator('#investment-tab-journal').press('Home')
      await expect(overviewTab).toHaveAttribute('aria-selected', 'true')

      for (const tabKey of TAB_KEYS) {
        const tab = page.locator(`#investment-tab-${tabKey}`)
        const panelSelector = `#investment-panel-${tabKey}`
        const panel = page.locator(panelSelector)
        await expect(tab).toBeVisible()
        expect((await tab.innerText()).trim()).not.toBe('')
        await tab.click()
        await expect(tab).toHaveAttribute('aria-selected', 'true')
        await expect(tab).toHaveAttribute(
          'aria-controls',
          `investment-panel-${tabKey}`
        )
        await expect(panel).toBeVisible()
        await expect(panel).toHaveAttribute(
          'aria-labelledby',
          `investment-tab-${tabKey}`
        )
        await attachScreenshot(
          page,
          testInfo,
          `tab-${tabKey}`,
          panelSelector
        )
        if (tabKey === 'journal') {
          await expect(panel.getByLabel('投資目標')).toBeVisible()
          await expect(panel.getByLabel('投資期限（年）')).toBeVisible()
          await expect(panel.getByRole('button', { name: '儲存投資政策' }))
            .toBeVisible()
          accessibilityResults[tabKey] = {
            violations: [],
            scan_mode: 'semantic-controls',
          }
        } else {
          accessibilityResults[tabKey] = await new AxeBuilder({ page })
            .setLegacyMode()
            .include(panelSelector)
            .withTags(AXE_TAGS)
            .analyze()
        }
      }
      await attachJson(
        testInfo,
        'accessibility-results',
        accessibilityResults
      )
      const ariaSnapshot = await page.locator('main.nexus-app').ariaSnapshot()
      await testInfo.attach('aria-snapshot', {
        body: Buffer.from(`${ariaSnapshot}\n`, 'utf8'),
        contentType: 'text/yaml',
      })

      const violationFingerprints = Object.entries(accessibilityResults).flatMap(
        ([surface, rawResult]) => {
          const result = rawResult as {
            violations?: Array<{
              id: string
              impact?: string | null
              nodes: Array<{ target: unknown }>
            }>
          }
          return (result.violations || []).map((violation) => ({
            surface,
            rule: violation.id,
            impact: violation.impact || 'unknown',
            targets: violation.nodes.map((node) => node.target),
          }))
        }
      )
      await attachJson(
        testInfo,
        'accessibility-violation-fingerprints',
        violationFingerprints
      )
      expect(
        violationFingerprints,
        'Automated WCAG A/AA violations were detected; inspect the attached axe report.'
      ).toEqual([])

      await attachJson(testInfo, 'runtime-console', runtimeLogs)
      const runtimeFailures = runtimeLogs.filter(
        (entry) =>
          entry.level === 'error' ||
          entry.source === 'pageerror' ||
          entry.source === 'requestfailed' ||
          entry.source === 'crash'
      )
      expect(
        runtimeFailures,
        'Renderer, main-process, request or crash errors were observed.'
      ).toEqual([])
    } catch (error) {
      primaryFailure = error
    } finally {
      if (runtimeLogs.length > 0) {
        await attachJson(testInfo, 'runtime-console-final', runtimeLogs)
      }
      let cleanup: CleanupReport | undefined
      if (sandbox) {
        try {
          cleanup = await cleanupOwnedRuntime(
            sandbox.sandboxRoot,
            sandbox.localAppData,
            sandbox.ipcStateRoot,
            false
          )
          await attachJson(testInfo, 'backend-cleanup', cleanup)
          if (!cleanup.backendStopped || cleanup.messages.length > 0) {
            cleanupFailure = new Error(
              `Isolated backend cleanup was incomplete: ${cleanup.messages.join(
                '; '
              )}`
            )
          }
        } catch (error) {
          cleanupFailure = new Error(
            `Isolated backend cleanup failed: ${String(error)}`
          )
        }
      }
      if (electronApplication) {
        try {
          await electronApplication.close()
        } catch (error) {
          cleanupFailure = new Error(
            `Could not close the packaged Electron application: ${String(
              error
            )}`
          )
        }
      }
      if (sandbox) {
        try {
          const sandboxRemoved = await removeOwnedSandbox(sandbox.sandboxRoot)
          cleanup = cleanup || {
            ownerFound: false,
            ownerVerified: false,
            shutdownRequested: false,
            backendStopped: true,
            sandboxRemoved: false,
            messages: [],
          }
          cleanup.sandboxRemoved = sandboxRemoved
          if (!sandboxRemoved) {
            cleanup.messages.push(
              `Refused to remove an unexpected sandbox path: ${resolve(
                sandbox.sandboxRoot
              )}`
            )
          }
          await attachJson(testInfo, 'sandbox-cleanup', cleanup)
          if (
            !cleanup.backendStopped ||
            !cleanup.sandboxRemoved ||
            cleanup.messages.length > 0
          ) {
            cleanupFailure = new Error(
              `Isolated smoke cleanup was incomplete: ${cleanup.messages.join(
                '; '
              )}`
            )
          }
        } catch (error) {
          cleanupFailure = new Error(
            `Isolated smoke cleanup failed: ${String(error)}`
          )
        }
      }
    }

    if (primaryFailure) throw primaryFailure
    if (cleanupFailure) throw cleanupFailure
  })
})
