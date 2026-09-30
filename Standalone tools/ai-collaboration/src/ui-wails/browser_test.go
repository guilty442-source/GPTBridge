//go:build windows

// browser_test.go — live WebView2 lifecycle test for BrowserManager.
// A hidden top-level HWND created on the pump thread stands in for the
// Wails main window (cross-thread parent/child attaches input queues and
// deadlocks a non-pumping owner, so the test parent must pump too);
// every session op runs through the real WebView2 pipeline.
package main

import (
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

const wsPopupWindow = 0x80000000

func createPopupWindow() (uintptr, error) {
	if err := registerBrowserClass(); err != nil {
		return 0, err
	}
	name, _ := syscall.UTF16PtrFromString("AiCollabWebView2Host")
	caption, _ := syscall.UTF16PtrFromString("aicollab-ui-test-parent")
	hinst, _, _ := procGetModuleHandleW.Call(0)
	hwnd, _, err := procCreateWindowExW.Call(
		0, uintptr(unsafe.Pointer(name)), uintptr(unsafe.Pointer(caption)),
		wsPopupWindow, 0, 0, 640, 480, 0, 0, hinst, 0)
	if hwnd == 0 {
		return 0, err
	}
	return hwnd, nil
}

func wantOK(t *testing.T, op string, res map[string]any) {
	t.Helper()
	if ok, _ := res["ok"].(bool); !ok {
		t.Fatalf("%s failed: %v", op, res)
	}
}

func TestBrowserManagerLifecycle(t *testing.T) {
	// WebView2 child processes outlive session close by a few hundred
	// ms — use a plain dir and retry cleanup instead of t.TempDir.
	dataRoot := filepath.Join(os.TempDir(),
		"aicollab-ui-test-"+strconv.Itoa(int(pid())))
	var evMu sync.Mutex
	var events []map[string]any
	m := NewBrowserManager(uint32(pid()), "aicollab-ui-test-parent",
		dataRoot, func(p map[string]any) {
			evMu.Lock()
			events = append(events, p)
			evMu.Unlock()
		})

	var parent uintptr
	var perr error
	m.run(func() { parent, perr = createPopupWindow() })
	if perr != nil || parent == 0 {
		t.Fatalf("create parent window: %v", perr)
	}
	m.parent = parent
	defer func() { m.run(func() { destroyWindow(parent) }) }()
	defer m.Shutdown()

	res := m.Invoke("embedded-browser:create", map[string]any{
		"id": "s1", "ownerModule": "test",
		"bounds": map[string]any{"x": 0, "y": 0, "width": 320, "height": 200},
	})
	wantOK(t, "create", res)

	res = m.DOMOp(map[string]any{"op": "navigate", "session_id": "s1",
		"url": "data:text/html,<title>aicollab-e2e</title><p>hi</p>"})
	wantOK(t, "navigate", res)

	res = m.DOMOp(map[string]any{"op": "exec", "session_id": "s1",
		"script": "document.title"})
	wantOK(t, "exec", res)
	if got, _ := res["result"].(string); got != "aicollab-e2e" {
		t.Fatalf("exec result=%v want aicollab-e2e", res["result"])
	}

	res = m.DOMOp(map[string]any{"op": "url", "session_id": "s1"})
	wantOK(t, "url", res)

	res = m.Invoke("embedded-browser:state", map[string]any{"id": "s1"})
	wantOK(t, "state", res)

	res = m.Invoke("embedded-browser:list", map[string]any{})
	wantOK(t, "list", res)
	if ids, _ := res["sessions"].([]string); len(ids) != 1 || ids[0] != "s1" {
		t.Fatalf("list=%v", res["sessions"])
	}

	res = m.Invoke("embedded-browser:close-module",
		map[string]any{"ownerModule": "test"})
	wantOK(t, "close-module", res)
	if n, _ := res["closed"].(int); n != 1 {
		t.Fatalf("closed=%v want 1", res["closed"])
	}

	res = m.DOMOp(map[string]any{"op": "url", "session_id": "s1"})
	if ok, _ := res["ok"].(bool); ok {
		t.Fatalf("url after close should fail: %v", res)
	}

	for i := 0; i < 20; i++ {
		if err := os.RemoveAll(dataRoot); err == nil {
			return
		}
		time.Sleep(250 * time.Millisecond)
	}
	_ = os.RemoveAll(dataRoot)
}
