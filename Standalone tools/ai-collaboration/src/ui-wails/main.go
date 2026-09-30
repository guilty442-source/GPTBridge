//go:build windows

// ai-collab-ui — Go + Wails tool window for ai-collaboration.
// The main window renders the governed src/ui renderer on WebView2;
// each provider browser session is a WebView2 child HWND managed by
// BrowserManager (browser.go).  Launch contract: governed launcher
// injects GPTBRIDGE_SOURCE_UI_* and passes --tool-window --tool-id=<id>.
package main

import (
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/logger"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
	wailswindows "github.com/wailsapp/wails/v2/pkg/options/windows"
)

const toolID = "ai-collaboration"

func envInt(key string, fallback int) int {
	if v, err := strconv.Atoi(strings.TrimSpace(os.Getenv(key))); err == nil && v > 0 {
		return v
	}
	return fallback
}

func pid() int { return os.Getpid() }

func main() {
	// Governed launch contract: tool-window mode requires the injected
	// source-UI environment; fail closed when it is absent.
	toolRoot := strings.TrimSpace(os.Getenv("GPTBRIDGE_SOURCE_UI_TOOL_ROOT"))
	workspaceRoot := strings.TrimSpace(
		os.Getenv("GPTBRIDGE_SOURCE_UI_WORKSPACE_ROOT"))
	wsURL := strings.TrimSpace(os.Getenv("GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL"))
	envToolID := strings.TrimSpace(os.Getenv("GPTBRIDGE_SOURCE_UI_TOOL_ID"))
	if envToolID == "" {
		for _, arg := range os.Args[1:] {
			if strings.HasPrefix(arg, "--tool-id=") {
				envToolID = strings.TrimPrefix(arg, "--tool-id=")
			}
		}
	}
	if toolRoot == "" || workspaceRoot == "" || wsURL == "" {
		fmt.Fprintln(os.Stderr,
			"CONFIG_INVALID: missing GPTBRIDGE_SOURCE_UI_* environment")
		os.Exit(2)
	}
	if envToolID != toolID {
		fmt.Fprintf(os.Stderr,
			"CONFIG_INVALID: tool id %q != %q\n", envToolID, toolID)
		os.Exit(2)
	}
	if !strings.HasPrefix(wsURL, "ws://127.0.0.1:") {
		fmt.Fprintln(os.Stderr,
			"CONFIG_INVALID: websocket url must be loopback")
		os.Exit(2)
	}
	title := strings.TrimSpace(os.Getenv("GPTBRIDGE_SOURCE_UI_TITLE"))
	if title == "" {
		title = "AI 協作 · GPTBridge"
	}

	app := NewApp()
	app.session = &uiSession{WebsocketURL: wsURL, Version: "1.0.0",
		Title: title, ToolRoot: toolRoot}

	handler := newAssetHandler(toolRoot, workspaceRoot)
	if dbg := strings.TrimSpace(os.Getenv("AICOLLAB_DEBUG_HTTP")); dbg != "" {
		go func() {
			_ = http.ListenAndServe("127.0.0.1:"+dbg, handler)
		}()
	}
	err := wails.Run(&options.App{
		Title:     title,
		Width:     envInt("GPTBRIDGE_SOURCE_UI_WIDTH", 1280),
		Height:    envInt("GPTBRIDGE_SOURCE_UI_HEIGHT", 840),
		MinWidth:  envInt("GPTBRIDGE_SOURCE_UI_MIN_WIDTH", 960),
		MinHeight: envInt("GPTBRIDGE_SOURCE_UI_MIN_HEIGHT", 600),
		AssetServer: &assetserver.Options{
			Handler: handler,
		},
		Bind:               []interface{}{app},
		OnStartup:          app.Startup,
		OnShutdown:         app.Shutdown,
		LogLevel:           logger.DEBUG,
		LogLevelProduction: logger.DEBUG,
		Windows: &wailswindows.Options{
			WebviewUserDataPath: filepath.Join(
				toolRoot, "runtime", "webview2", "shell"),
		},
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "wails runtime error:", err)
		os.Exit(1)
	}
}
