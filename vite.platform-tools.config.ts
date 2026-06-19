import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { defineConfig } from 'vite'

export default defineConfig({
  root: resolve(__dirname, 'src-ui/platform-tools'),
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
    outDir: resolve(__dirname, 'dist-ui/platform-tools/renderer'),
    emptyOutDir: true,
    minify: false,
    target: 'es2022',
  },
})
