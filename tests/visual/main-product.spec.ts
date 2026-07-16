import { _electron as electron, expect, test } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import path from 'node:path'

test.describe.configure({ mode: 'serial' })
test.setTimeout(15 * 60_000)

const ROOT = path.resolve(__dirname, '..', '..')
const TOOL_IDS = [
  'ai-assistant',
  'local-ai',
  'ai-collaboration',
  'file-sorter',
  'vaultly',
  'project-cleaner',
]

test('offline interface disables every state-changing action without queuing', async ({
  page,
}) => {
  await page.goto('http://127.0.0.1:5180')
  await expect(page.getByTestId('product-version')).toHaveText('v1.0')
  await expect(page.getByText('離線安全模式')).toBeVisible()
  await expect(
    page.getByText('狀態變更指令不會送出或排隊', { exact: false })
  ).toBeVisible()
  await expect(page.getByTestId('check-updates')).toBeDisabled()
  for (const toolId of TOOL_IDS) {
    await expect(page.getByTestId(`start-${toolId}`)).toBeDisabled()
    await expect(page.getByTestId(`stop-${toolId}`)).toBeDisabled()
  }
})

test('production shell connects and exercises every launcher capability', async () => {
  const inheritedEnvironment = { ...process.env }
  delete inheritedEnvironment.ELECTRON_RUN_AS_NODE

  const app = await electron.launch({
    args: [path.join(ROOT, 'dist-ui', 'main', 'index.js')],
    cwd: ROOT,
    env: {
      ...inheritedEnvironment,
      GPTBRIDGE_SOURCE_PRODUCTION: '1',
      GPTBRIDGE_MANAGE_BACKEND: '1',
      GPTBRIDGE_PROJECT_ROOT: ROOT,
      NODE_ENV: 'production',
    },
  })

  try {
    const window = await app.firstWindow()
    await expect(window.getByTestId('product-version')).toHaveText('v1.0', {
      timeout: 180_000,
    })
    await expect(window.locator('body')).not.toContainText('39.8.10')
    await expect(window.getByTestId('backend-connection')).toContainText(
      '後端已連線',
      { timeout: 180_000 }
    )
    await expect(window.getByTestId('tool-card-ai-assistant')).toBeVisible()

    await expect(window.getByTestId('check-updates')).toBeEnabled({
      timeout: 180_000,
    })
    await expect(window.getByTestId('refresh-tools')).toBeEnabled({
      timeout: 30_000,
    })
    await window.getByTestId('refresh-tools').click()
    await expect(window.getByTestId('refresh-tools')).toBeEnabled({
      timeout: 30_000,
    })

    await window.getByTestId('check-updates').click()
    await expect(window.getByText('目前已是最新狀態')).toBeVisible({
      timeout: 30_000,
    })

    for (const toolId of TOOL_IDS) {
      const card = window.getByTestId(`tool-card-${toolId}`)
      const start = window.getByTestId(`start-${toolId}`)
      const stop = window.getByTestId(`stop-${toolId}`)

      if (await stop.isEnabled()) {
        await stop.click()
        await expect(card.getByText('已停止', { exact: true })).toBeVisible({
          timeout: 180_000,
        })
      }
      await expect(start).toBeEnabled({ timeout: 30_000 })
      await start.click()
      await expect(card.getByText('執行中', { exact: true })).toBeVisible({
        timeout: 90_000,
      })
      await expect(stop).toBeEnabled()
      await stop.click()
      await expect(card.getByText('已停止', { exact: true })).toBeVisible({
        timeout: 180_000,
      })
    }

    const backendPid = Number(
      execFileSync(
        'powershell.exe',
        [
          '-NoProfile',
          '-Command',
          '(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop | Select-Object -First 1).OwningProcess',
        ],
        { encoding: 'utf8' }
      ).trim()
    )
    expect(backendPid).toBeGreaterThan(0)
    execFileSync(
      'powershell.exe',
      ['-NoProfile', '-Command', `Stop-Process -Id ${backendPid} -Force -ErrorAction Stop`],
      { encoding: 'utf8' }
    )

    await expect(window.getByTestId('check-updates')).toBeDisabled({
      timeout: 30_000,
    })
    await expect(window.getByTestId('backend-connection')).toContainText(
      '後端已連線',
      { timeout: 180_000 }
    )
    await expect(window.getByTestId('check-updates')).toBeEnabled({
      timeout: 180_000,
    })
  } finally {
    await app.close()
  }
})
