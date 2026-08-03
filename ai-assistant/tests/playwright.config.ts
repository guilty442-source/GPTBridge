import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/visual',
  testMatch: 'ai-assistant.packaged.spec.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: {
    timeout: 20_000,
  },
  outputDir: 'test-results/ai-assistant-visual',
  reporter: [
    ['line'],
    [
      'html',
      {
        open: 'never',
        outputFolder: 'playwright-report/ai-assistant',
      },
    ],
  ],
})
