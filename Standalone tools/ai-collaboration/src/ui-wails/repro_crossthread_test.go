//go:build windows

// repro_crossthread_test.go — reproduces the real app topology:
// a top-level parent window owned by a DIFFERENT thread (like the
// Wails main window) while sessions are children created on the
// pump thread.  If this fails with E_ABORT the same way the app
// does, cross-thread parenting is the root cause.
package main

import (
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
	"time"
	"unsafe"
)

// foreignParent runs a second UI thread that owns a top-level window
// and pumps its own message loop — mimicking the Wails main window.
func foreignParent(t *testing.T, title string) chan struct{} {
	t.Helper()
	done := make(chan struct{})
	ready := make(chan struct{})
	go func() {
		// This thread mimics the Wails GUI thread: owns the parent
		// window and pumps a real GetMessage loop.
		coInitialize()
		defer coUninitialize()
		if err := registerBrowserClass(); err != nil {
			t.Errorf("register class: %v", err)
			close(ready)
			return
		}
		name, _ := syscall.UTF16PtrFromString("AiCollabWebView2Host")
		cap16, _ := syscall.UTF16PtrFromString(title)
		hinst, _, _ := procGetModuleHandleW.Call(0)
		hwnd, _, _ := procCreateWindowExW.Call(
			0, uintptr(unsafe.Pointer(name)), uintptr(unsafe.Pointer(cap16)),
			wsPopupWindow, 0, 0, 640, 480, 0, 0, hinst, 0)
		if hwnd == 0 {
			t.Errorf("foreign parent create failed")
			close(ready)
			return
		}
		// findWindowByPIDAndTitle requires a visible window.
		_, _, _ = procShowWindow.Call(hwnd, swShow)
		close(ready)
		var m msg
		for {
			select {
			case <-done:
				_, _, _ = procPostThreadMessageW.Call(currentThreadID(), wmQuit, 0, 0)
			default:
			}
			r, _, _ := procGetMessageW.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
			if int32(r) <= 0 {
				destroyWindow(hwnd)
				return
			}
			_, _, _ = procTranslateMessage.Call(uintptr(unsafe.Pointer(&m)))
			_, _, _ = procDispatchMessageW.Call(uintptr(unsafe.Pointer(&m)))
		}
	}()
	<-ready
	return done
}

// TestDualEnvironmentRepro mimics the real app: one WebView2
// environment already hosting a webview (the Wails main window,
// user-data = env A), then the provider env (env B) creates a
// controller.  If env B aborts, dual-env in-process is the cause.
func TestDualEnvironmentRepro(t *testing.T) {
	dataRootA := filepath.Join(os.TempDir(),
		"aicollab-ui-dualA-"+strconv.Itoa(int(pid())))
	dataRootB := filepath.Join(os.TempDir(),
		"aicollab-ui-dualB-"+strconv.Itoa(int(pid())))
	mA := NewBrowserManager(uint32(pid()), "dual-parent", dataRootA, nil)
	defer mA.Shutdown()
	mB := NewBrowserManager(uint32(pid()), "dual-parent", dataRootB, nil)
	defer mB.Shutdown()

	var parent uintptr
	var perr error
	mA.run(func() { parent, perr = createPopupWindow() })
	if perr != nil || parent == 0 {
		t.Fatalf("create parent window: %v", perr)
	}
	mA.parent = parent
	mB.parent = parent
	defer func() { mA.run(func() { destroyWindow(parent) }) }()

	// Env A hosts a live session first (the "Wails" webview).
	res := mA.Invoke("embedded-browser:create", map[string]any{
		"id": "a1", "ownerModule": "test",
		"bounds": map[string]any{"x": 0, "y": 0, "width": 200, "height": 100},
	})
	wantOK(t, "createA", res)

	// Env B (the provider lane) tries its own session.
	res = mB.Invoke("embedded-browser:create", map[string]any{
		"id": "b1", "ownerModule": "test",
		"bounds": map[string]any{"x": 0, "y": 100, "width": 200, "height": 100},
	})
	wantOK(t, "createB", res)
}

func TestCrossThreadParentRepro(t *testing.T) {
	title := "aicollab-foreign-parent"
	done := foreignParent(t, title)
	defer close(done)
	time.Sleep(200 * time.Millisecond) // let the window register

	dataRoot := filepath.Join(os.TempDir(),
		"aicollab-ui-xthread-"+strconv.Itoa(int(pid())))
	m := NewBrowserManager(uint32(pid()), title, dataRoot, nil)
	defer m.Shutdown()

	res := m.Invoke("embedded-browser:create", map[string]any{
		"id": "xt1", "ownerModule": "test",
		"url":    "data:text/html,<title>xt</title><p>hi</p>",
		"bounds": map[string]any{"x": 0, "y": 0, "width": 320, "height": 200},
	})
	if ok, _ := res["ok"].(bool); !ok {
		t.Fatalf("create under foreign-thread parent failed: %v", res)
	}
}
