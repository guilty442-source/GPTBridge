import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { defineConfig } from 'vite'

const platformRoot = resolve(__dirname, 'src-ui/platform-tools')
const toolId = process.env.GPTBRIDGE_PLATFORM_TOOL_ID || 'vaultly'
const knownToolIds = new Set([
  'agent-coder',
  'ai-assistant',
  'file-sorter',
  'project-cleaner',
  'tool-mpz30cfk-hfnf',
  'tool-mqi8uv5x-fo9f',
  'vaultly',
])

if (!knownToolIds.has(toolId)) {
  throw new Error(`Unknown platform tool id: ${toolId}`)
}

export default defineConfig({
  root: platformRoot,
  base: './',
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src-ui/renderer'),
      '@resources': resolve(__dirname, 'resources'),
    },
  },
  server: {
    fs: {
      allow: [__dirname],
    },
  },
  build: {
    outDir: resolve(__dirname, 'dist-ui/platform-tools', toolId, 'renderer'),
    emptyOutDir: true,
    minify: false,
    target: 'es2022',
    rollupOptions: {
      input: {
        index: resolve(platformRoot, 'entries', `${toolId}.html`),
      },
    },
  },
})
