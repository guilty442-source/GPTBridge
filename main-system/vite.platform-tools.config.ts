import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import { isAbsolute, relative, resolve } from 'node:path'
import { defineConfig } from 'vite'

const toolId = String(process.env.GPTBRIDGE_PLATFORM_TOOL_ID || '').trim()
if (!/^[a-z0-9_-]+$/.test(toolId)) {
  throw new Error('GPTBRIDGE_PLATFORM_TOOL_ID is required')
}

const projectRoot = resolve(__dirname, '..')
const declaredToolRoot = String(process.env.GPTBRIDGE_PLATFORM_TOOL_ROOT || '').trim()
const toolRoot = declaredToolRoot
  ? resolve(declaredToolRoot)
  : resolve(projectRoot, toolId)
const relativeToolRoot = relative(projectRoot, toolRoot)
if (!relativeToolRoot || relativeToolRoot.startsWith('..') || isAbsolute(relativeToolRoot)) {
  throw new Error(`Tool renderer root escaped the project for ${toolId}`)
}
const platformRoot = resolve(toolRoot, 'src', 'ui')
const manifestPath = resolve(toolRoot, 'manifest.json')
const entryPath = resolve(platformRoot, 'index.html')

if (!fs.existsSync(manifestPath) || !fs.existsSync(entryPath)) {
  throw new Error(`Tool-owned renderer entry is missing for ${toolId}`)
}

export default defineConfig({
  root: platformRoot,
  base: './',
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src-ui', 'renderer'),
      '@resources': resolve(__dirname, 'resources'),
      react: resolve(__dirname, 'node_modules', 'react'),
      'react-dom': resolve(__dirname, 'node_modules', 'react-dom'),
    },
    dedupe: ['react', 'react-dom'],
  },
  build: {
    outDir: resolve(__dirname, 'dist-ui', 'independent-tools', toolId, 'renderer'),
    emptyOutDir: true,
    minify: false,
    target: 'es2022',
    rollupOptions: {
      input: { index: entryPath },
    },
  },
})
