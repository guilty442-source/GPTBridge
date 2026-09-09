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
      '@main-locales': resolve(__dirname, 'locales'),
      '@resources': resolve(__dirname, 'resources'),
    },
  },
  server: {
    host: true,
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: resolve(__dirname, 'dist-ui/renderer'),
    emptyOutDir: true,
    minify: false,
    target: 'es2022',
  },
})
