//go:build windows

// app.go — Wails bound app: the window.electron-compatible invoke
// surface the ai-collaboration renderer expects (app:* session channels
// + embedded-browser:* + the backend-delegated dom-op lane).
package main

import (
	"context"
	"encoding/json"

	wailsruntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

// App is bound to the frontend via `window.go.main.App`.
type App struct {
	ctx     context.Context
	manager *BrowserManager
	session *uiSession
}

type uiSession struct {
	WebsocketURL string
	Version      string
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
		return map[string]any{"ok": true}
	case "app:get-backend-session":
		return map[string]any{
			"ok":              true,
			"websocketUrl":    a.session.WebsocketURL,
			"protocolVersion": 1,
			"backendVersion":  a.session.Version,
		}
	case "embedded-browser:dom-op":
		if a.manager == nil {
			return failOp("BROWSER_BRIDGE_UNAVAILABLE",
				"browser manager not ready")
		}
		return a.manager.DOMOp(payload)
	default:
		if a.manager == nil {
			return map[string]any{"ok": false,
				"message": "browser manager not ready"}
		}
		return a.manager.Invoke(channel, payload)
	}
}
