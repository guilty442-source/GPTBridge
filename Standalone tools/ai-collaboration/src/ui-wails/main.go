//go:build windows

// ai-collab-ui — Go + Wails tool window for ai-collaboration.
// The main window renders the governed src/ui renderer on WebView2;
// each provider browser session is a WebView2 child HWND managed by
// BrowserManager (browser.go).  Launch contract mirrors the governed
// tool-window host (main-system/src-tauri tool_window): the launcher
// injects GPTBRIDGE_SOURCE_UI_* and the process fails closed without it.
package main

import (
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"
	"unsafe"

	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/logger"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
	wailswindows "github.com/wailsapp/wails/v2/pkg/options/windows"
)

const toolID = "ai-collaboration"

var traceFile = filepath.Join(os.TempDir(), "aicollab-ui-trace.log")

var traceOn = os.Getenv("AICOLLAB_TRACE") == "1"

func trace(msg string) {
	if !traceOn {
		return
	}
	f, err := os.OpenFile(traceFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "%s %s\n", time.Now().Format("15:04:05.000"), msg)
}

func envInt(key string, fallback int) int {
	if v, err := strconv.Atoi(strings.TrimSpace(os.Getenv(key))); err == nil && v > 0 {
		return v
	}
	return fallback
}

func pid() int { return os.Getpid() }

func isHex(s string) bool {
	for _, c := range s {
		if !('0' <= c && c <= '9') && !('a' <= c && c <= 'f') &&
			!('A' <= c && c <= 'F') {
			return false
		}
	}
	return true
}

// validateWSURL — Electron validateConfiguration parity: loopback ws://,
// port 1024-65535, 64-hex session token, 24-hex workspace instance id.
func validateWSURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return err
	}
	if u.Scheme != "ws" || u.Hostname() != "127.0.0.1" ||
		u.User != nil {
		return fmt.Errorf("websocket url must be loopback ws://")
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil || port < 1024 || port > 65535 {
		return fmt.Errorf("websocket port invalid")
	}
	tok, inst := u.Query().Get("token"), u.Query().Get("instance")
	if len(tok) != 64 || !isHex(tok) || len(inst) != 24 || !isHex(inst) {
		return fmt.Errorf("websocket token/instance invalid")
	}
	return nil
}

func inside(root, child string) bool {
	abs, err := filepath.Abs(child)
	if err != nil {
		return false
	}
	r := filepath.Clean(root)
	return strings.HasPrefix(strings.ToLower(abs),
		strings.ToLower(r)+string(filepath.Separator))
}

// acquireToolMutex — per-tool single instance parity
// (Local\gptbridge-tool-window-<id>).
func acquireToolMutex(id string) bool {
	name := "Local\\gptbridge-tool-window-" + id
	u16 := utf16.Encode([]rune(name + "\x00"))
	h, _, _ := syscall.NewLazyDLL("kernel32.dll").
		NewProc("CreateMutexW").Call(0, 0,
		uintptr(unsafe.Pointer(&u16[0])))
	if h == 0 {
		return true
	}
	_, e, _ := syscall.NewLazyDLL("kernel32.dll").
		NewProc("GetLastError").Call()
	const errorAlreadyExists = 183
	return e != errorAlreadyExists
}

func main() {
	// Governed launch contract: tool-window mode requires the injected
	// source-UI environment; fail closed when it is absent or invalid.
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
	if !inside(workspaceRoot, toolRoot) {
		fmt.Fprintln(os.Stderr,
			"CONFIG_INVALID: tool root outside workspace")
		os.Exit(2)
	}
	if err := validateWSURL(wsURL); err != nil {
		fmt.Fprintln(os.Stderr, "CONFIG_INVALID:", err)
		os.Exit(2)
	}
	if !acquireToolMutex(toolID) {
		fmt.Fprintln(os.Stderr, "instance.duplicate:", toolID)
		os.Exit(0)
	}
	title := strings.TrimSpace(os.Getenv("GPTBRIDGE_SOURCE_UI_TITLE"))
	if title == "" {
		title = "AI 協作 · GPTBridge"
	}
	version := strings.TrimSpace(os.Getenv("GPTBRIDGE_SOURCE_UI_VERSION"))
	if version == "" {
		version = "1.0.0"
	}
	cacheRoot := strings.TrimSpace(os.Getenv("GPTBRIDGE_TOOL_CACHE_ROOT"))
	if cacheRoot == "" {
		cacheRoot = filepath.Join(toolRoot, "runtime", "cache")
	}
	_ = os.MkdirAll(cacheRoot, 0o755)

	app := NewApp()
	app.workspaceRoot = workspaceRoot
	app.session = &uiSession{WebsocketURL: wsURL, Token: wsToken(wsURL),
		Version: version, Title: title, ToolRoot: toolRoot}

	handler := newAssetHandler(toolRoot, workspaceRoot)
	trace("before wails.Run")
	if dbg := strings.TrimSpace(os.Getenv("AICOLLAB_DEBUG_HTTP")); dbg != "" {
		go func() {
			_ = http.ListenAndServe("127.0.0.1:"+dbg, handler)
		}()
	}
	err := wails.Run(&options.App{
		Title:       title,
		Width:       envInt("GPTBRIDGE_SOURCE_UI_WIDTH", 1440),
		Height:      envInt("GPTBRIDGE_SOURCE_UI_HEIGHT", 920),
		MinWidth:    envInt("GPTBRIDGE_SOURCE_UI_MIN_WIDTH", 1120),
		MinHeight:   envInt("GPTBRIDGE_SOURCE_UI_MIN_HEIGHT", 760),
		StartHidden: os.Getenv("GPTBRIDGE_START_HIDDEN") == "1",
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
				cacheRoot, "source-ui-user-data"),
		},
	})
	trace(fmt.Sprintf("after wails.Run err=%v", err))
	if err != nil {
		fmt.Fprintln(os.Stderr, "wails runtime error:", err)
		os.Exit(1)
	}
}
