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
// Keeps the renderer contract identical to the governed host preload.
window.electron = {
	invoke: function (channel, payload) {
		return window.go.main.App.Invoke(channel, JSON.stringify(payload ?? {}));
	},
	onEvent: function (name, cb) {
		return window.runtime.EventsOn(name, function (data) { cb(data); });
	}
};
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
		h.serveFile(w, r, h.sharedDir, rest)
		return
	}
	// Everything else resolves inside src/ui (flat module paths).
	h.serveFile(w, r, h.uiDir, strings.TrimPrefix(urlPath, "/"))
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

// serveFile streams one whitelisted file.  A .css fetch coming from a
// module import (Sec-Fetch-Dest: script/empty) gets the link-injection
// shim so `import "./x.css"` stays legal in native ESM.
func (h *assetHandler) serveFile(w http.ResponseWriter, r *http.Request,
	dir, name string) {
	if !safeName.MatchString(name) {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	full := filepath.Join(dir, name)
	ext := strings.ToLower(filepath.Ext(name))
	if ext == ".css" {
		dest := r.Header.Get("Sec-Fetch-Dest")
		if dest == "script" || dest == "empty" || strings.Contains(
			r.Header.Get("Accept"), "javascript") {
			w.Header().Set("Content-Type", mimeTypes[".js"])
			_, _ = w.Write([]byte(
				`const l=document.createElement("link");l.rel="stylesheet";l.href=` +
					strconv.Quote("/"+name) +
					`;document.head.appendChild(l);export default null;`))
			return
		}
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
