//go:build windows

// win32.go — minimal user32/ole32 bindings for the embedded-browser
// session manager.  Child HWNDs live on a dedicated pump thread so the
// Wails main-window message loop is never touched.
package main

import (
	"errors"
	"syscall"
	"unsafe"
)

var (
	user32   = syscall.NewLazyDLL("user32.dll")
	ole32    = syscall.NewLazyDLL("ole32.dll")
	kernel32 = syscall.NewLazyDLL("kernel32.dll")

	procEnumWindows              = user32.NewProc("EnumWindows")
	procGetWindowThreadProcessId = user32.NewProc("GetWindowThreadProcessId")
	procGetWindowTextLengthW     = user32.NewProc("GetWindowTextLengthW")
	procGetWindowTextW           = user32.NewProc("GetWindowTextW")
	procIsWindowVisible          = user32.NewProc("IsWindowVisible")
	procCreateWindowExW          = user32.NewProc("CreateWindowExW")
	procDestroyWindow            = user32.NewProc("DestroyWindow")
	procMoveWindow               = user32.NewProc("MoveWindow")
	procShowWindow               = user32.NewProc("ShowWindow")
	procRegisterClassExW         = user32.NewProc("RegisterClassExW")
	procDefWindowProcW           = user32.NewProc("DefWindowProcW")
	procGetModuleHandleW         = kernel32.NewProc("GetModuleHandleW")
	procGetMessageW              = user32.NewProc("GetMessageW")
	procPeekMessageW             = user32.NewProc("PeekMessageW")
	procTranslateMessage         = user32.NewProc("TranslateMessage")
	procDispatchMessageW         = user32.NewProc("DispatchMessageW")
	procPostThreadMessageW       = user32.NewProc("PostThreadMessageW")
	procGetCurrentThreadId       = kernel32.NewProc("GetCurrentThreadId")
	procCoInitializeEx           = ole32.NewProc("CoInitializeEx")
	procCoUninitialize           = ole32.NewProc("CoUninitialize")
	procGetDpiForWindow          = user32.NewProc("GetDpiForWindow")
)

const (
	wsChild     = 0x40000000
	wsVisible   = 0x10000000
	wsClipChild = 0x02000000
	swHide      = 0
	swShow      = 5
	pmRemove    = 0x0001
	pmNoRemove  = 0x0000
	wmQuit      = 0x0012
	wmUser      = 0x0400
	wmRunJob    = 0x0400 + 0x42
	// COINIT_APARTMENTTHREADED — WebView2 callbacks are delivered
	// through the creating thread's message loop.
	coinitApartment = 0x2
)

type wndClassExW struct {
	Size       uint32
	Style      uint32
	WndProc    uintptr
	ClsExtra   int32
	WndExtra   int32
	Instance   uintptr
	Icon       uintptr
	Cursor     uintptr
	Background uintptr
	MenuName   *uint16
	ClassName  *uint16
	IconSm     uintptr
}

type msg struct {
	Hwnd    uintptr
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      struct{ X, Y int32 }
}

func coInitialize() {
	_, _, _ = procCoInitializeEx.Call(0, coinitApartment)
}
func coUninitialize() {
	_, _, _ = procCoUninitialize.Call()
}

func currentThreadID() uintptr {
	r, _, _ := procGetCurrentThreadId.Call()
	return r
}

// findWindowByPIDAndTitle returns the first visible top-level HWND
// owned by pid whose title matches exactly.
func findWindowByPIDAndTitle(pid uint32, title string) uintptr {
	var found uintptr
	cb := syscall.NewCallback(func(hwnd uintptr, _ uintptr) uintptr {
		var wpid uint32
		_, _, _ = procGetWindowThreadProcessId.Call(hwnd, uintptr(unsafe.Pointer(&wpid)))
		if wpid != pid {
			return 1
		}
		vis, _, _ := procIsWindowVisible.Call(hwnd)
		if vis == 0 {
			return 1
		}
		n, _, _ := procGetWindowTextLengthW.Call(hwnd)
		if n == 0 {
			return 1
		}
		buf := make([]uint16, n+1)
		_, _, _ = procGetWindowTextW.Call(hwnd,
			uintptr(unsafe.Pointer(&buf[0])), n+1)
		if syscall.UTF16ToString(buf[:n]) == title {
			found = hwnd
			return 0
		}
		return 1
	})
	_, _, _ = procEnumWindows.Call(cb, 0)
	return found
}

var browserClassRegistered bool

func registerBrowserClass() error {
	if browserClassRegistered {
		return nil
	}
	name, err := syscall.UTF16PtrFromString("AiCollabWebView2Host")
	if err != nil {
		return err
	}
	hinst, _, _ := procGetModuleHandleW.Call(0)
	wc := wndClassExW{
		Size:      uint32(unsafe.Sizeof(wndClassExW{})),
		WndProc:   procDefWindowProcW.Addr(),
		Instance:  hinst,
		ClassName: name,
	}
	r, _, e := procRegisterClassExW.Call(uintptr(unsafe.Pointer(&wc)))
	if r == 0 {
		return e
	}
	browserClassRegistered = true
	return nil
}

const wsPopupToplevel = 0x80000000

// createPopupHost creates a pump-thread-owned WS_POPUP window — same
// ownership topology as the passing live tests (experiment for the
// E_ABORT under a foreign-thread WS_CHILD parent).
func createPopupHost() (uintptr, error) {
	if err := registerBrowserClass(); err != nil {
		return 0, err
	}
	name, _ := syscall.UTF16PtrFromString("AiCollabWebView2Host")
	cap16, _ := syscall.UTF16PtrFromString("ai-collab-browser")
	hinst, _, _ := procGetModuleHandleW.Call(0)
	hwnd, _, err := procCreateWindowExW.Call(
		0, uintptr(unsafe.Pointer(name)), uintptr(unsafe.Pointer(cap16)),
		wsPopupToplevel|wsVisible, 100, 100, 800, 600, 0, 0, hinst, 0)
	if hwnd == 0 {
		return 0, err
	}
	return hwnd, nil
}

func createChildWindow(parent uintptr, x, y, w, h int32) (uintptr, error) {
	if err := registerBrowserClass(); err != nil {
		return 0, err
	}
	name, _ := syscall.UTF16PtrFromString("AiCollabWebView2Host")
	hinst, _, _ := procGetModuleHandleW.Call(0)
	r, _, e := procCreateWindowExW.Call(
		0,
		uintptr(unsafe.Pointer(name)),
		0,
		wsChild|wsClipChild,
		uintptr(int64(x)), uintptr(int64(y)),
		uintptr(int64(w)), uintptr(int64(h)),
		parent, 0, hinst, 0)
	if r == 0 {
		return 0, e
	}
	return r, nil
}

// scaleCoord converts renderer CSS-pixel coordinates into physical
// window coordinates — the process is system-DPI-aware (wails calls
// SetProcessDPIAware), so CSS px map 1:1 onto device px × DPI/96.
func scaleCoord(hwnd uintptr, css int32) int32 {
	if hwnd == 0 {
		return css
	}
	dpi, _, _ := procGetDpiForWindow.Call(hwnd)
	if dpi <= 96 {
		return css
	}
	return int32((int64(css)*int64(dpi) + 48) / 96)
}

func moveWindow(hwnd uintptr, x, y, w, h int32) {
	_, _, _ = procMoveWindow.Call(hwnd,
		uintptr(int64(x)), uintptr(int64(y)),
		uintptr(int64(w)), uintptr(int64(h)), 1)
}

func showWindow(hwnd uintptr, visible bool) {
	sw := uintptr(swHide)
	if visible {
		sw = swShow
	}
	_, _, _ = procShowWindow.Call(hwnd, sw)
}

func destroyWindow(hwnd uintptr) {
	_, _, _ = procDestroyWindow.Call(hwnd)
}

func postThreadJob(tid uintptr) {
	_, _, _ = procPostThreadMessageW.Call(tid, wmRunJob, 0, 0)
}

// forceMessageQueue materialises the calling thread's message queue —
// PostThreadMessage fails silently until the queue exists.
func forceMessageQueue() {
	var m msg
	_, _, _ = procPeekMessageW.Call(uintptr(unsafe.Pointer(&m)), 0,
		wmUser, wmUser, pmNoRemove)
}

// pumpLoop is the session thread's message loop: WM_RUN_JOB drains the
// jobs queue; every other message feeds the child HWNDs/WebView2.
func pumpLoop(jobs chan func()) {
	var m msg
	for {
		r, _, _ := procGetMessageW.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		// GetMessage returns -1 on error and 0 on WM_QUIT.
		if int32(r) <= 0 {
			return
		}
		if m.Message == wmRunJob {
			for {
				select {
				case job := <-jobs:
					job()
				default:
					goto drained
				}
			}
		drained:
			continue
		}
		_, _, _ = procTranslateMessage.Call(uintptr(unsafe.Pointer(&m)))
		_, _, _ = procDispatchMessageW.Call(uintptr(unsafe.Pointer(&m)))
	}
}

var errNoWindow = errors.New("MAIN_WINDOW_NOT_FOUND")
