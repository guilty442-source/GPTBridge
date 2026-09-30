//go:build windows

// app.go — Wails bound app: the window.electron-compatible invoke
// surface the ai-collaboration renderer expects (app:* session channels
// + embedded-browser:* + the backend-delegated dom-op lane).  Channel
// semantics mirror the governed tool-window dispatch
// (main-system/src-tauri/src/tool_dispatch.rs).
package main

import (
	"context"
	"encoding/json"
	"net/url"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"

	wailsruntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

// App is bound to the frontend via `window.go.main.App`.
type App struct {
	ctx           context.Context
	manager       *BrowserManager
	session       *uiSession
	workspaceRoot string
}

type uiSession struct {
	WebsocketURL string
	Token        string
	Version      string
	Title        string
	ToolRoot     string
}

func NewApp() *App { return &App{} }

func (a *App) Startup(ctx context.Context) {
	a.ctx = ctx
	mgr := NewBrowserManager(uint32(pid()), a.session.Title,
		a.session.ToolRoot, func(payload map[string]any) {
			if a.ctx != nil {
				wailsruntime.EventsEmit(a.ctx, "embedded-browser:event", payload)
			}
		})
	a.manager = mgr
}

func (a *App) Shutdown(context.Context) {
	if a.manager != nil {
		a.manager.Shutdown()
	}
}

// Invoke mirrors window.electron.invoke(channel, payloadJson).
func (a *App) Invoke(channel string, payloadJSON string) map[string]any {
	var payload map[string]any
	if payloadJSON != "" {
		_ = json.Unmarshal([]byte(payloadJSON), &payload)
	}
	if payload == nil {
		payload = map[string]any{}
	}
	switch channel {
	case "app:ensure-backend-started":
		// The governed runtime already spawned the backend — the UI
		// never starts it itself.
		return map[string]any{"ok": true, "managed": true,
			"runtimeMode": "governed-source"}
	case "app:get-backend-session":
		return map[string]any{
			"ok":              true,
			"token":           a.session.Token,
			"websocketUrl":    a.session.WebsocketURL,
			"protocolVersion": 1,
			"backendVersion":  a.session.Version,
			"runtimeMode":     "governed-source",
		}
	case "app:open-path":
		return a.openPath(payload)
	case "embedded-browser:dom-op":
		if a.manager == nil {
			return failOp("BROWSER_BRIDGE_UNAVAILABLE",
				"browser manager not ready")
		}
		return a.manager.DOMOp(payload)
	default:
		if strings.HasPrefix(channel, "embedded-browser:") {
			if a.manager == nil {
				return map[string]any{"ok": false,
					"message": "browser manager not ready"}
			}
			return a.manager.Invoke(channel, payload)
		}
		return map[string]any{"ok": false,
			"message": "Blocked IPC channel: " + channel}
	}
}

// openPath opens a path with the system shell — fail closed to paths
// outside the workspace root (tool_dispatch.rs parity).
func (a *App) openPath(payload map[string]any) map[string]any {
	raw, _ := payload["path"].(string)
	if raw == "" {
		base, _ := payload["basePath"].(string)
		rel, _ := payload["relativePath"].(string)
		if base == "" {
			base = a.workspaceRoot
		}
		raw = filepath.Join(base, rel)
	}
	if raw == "" {
		return map[string]any{"ok": false, "message": "path is required"}
	}
	abs, err := filepath.Abs(raw)
	if err != nil {
		return map[string]any{"ok": false, "message": err.Error()}
	}
	ws := filepath.Clean(a.workspaceRoot)
	if !strings.HasPrefix(strings.ToLower(abs),
		strings.ToLower(ws)+string(filepath.Separator)) {
		return map[string]any{"ok": false,
			"message": "Path is outside the application workspace"}
	}
	var cmd *exec.Cmd
	if mode, _ := payload["mode"].(string); mode == "reveal" {
		cmd = exec.Command("explorer.exe", "/select,", abs)
	} else {
		cmd = exec.Command("rundll32.exe",
			"url.dll,FileProtocolHandler", abs)
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	if err := cmd.Start(); err != nil {
		return map[string]any{"ok": false, "message": err.Error()}
	}
	return map[string]any{"ok": true, "path": abs}
}

// wsToken extracts the session token from the governed ws URL query.
func wsToken(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return u.Query().Get("token")
}
