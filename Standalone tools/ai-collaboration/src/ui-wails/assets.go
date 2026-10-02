//go:build windows

// assets.go — serves the ai-collaboration renderer (src/ui) and the
// shared-layer toolWindow modules straight from the governed tool tree,
// injects the window.electron bridge prelude, and shims the CSS module
// import into a <link> injection so native ESM stays unchanged.
package main

import (
	"net/http"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

const bridgeJS = `// wails-bridge.js — window.electron shim over Wails bindings.
// The name is a compatibility alias only (the governed Tauri/WebView2
// preload uses the same surface); no Electron runtime exists —
// Electron is forbidden by the project language policy.
// Keeps the renderer contract identical to the governed host preload.
window.electron = {
	invoke: function (channel, payload) {
		return window.go.main.App.Invoke(channel, JSON.stringify(payload ?? {}));
	},
	onEvent: function (name, cb) {
		return window.runtime.EventsOn(name, function (data) { cb(data); });
	}
};
// TEMP-DEBUG: surface renderer errors via an asset request.
window.addEventListener("error", function (e) {
	try { fetch("/__dbg?m=" + encodeURIComponent("ERR " + (e.message || "") + " @" + (e.filename || "") + ":" + (e.lineno || 0))); } catch (_) {}
});
window.addEventListener("unhandledrejection", function (e) {
	try { fetch("/__dbg?m=" + encodeURIComponent("REJ " + String(e.reason))); } catch (_) {}
});
try { fetch("/__dbg?m=bridge-ready go=" + typeof window.go + " rt=" + typeof window.runtime); } catch (_) {}
`

var safeName = regexp.MustCompile(`^[a-zA-Z0-9._-]+$`)

type assetHandler struct {
	toolRoot      string
	workspaceRoot string
	uiDir         string
	sharedDir     string
}

func newAssetHandler(toolRoot, workspaceRoot string) *assetHandler {
	return &assetHandler{
		toolRoot:      toolRoot,
		workspaceRoot: workspaceRoot,
		uiDir:         filepath.Join(toolRoot, "src", "ui"),
		sharedDir: filepath.Join(workspaceRoot,
			"shared-layer", "src", "ui", "toolWindow"),
	}
}

var mimeTypes = map[string]string{
	".html":  "text/html; charset=utf-8",
	".js":    "text/javascript; charset=utf-8",
	".mjs":   "text/javascript; charset=utf-8",
	".css":   "text/css; charset=utf-8",
	".json":  "application/json; charset=utf-8",
	".svg":   "image/svg+xml",
	".png":   "image/png",
	".ico":   "image/x-icon",
	".woff":  "font/woff",
	".woff2": "font/woff2",
}

func (h *assetHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	urlPath := path.Clean("/" + r.URL.Path)
	trace("http " + r.URL.Path + " -> " + urlPath)
	switch {
	case urlPath == "/" || urlPath == "/index.html":
		h.serveIndex(w)
		return
	case urlPath == "/wails-bridge.js":
		w.Header().Set("Content-Type", mimeTypes[".js"])
		_, _ = w.Write([]byte(bridgeJS))
		return
	}
	// /shared-layer/... → workspace shared-layer tree (toolWindow only).
	if rest, ok := strings.CutPrefix(urlPath, "/shared-layer/src/ui/toolWindow/"); ok {
		h.serveFile(w, r, h.sharedDir, rest, urlPath)
		return
	}
	// Everything else resolves inside src/ui (flat module paths).
	h.serveFile(w, r, h.uiDir, strings.TrimPrefix(urlPath, "/"), urlPath)
}

func (h *assetHandler) serveIndex(w http.ResponseWriter) {
	raw, err := os.ReadFile(filepath.Join(h.uiDir, "index.html"))
	if err != nil {
		http.Error(w, "index.html unavailable", http.StatusNotFound)
		return
	}
	html := string(raw)
	inject := `<script src="/wails-bridge.js"></script>`
	if i := strings.Index(html, "</head>"); i >= 0 {
		html = html[:i] + inject + html[i:]
	} else {
		html = inject + html
	}
	w.Header().Set("Content-Type", mimeTypes[".html"])
	_, _ = w.Write([]byte(html))
}

// serveFile streams one whitelisted file.  A .css fetch without the
// ?as=link marker is an ES-module import and gets the link-injection
// shim (the shim's <link> re-fetches the same URL with the marker, so
// stylesheet bytes are only ever served to a real stylesheet fetch) —
// `import "./x.css"` stays legal in native ESM without header sniffing.
func (h *assetHandler) serveFile(w http.ResponseWriter, r *http.Request,
	dir, name, urlPath string) {
	if !safeName.MatchString(name) {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	full := filepath.Join(dir, name)
	ext := strings.ToLower(filepath.Ext(name))
	if ext == ".css" && r.URL.Query().Get("as") != "link" {
		w.Header().Set("Content-Type", mimeTypes[".js"])
		_, _ = w.Write([]byte(
			`const l=document.createElement("link");l.rel="stylesheet";l.href=` +
				strconv.Quote(urlPath+"?as=link") +
				`;document.head.appendChild(l);export default null;`))
		return
	}
	data, err := os.ReadFile(full)
	if err != nil {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	if mt, ok := mimeTypes[ext]; ok {
		w.Header().Set("Content-Type", mt)
	}
	_, _ = w.Write(data)
}
