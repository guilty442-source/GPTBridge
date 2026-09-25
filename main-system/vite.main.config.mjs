import { builtinModules } from "node:module";
import { resolve } from "node:path";
import { defineConfig } from "vite";
export default defineConfig({
	resolve: { alias: {
		"@": resolve(import.meta.dirname, "src-ui/main"),
		"@shared": resolve(import.meta.dirname, "src-ui/types")
	} },
	build: {
		outDir: resolve(import.meta.dirname, "dist-ui/main"),
		emptyOutDir: true,
		minify: false,
		target: "node18",
		rollupOptions: {
			input: {
				index: resolve(import.meta.dirname, "src-ui/main/index.ts"),
				preload: resolve(import.meta.dirname, "src-ui/main/preload.ts")
			},
			output: {
				format: "cjs",
				entryFileNames: "[name].js"
			},
			external: [
				"electron",
				...builtinModules,
				...builtinModules.map((m) => `node:${m}`)
			]
		}
	}
});
