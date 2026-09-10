import { builtinModules } from 'node:module'
import { resolve } from 'node:path'
import { defineConfig } from 'vite'

export default defineConfig({
  build: {
    outDir: resolve(__dirname, 'dist-ui/templates'),
    emptyOutDir: true,
    minify: false,
    target: 'node18',
    rollupOptions: {
      input: {
        main: resolve(
          __dirname,
          'src-core/tasks/templates/platform-tool-app/main.ts',
        ),
        preload: resolve(
          __dirname,
          'src-core/tasks/templates/platform-tool-app/preload.ts',
        ),
      },
      output: {
        format: 'cjs',
        entryFileNames: '[name].cjs',
      },
      external: [
        'electron',
        ...builtinModules,
        ...builtinModules.map((m) => `node:${m}`),
      ],
    },
  },
})
