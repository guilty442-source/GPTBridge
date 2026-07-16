import { _electron as electron, expect, test } from '@playwright/test'
import path from 'node:path'

test.setTimeout(5 * 60_000)

const ROOT = path.resolve(__dirname, '..', '..')
const EXECUTABLE = path.join(ROOT, 'release', 'win-unpacked', 'GPTBridge.exe')
const TOOL_IDS = [
  'ai-assistant',
  'local-ai',
  'ai-collaboration',
  'file-sorter',
  'vaultly',
  'project-cleaner',
]

test('packaged product starts from embedded resources and connects locally', async () => {
  const inheritedEnvironment = { ...process.env }
  delete inheritedEnvironment.ELECTRON_RUN_AS_NODE
  delete inheritedEnvironment.GPTBRIDGE_PROJECT_ROOT
  delete inheritedEnvironment.GPTBRIDGE_SOURCE_PRODUCTION

  const app = await electron.launch({
    executablePath: EXECUTABLE,
    env: {
      ...inheritedEnvironment,
      GPTBRIDGE_MANAGE_BACKEND: '1',
      NODE_ENV: 'production',
    },
  })

  try {
    const runtime = await app.evaluate(({ app: electronApp }) => ({
      isPackaged: electronApp.isPackaged,
      resourcesPath: process.resourcesPath,
    }))
    expect(runtime.isPackaged).toBe(true)
    expect(path.normalize(runtime.resourcesPath)).toBe(
      path.join(ROOT, 'release', 'win-unpacked', 'resources')
    )

    const window = await app.firstWindow()
    await expect(window.getByTestId('product-version')).toHaveText('v1.0', {
      timeout: 180_000,
    })
    await expect(window.locator('body')).not.toContainText('39.8.10')
    await expect(window.getByTestId('backend-connection')).toContainText(
      '後端已連線',
      { timeout: 180_000 }
    )

    for (const toolId of TOOL_IDS) {
      const stop = window.getByTestId(`stop-${toolId}`)
      if (await stop.isEnabled()) {
        await stop.click()
        await expect(
          window
            .getByTestId(`tool-card-${toolId}`)
            .getByText('已停止', { exact: true })
        ).toBeVisible({ timeout: 180_000 })
      }
    }
  } finally {
    await app.close()
  }
})
