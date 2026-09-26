import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig } from "vite";
export default defineConfig({
	root: resolve(import.meta.dirname, "src-ui/renderer"),
	base: "./",
	plugins: [react({
		// React Compiler: auto-memoizes components/hooks at build time —
		// eliminates redundant re-renders without manual useMemo/memo.
		babel: { plugins: ["babel-plugin-react-compiler"] }
	})],
	resolve: { alias: {
		"@": resolve(import.meta.dirname, "src-ui/renderer"),
		"@main-locales": resolve(import.meta.dirname, "locales"),
		"@resources": resolve(import.meta.dirname, "resources")
	} },
	server: {
		host: true,
		port: 5173,
		strictPort: true
	},
	build: {
		outDir: resolve(import.meta.dirname, "dist-ui/renderer"),
		emptyOutDir: true,
		// Perf: esbuild minify + react vendor chunk — smaller first load
		// and long-term caching of the framework bundle across app edits.
		minify: true,
		target: "es2022",
		rollupOptions: {
			output: {
				// rolldown-based vite expects the function form here.
				manualChunks: (id) => {
					if (
						id.includes("node_modules/react/") ||
						id.includes("node_modules/react-dom/") ||
						id.includes("node_modules/scheduler/")
					) {
						return "vendor"
					}
					return undefined
				},
			},
		}
	}
});
