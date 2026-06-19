import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { defineConfig } from 'vite'

export default defineConfig({
  root: resolve(__dirname, 'src-ui/renderer'),
  base: './',
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src-ui/renderer'),
      '@resources': resolve(__dirname, 'resources'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5180,
    strictPort: true,
    fs: {
      allow: [__dirname],
    },
    hmr: {
      protocol: 'ws',
      host: '127.0.0.1',
      port: 5180,
      clientPort: 5180,
      overlay: true,
      timeout: 30000,
    },
    watch: {
      // Keep watcher focused to reduce false reloads and HMR disconnects.
      ignored: [
        '**/runtime/**',
        '**/backups/**',
        '**/.GPTBridge_RuntimeSandbox/**',
        '**/dist-ui/**',
        '**/release/**',
      ],
      // Fast by default. Set GPTBRIDGE_HMR_POLLING=1 when fs events are unreliable.
      usePolling: process.env.GPTBRIDGE_HMR_POLLING === '1',
      interval: 120,
      binaryInterval: 300,
      awaitWriteFinish: {
        stabilityThreshold: 120,
        pollInterval: 50,
      },
    },
  },
  build: {
    outDir: resolve(__dirname, 'dist-ui/renderer'),
    emptyOutDir: true,
    minify: false,
    target: 'es2022',
  },
})
