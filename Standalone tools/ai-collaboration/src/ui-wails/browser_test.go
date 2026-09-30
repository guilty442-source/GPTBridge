//go:build windows

// browser_test.go — live WebView2 lifecycle test for BrowserManager.
// A hidden top-level HWND stands in for the Wails main window; every
// session op runs through the real pump-thread/WebView2 pipeline.
package main

import (
	"sync"
	"syscall"
	"testing"
	"unsafe"
)

const wsPopupWindow = 0x80000000

func newTestParent(t *testing.T) uintptr {
	t.Helper()
	if err := registerBrowserClass(); err != nil {
		t.Fatalf("register browser class: %v", err)
	}
	name, _ := syscall.UTF16PtrFromString("AiCollabWebView2Host")
	caption, _ := syscall.UTF16PtrFromString("aicollab-ui-test-parent")
	hinst, _, _ := procGetModuleHandleW.Call(0)
	hwnd, _, err := procCreateWindowExW.Call(
		0, uintptr(unsafe.Pointer(name)), uintptr(unsafe.Pointer(caption)),
		wsPopupWindow, 0, 0, 640, 480, 0, 0, hinst, 0)
	if hwnd == 0 {
		t.Fatalf("create parent window: %v", err)
	}
	return hwnd
}

func wantOK(t *testing.T, op string, res map[string]any) {
	t.Helper()
	if ok, _ := res["ok"].(bool); !ok {
		t.Fatalf("%s failed: %v", op, res)
	}
}

func TestBrowserManagerLifecycle(t *testing.T) {
	parent := newTestParent(t)
	defer destroyWindow(parent)

	var evMu sync.Mutex
	var events []map[string]any
	m := NewBrowserManager(uint32(pid()), "aicollab-ui-test-parent",
		t.TempDir(), func(p map[string]any) {
			evMu.Lock()
			events = append(events, p)
			evMu.Unlock()
		})
	m.parent = parent
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
}
