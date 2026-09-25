import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig } from "vite";
export default defineConfig({
	root: resolve(import.meta.dirname, "src-ui/renderer"),
	base: "./",
	plugins: [react()],
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
		minify: false,
		target: "es2022"
	}
});
