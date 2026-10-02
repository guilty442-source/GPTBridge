//go:build windows

// browser.go — embedded-browser session manager.  Each session is a
// WebView2 (edge.Chromium) embedded in a child HWND of the Wails main
// window, created on a dedicated pump thread (Win32 child windows are
// owned by their creating thread).  Semantics mirror the retired host
// bridge and the Qt WebEngine implementation: one shared profile,
// create/navigate/resize/show/hide/close plus backend-delegated DOM ops
// (ai_collab_browser_op → create/navigate/exec/url/close).
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	goruntime "runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"github.com/wailsapp/go-webview2/pkg/edge"
	"github.com/wailsapp/go-webview2/webviewloader"
)

const (
	opReplyTimeout = 30 * time.Second
	embedTimeout   = 90 * time.Second
)

type bounds struct {
	X, Y, W, H int32
	valid      bool
}

type execResult struct {
	ok     bool
	result any
	err    string
}

type session struct {
	id       string
	owner    string
	hwnd     uintptr
	chromium *edge.Chromium
	ctrlH    *ctrlCompletedHandler // keeps the COM handler alive
	bounds   bounds
	url      string
	loading  bool
	opMu     sync.Mutex // serialise navigate/exec per session

	navWait chan bool

	execSeq    uint64
	execWait   map[uint64]chan execResult
	execWaitMu sync.Mutex
}

// BrowserManager owns every WebView2 session for the tool window.
type BrowserManager struct {
	mu       sync.Mutex
	sessions map[string]*session
	active   string
	parent   uintptr
	pid      uint32
	title    string
	dataDir  string
	emit     func(map[string]any)

	jobs  chan func()
	tid   uintptr
	ready chan struct{}

	// ensureMu serialises session creation: a timed-out backend op leaves
	// ensureSession running (attach retry path), and a second create for
	// the same id must not race it.
	ensureMu sync.Mutex

	envMu      sync.Mutex
	envStarted bool
	env        *edge.ICoreWebView2Environment
	envErr     error
}

func NewBrowserManager(pid uint32, title, toolRoot string,
	emit func(map[string]any)) *BrowserManager {
	m := &BrowserManager{
		sessions: map[string]*session{},
		pid:      pid,
		title:    title,
		dataDir:  filepath.Join(toolRoot, "runtime", "webview2", "providers"),
		emit:     emit,
		jobs:     make(chan func(), 256),
		ready:    make(chan struct{}),
	}
	go m.pump()
	return m
}

func (m *BrowserManager) pump() {
	goruntime.LockOSThread()
	coInitialize()
	defer coUninitialize()
	m.tid = currentThreadID()
	forceMessageQueue()
	close(m.ready)
	pumpLoop(m.jobs)
}

// run schedules fn on the pump thread and waits for completion —
// only for work that does not itself wait on WebView2 callbacks.
func (m *BrowserManager) run(fn func()) {
	<-m.ready
	done := make(chan struct{})
	m.jobs <- func() { fn(); close(done) }
	postThreadJob(m.tid)
	<-done
}

// post enqueues fn on the pump thread and returns immediately —
// used for ops whose completion arrives asynchronously through
// WebView2 event callbacks.
func (m *BrowserManager) post(fn func()) {
	<-m.ready
	m.jobs <- fn
	postThreadJob(m.tid)
}

func (m *BrowserManager) findParent() (uintptr, error) {
	if m.parent != 0 {
		return m.parent, nil
	}
	hwnd := findWindowByPIDAndTitle(m.pid, m.title)
	if hwnd == 0 {
		return 0, errNoWindow
	}
	m.parent = hwnd
	return hwnd, nil
}

func (m *BrowserManager) emitEvent(id, typ string, extra map[string]any) {
	if m.emit == nil {
		return
	}
	p := map[string]any{"id": id, "type": typ}
	for k, v := range extra {
		p[k] = v
	}
	m.emit(p)
}

func (m *BrowserManager) lookup(id string) *session {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.sessions[id]
}

// navCompletionStatus reads IsSuccess/WebErrorStatus out of the
// COM event args (go-webview2 does not export the getters — the vtbl
// layout is stable COM ABI).
func navCompletionStatus(
	args *edge.ICoreWebView2NavigationCompletedEventArgs) (bool, int32) {
	type vtbl struct {
		_            [3]uintptr // IUnknown: QueryInterface, AddRef, Release
		getIsSuccess uintptr
		getStatus    uintptr
	}
	obj := (*struct{ vtbl *vtbl })(unsafe.Pointer(args))
	if obj == nil || obj.vtbl == nil {
		return false, 0
	}
	var ok int32
	_, _, _ = syscall.Syscall(obj.vtbl.getIsSuccess, 2,
		uintptr(unsafe.Pointer(args)),
		uintptr(unsafe.Pointer(&ok)), 0)
	var status int32
	_, _, _ = syscall.Syscall(obj.vtbl.getStatus, 2,
		uintptr(unsafe.Pointer(args)),
		uintptr(unsafe.Pointer(&status)), 0)
	return ok != 0, status
}

// WebView2 topology: one environment per manager, one controller per
// session.  Environments must NOT be shared beyond the pump thread that
// created them, and once the last controller on an environment is
// closed the browser process exits and the env goes zombie — a new
// controller then fails with 0x802A000C.  On that failure the env is
// discarded and recreated (env creation itself is repeatable; only
// go-webview2's Chromium.Embed path was unsafe because it checks
// GetLastError instead of the HRESULT).
type envCreateHandler struct {
	m *BrowserManager
}

func (h envCreateHandler) EnvironmentCompleted(code webviewloader.HRESULT,
	e *webviewloader.ICoreWebView2Environment) webviewloader.HRESULT {
	h.m.envMu.Lock()
	defer h.m.envMu.Unlock()
	if e != nil {
		// the loader Releases the env after this callback — keep ours.
		e.AddRef()
		h.m.env = (*edge.ICoreWebView2Environment)(unsafe.Pointer(e))
	} else {
		h.m.envErr = fmt.Errorf("webview2 environment failed: %08x",
			uint32(code))
	}
	return 0
}

// kickEnv starts environment creation for this manager.  Must run on
// the pump thread — the completion callback is dispatched through that
// thread's message queue.
func (m *BrowserManager) kickEnv() {
	m.envMu.Lock()
	if m.envStarted {
		m.envMu.Unlock()
		return
	}
	m.envStarted = true
	m.envMu.Unlock()
	_ = os.MkdirAll(m.dataDir, 0o755)
	webviewloader.CreateCoreWebView2EnvironmentWithOptions(
		envCreateHandler{m},
		webviewloader.WithUserDataFolder(m.dataDir))
}

// waitEnv blocks a non-pump goroutine until this manager's environment
// is ready; the pump thread keeps dispatching meanwhile.
func (m *BrowserManager) waitEnv(timeout time.Duration) (*edge.ICoreWebView2Environment, error) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		m.envMu.Lock()
		env, err := m.env, m.envErr
		m.envMu.Unlock()
		if env != nil || err != nil {
			return env, err
		}
		time.Sleep(25 * time.Millisecond)
	}
	return nil, errors.New("WEBVIEW2_ENV_TIMEOUT")
}

// invalidateEnv drops a zombie environment so the next session creates
// a fresh one.  Call when CreateCoreWebView2Controller rejects.
func (m *BrowserManager) invalidateEnv() {
	m.envMu.Lock()
	m.env = nil
	m.envErr = nil
	m.envStarted = false
	m.envMu.Unlock()
}

// setChromiumHwnd fills the unexported Chromium.hwnd (Resize and
// PutParentWindow read it; the field layout is stable in go-webview2
// v1.0.16).
func setChromiumHwnd(cr *edge.Chromium, hwnd uintptr) {
	f := reflect.ValueOf(cr).Elem().FieldByName("hwnd")
	reflect.NewAt(f.Type(), unsafe.Pointer(f.UnsafeAddr())).
		Elem().SetUint(uint64(hwnd))
}

// attachController replicates Chromium.EnvironmentCompleted —
// env.AddRef + CreateCoreWebView2Controller(hwnd, handler) — but checks
// the returned HRESULT instead of GetLastError.  go-webview2 tests
// `err` (last-error) after every COM call, so a stale win32 error from
// an earlier call kills the process via errorCallback → os.Exit even
// though controller creation succeeded (verified live: hr==S_OK).
//
// The handler passed here is our own ctrlCompletedHandler, NOT
// Chromium.controllerCompleted: the stock implementation routes
// async failures (e.g. E_ABORT) through errorCallback which ends in
// os.Exit(1), killing the whole tool window before the retry path
// can run.  Ours reports the failure on a channel instead and
// delegates only the success path back to the Chromium method.
func attachController(cr *edge.Chromium,
	env *edge.ICoreWebView2Environment, hwnd uintptr,
	handler *ctrlCompletedHandler) error {
	type envVtbl struct {
		qi               uintptr
		addRef           uintptr
		release          uintptr
		createController uintptr
	}
	obj := (*struct{ vtbl *envVtbl })(unsafe.Pointer(env))
	if obj == nil || obj.vtbl == nil || obj.vtbl.createController == 0 {
		return errors.New("WEBVIEW2_ENV_VTBL_INVALID")
	}
	// IUnknown::AddRef — EnvironmentCompleted does the same so the env
	// outlives this Chromium.
	_, _, _ = syscall.Syscall(obj.vtbl.addRef, 1,
		uintptr(unsafe.Pointer(env)), 0, 0)
	hr, _, _ := syscall.SyscallN(obj.vtbl.createController,
		uintptr(unsafe.Pointer(env)), hwnd, uintptr(unsafe.Pointer(handler)))
	if int32(hr) < 0 {
		return fmt.Errorf("WEBVIEW2_CONTROLLER_FAILED %08x", uint32(hr))
	}
	return nil
}

// ---------------- safe controller-completed COM handler ----------------
//
// COM layout identical to go-webview2's
// iCoreWebView2CreateCoreWebView2ControllerCompletedHandler:
// {vtbl, impl} where vtbl is [QI, AddRef, Release, Invoke].
// CreateCoreWebView2Controller stores the handler pointer and calls
// Invoke asynchronously on the creating thread's message pump.

type ctrlCompletedImpl struct {
	cr    *edge.Chromium
	errCh chan error
}

func (i *ctrlCompletedImpl) queryInterface(_, _ uintptr) uintptr { return 0 }
func (i *ctrlCompletedImpl) addRef() uintptr                     { return 1 }
func (i *ctrlCompletedImpl) release() uintptr                    { return 1 }

// invoke matches ICoreWebView2CreateCoreWebView2ControllerCompletedHandler.
// Failure: report on errCh and return S_OK — the caller retries with a
// fresh environment.  Success: delegate to the Chromium method which does
// the full controller/webview/event-handler wiring itself.
func (i *ctrlCompletedImpl) invoke(res uintptr,
	c *edge.ICoreWebView2Controller) uintptr {
	if int32(res) < 0 || c == nil {
		select {
		case i.errCh <- fmt.Errorf(
			"WEBVIEW2_CONTROLLER_FAILED %08x", uint32(res)):
		default:
		}
		return 0
	}
	return i.cr.CreateCoreWebView2ControllerCompleted(res, c)
}

type iunknownVtbl struct{ qi, addRef, release uintptr }

type ctrlCompletedVtbl struct {
	iunknownVtbl
	invoke uintptr
}

type ctrlCompletedHandler struct {
	vtbl *ctrlCompletedVtbl
	impl *ctrlCompletedImpl
}

func newCtrlCompletedHandler(cr *edge.Chromium) (*ctrlCompletedHandler, chan error) {
	errCh := make(chan error, 1)
	h := &ctrlCompletedHandler{impl: &ctrlCompletedImpl{cr: cr, errCh: errCh}}
	h.vtbl = &ctrlCompletedVtbl{
		iunknownVtbl: iunknownVtbl{
			qi: syscall.NewCallback(
				func(this *ctrlCompletedHandler, refiid, object uintptr) uintptr {
					return this.impl.queryInterface(refiid, object)
				}),
			addRef: syscall.NewCallback(
				func(this *ctrlCompletedHandler) uintptr {
					return this.impl.addRef()
				}),
			release: syscall.NewCallback(
				func(this *ctrlCompletedHandler) uintptr {
					return this.impl.release()
				}),
		},
		invoke: syscall.NewCallback(
			func(this *ctrlCompletedHandler, res uintptr,
				c *edge.ICoreWebView2Controller) uintptr {
				return this.impl.invoke(res, c)
			}),
	}
	return h, errCh
}

// ensureSession creates the session webview on the pump thread when
// absent, waits for the controller, and navigates to url when given.
func (m *BrowserManager) ensureSession(id, owner, url string,
	b bounds) (*session, error) {
	if s := m.lookup(id); s != nil {
		if url != "" {
			m.post(func() { m.navigateNow(s, url) })
		}
		return s, nil
	}
	m.ensureMu.Lock()
	defer m.ensureMu.Unlock()
	// Re-check after taking the lock — a racing creator may have finished.
	if s := m.lookup(id); s != nil {
		if url != "" {
			m.post(func() { m.navigateNow(s, url) })
		}
		return s, nil
	}
	var err error
	var s *session
	var hw uintptr
	cr := edge.NewChromium()
	m.run(func() {
		parent, perr := m.findParent()
		if perr != nil {
			err = perr
			return
		}
		hw, err = createChildWindow(parent,
			scaleCoord(parent, b.X), scaleCoord(parent, b.Y),
			scaleCoord(parent, b.W), scaleCoord(parent, b.H))
		if err != nil {
			return
		}
		m.kickEnv()
	})
	if err != nil {
		return nil, err
	}
	env, err := m.waitEnv(embedTimeout)
	if err != nil {
		m.run(func() { destroyWindow(hw) })
		return nil, err
	}
	s = &session{id: id, owner: owner, hwnd: hw, chromium: cr, bounds: b,
		navWait:  make(chan bool, 1),
		execWait: map[uint64]chan execResult{}}
	cr.MessageCallback = func(message string) {
		m.onWebMessage(s, message)
	}
	cr.NavigationCompletedCallback = func(_ *edge.ICoreWebView2,
		args *edge.ICoreWebView2NavigationCompletedEventArgs) {
		ok, status := navCompletionStatus(args)
		s.loading = false
		select {
		case s.navWait <- ok:
		default:
		}
		if ok {
			m.emitEvent(id, "loading-stop", nil)
			m.emitEvent(id, "navigate", map[string]any{"url": s.url})
		} else {
			m.emitEvent(id, "load-failed", map[string]any{
				"error":     "navigation failed",
				"errorCode": status,
			})
		}
	}
	cr.SetErrorCallback(func(err error) {
		fmt.Fprintf(os.Stderr, "webview2 error (session %s): %v\n", id, err)
	})
	// If the previous session closed the environment's last controller,
	// the browser process exits and the env goes zombie
	// (0x802A000C) — discard it, create a fresh env, retry.  Async
	// controller failures (E_ABORT and friends) arrive through the
	// completion handler's errCh, observed by waitReady.
	for attempt := 0; attempt < 3; attempt++ {
		var handlerErr chan error
		m.run(func() {
			setChromiumHwnd(cr, hw)
			h, ch := newCtrlCompletedHandler(cr)
			s.ctrlH = h
			handlerErr = ch
			err = attachController(cr, env, hw, h)
		})
		if err == nil {
			// async completion: success registers the controller,
			// failure arrives on handlerErr (no process exit).
			if werr := m.waitReady(s, handlerErr); werr != nil {
				err = werr
			} else {
				break
			}
		}
		m.invalidateEnv()
		m.run(func() { m.kickEnv() })
		env, err = m.waitEnv(embedTimeout)
		if err != nil {
			break
		}
	}
	if err != nil {
		m.run(func() { destroyWindow(hw) })
		return nil, err
	}
	m.mu.Lock()
	m.sessions[id] = s
	m.mu.Unlock()
	m.post(func() {
		cr.Init("window.external={invoke:s=>window.chrome.webview.postMessage(s)}")
		cr.Resize()
	})
	if b.valid {
		m.applyBounds(s, b)
		m.post(func() {
			_ = s.chromium.Show()
			showWindow(s.hwnd, true)
		})
	}
	if url != "" {
		m.post(func() { m.navigateNow(s, url) })
	}
	m.activate(id)
	return s, nil
}

func (m *BrowserManager) waitReady(s *session, errCh chan error) error {
	deadline := time.Now().Add(embedTimeout)
	for time.Now().Before(deadline) {
		select {
		case err := <-errCh:
			return err
		default:
		}
		if s.chromium.GetController() != nil {
			return nil
		}
		time.Sleep(25 * time.Millisecond)
	}
	return errors.New("WEBVIEW2_INIT_TIMEOUT")
}

func (m *BrowserManager) activate(id string) {
	m.mu.Lock()
	m.active = id
	m.mu.Unlock()
	// Only one session is visible at a time — the activated one.
	m.post(func() {
		m.mu.Lock()
		for otherID, s := range m.sessions {
			if otherID == id {
				_ = s.chromium.Show()
				showWindow(s.hwnd, true)
			} else {
				_ = s.chromium.Hide()
				showWindow(s.hwnd, false)
			}
		}
		m.mu.Unlock()
	})
}

func (m *BrowserManager) applyBounds(s *session, b bounds) {
	s.bounds = b
	if b.valid {
		m.post(func() {
			moveWindow(s.hwnd, scaleCoord(m.parent, b.X),
				scaleCoord(m.parent, b.Y), scaleCoord(m.parent, b.W),
				scaleCoord(m.parent, b.H))
			// MoveWindow only resizes the host HWND — the WebView2
			// controller keeps its own bounds, so it must be pushed
			// explicitly or the webview keeps rendering at the old
			// (possibly 0x0) size.
			s.chromium.Resize()
		})
	}
}

func (m *BrowserManager) navigateNow(s *session, url string) {
	s.url = url
	s.loading = true
	m.emitEvent(s.id, "loading-start", nil)
	s.chromium.Navigate(url)
}

func (m *BrowserManager) onWebMessage(s *session, message string) {
	var frame struct {
		Exec   uint64 `json:"__aicollab_exec"`
		OK     bool   `json:"ok"`
		Result any    `json:"result"`
		Error  string `json:"error"`
	}
	if err := json.Unmarshal([]byte(message), &frame); err != nil ||
		frame.Exec == 0 {
		return
	}
	s.execWaitMu.Lock()
	ch := s.execWait[frame.Exec]
	delete(s.execWait, frame.Exec)
	s.execWaitMu.Unlock()
	if ch != nil {
		ch <- execResult{ok: frame.OK, result: frame.Result, err: frame.Error}
	}
}

func (m *BrowserManager) closeSession(id string) {
	m.mu.Lock()
	s := m.sessions[id]
	delete(m.sessions, id)
	if m.active == id {
		m.active = ""
	}
	m.mu.Unlock()
	if s == nil {
		return
	}
	m.post(func() {
		closeController(s.chromium.GetController())
		destroyWindow(s.hwnd)
	})
}

// closeController invokes ICoreWebView2Controller::Close through the
// COM vtbl (go-webview2 does not export it — index 23 in the stable
// COM ABI).  Without Close the browser resources stay attached to the
// destroyed window and the webview keeps a dead host.
func closeController(c *edge.ICoreWebView2Controller) {
	if c == nil {
		return
	}
	type vtbl struct {
		_     [23]uintptr // IUnknown + members preceding Close
		close uintptr
	}
	obj := (*struct{ vtbl *vtbl })(unsafe.Pointer(c))
	if obj == nil || obj.vtbl == nil || obj.vtbl.close == 0 {
		return
	}
	_, _, _ = syscall.Syscall(obj.vtbl.close, 1,
		uintptr(unsafe.Pointer(c)), 0, 0)
}

// Shutdown destroys every session (window teardown → process exit).
func (m *BrowserManager) Shutdown() {
	m.mu.Lock()
	ids := make([]string, 0, len(m.sessions))
	for id := range m.sessions {
		ids = append(ids, id)
	}
	m.mu.Unlock()
	for _, id := range ids {
		m.closeSession(id)
	}
}

// --------------- frontend embedded-browser:* surface ----------------

func toBounds(p map[string]any) bounds {
	raw, ok := p["bounds"].(map[string]any)
	if !ok {
		return bounds{}
	}
	num := func(k string) int32 {
		if v, ok := raw[k].(float64); ok {
			return int32(v)
		}
		return 0
	}
	b := bounds{X: num("x"), Y: num("y"), W: num("width"), H: num("height")}
	b.valid = b.W > 0 && b.H > 0
	return b
}

func (m *BrowserManager) Invoke(channel string, p map[string]any) map[string]any {
	id, _ := p["id"].(string)
	switch channel {
	case "embedded-browser:create":
		url, _ := p["url"].(string)
		owner, _ := p["ownerModule"].(string)
		s, err := m.ensureSession(id, owner, url, toBounds(p))
		if err != nil {
			return map[string]any{"ok": false, "message": err.Error()}
		}
		return map[string]any{"ok": true, "id": s.id}
	case "embedded-browser:state":
		if s := m.lookup(id); s != nil {
			return m.sessionState(s)
		}
		return map[string]any{"ok": false}
	case "embedded-browser:execute":
		script, _ := p["script"].(string)
		if s := m.lookup(id); s != nil && script != "" {
			res := m.opExec(s, script)
			if ok, _ := res["ok"].(bool); !ok {
				return map[string]any{"ok": false,
					"message": res["message"]}
			}
			return map[string]any{"ok": true, "result": res["result"]}
		}
		return map[string]any{"ok": false, "message": "SESSION_NOT_FOUND"}
	case "embedded-browser:list":
		m.mu.Lock()
		ids := make([]string, 0, len(m.sessions))
		for k := range m.sessions {
			ids = append(ids, k)
		}
		m.mu.Unlock()
		return map[string]any{"ok": true, "sessions": ids}
	case "embedded-browser:close-module":
		owner, _ := p["ownerModule"].(string)
		m.mu.Lock()
		var ids []string
		for k, s := range m.sessions {
			if s.owner == owner {
				ids = append(ids, k)
			}
		}
		m.mu.Unlock()
		for _, sid := range ids {
			m.closeSession(sid)
		}
		return map[string]any{"ok": true, "closed": len(ids)}
	case "embedded-browser:navigate":
		url, _ := p["url"].(string)
		if s := m.lookup(id); s != nil && url != "" {
			m.post(func() { m.navigateNow(s, url) })
			m.activate(id)
			return map[string]any{"ok": true}
		}
		return map[string]any{"ok": false}
	case "embedded-browser:resize":
		if s := m.lookup(id); s != nil {
			m.applyBounds(s, toBounds(p))
			return map[string]any{"ok": true}
		}
		return map[string]any{"ok": false}
	case "embedded-browser:show":
		if s := m.lookup(id); s != nil {
			m.activate(id)
			m.post(func() {
				_ = s.chromium.Show()
				showWindow(s.hwnd, true)
			})
			return map[string]any{"ok": true}
		}
		return map[string]any{"ok": false}
	case "embedded-browser:hide":
		if s := m.lookup(id); s != nil {
			m.post(func() {
				_ = s.chromium.Hide()
				showWindow(s.hwnd, false)
			})
			return map[string]any{"ok": true}
		}
		return map[string]any{"ok": false}
	case "embedded-browser:close":
		m.closeSession(id)
		return map[string]any{"ok": true}
	case "embedded-browser:reload":
		if s := m.lookup(id); s != nil {
			m.post(func() { s.chromium.Eval("location.reload()") })
			return map[string]any{"ok": true}
		}
		return map[string]any{"ok": false}
	case "embedded-browser:go-back":
		if s := m.lookup(id); s != nil {
			m.post(func() { s.chromium.Eval("history.back()") })
			return map[string]any{"ok": true}
		}
		return map[string]any{"ok": false}
	case "embedded-browser:go-forward":
		if s := m.lookup(id); s != nil {
			m.post(func() { s.chromium.Eval("history.forward()") })
			return map[string]any{"ok": true}
		}
		return map[string]any{"ok": false}
	case "embedded-browser:url":
		if s := m.lookup(id); s != nil {
			res := m.opEvalValue(s, "location.href")
			if v, _ := res["result"].(string); v != "" {
				return map[string]any{"ok": true, "url": v}
			}
			return map[string]any{"ok": true, "url": s.url}
		}
		return map[string]any{"ok": false}
	}
	return map[string]any{"ok": false, "message": "unknown channel: " + channel}
}

// sessionState returns live url/history flags straight from the page —
// the eval pipeline doubles as a liveness probe for crashed sessions.
func (m *BrowserManager) sessionState(s *session) map[string]any {
	res := m.opEvalValue(s,
		`JSON.stringify({u:location.href,b:!!(navigation&&navigation.canGoBack),f:!!(navigation&&navigation.canGoForward)})`)
	out := map[string]any{"ok": true, "url": s.url, "loading": s.loading,
		"canGoBack": false, "canGoForward": false}
	if raw, _ := res["result"].(string); raw != "" {
		var live struct {
			U string `json:"u"`
			B bool   `json:"b"`
			F bool   `json:"f"`
		}
		if json.Unmarshal([]byte(raw), &live) == nil {
			out["url"] = live.U
			out["canGoBack"] = live.B
			out["canGoForward"] = live.F
		}
	}
	return out
}

// --------------- backend ai_collab_browser_op surface ---------------

func failOp(code, msg string) map[string]any {
	if msg == "" {
		msg = code
	}
	return map[string]any{"ok": false, "error_code": code, "message": msg}
}

// DOMOp executes one backend-delegated op (op_id handled by caller).
func (m *BrowserManager) DOMOp(p map[string]any) map[string]any {
	op, _ := p["op"].(string)
	sid, _ := p["session_id"].(string)
	url, _ := p["url"].(string)
	script, _ := p["script"].(string)
	if sid == "" {
		return failOp("SESSION_REQUIRED", "session_id is required")
	}
	switch op {
	case "create":
		// Backend sessions render into the last-known canvas bounds.
		m.mu.Lock()
		var b bounds
		for _, s := range m.sessions {
			if s.bounds.valid {
				b = s.bounds
				break
			}
		}
		m.mu.Unlock()
		s, err := m.ensureSession(sid, toolID, url, b)
		if err != nil {
			return failOp("SESSION_CREATE_FAILED", err.Error())
		}
		return map[string]any{"ok": true, "id": s.id,
			"backend": "wails-webview2"}
	}
	s := m.lookup(sid)
	if s == nil {
		return failOp("SESSION_NOT_FOUND", "session not found")
	}
	switch op {
	case "navigate":
		if url == "" {
			return failOp("INVALID_URL", "url is required")
		}
		return m.opNavigate(s, url)
	case "exec":
		if script == "" {
			return failOp("INVALID_SCRIPT", "script is required")
		}
		return m.opExec(s, script)
	case "url":
		return map[string]any{"ok": true, "url": s.url}
	case "close":
		m.closeSession(sid)
		return map[string]any{"ok": true}
	}
	return failOp("UNSUPPORTED_OP", "unsupported op: "+op)
}

func (m *BrowserManager) opNavigate(s *session, url string) map[string]any {
	s.opMu.Lock()
	defer s.opMu.Unlock()
	wait := make(chan bool, 1)
	m.post(func() {
		s.navWait = wait
		m.navigateNow(s, url)
	})
	m.activate(s.id)
	select {
	case ok := <-wait:
		if !ok {
			return failOp("NAVIGATE_FAILED", "page load failed")
		}
		return map[string]any{"ok": true, "url": s.url}
	case <-time.After(opReplyTimeout):
		return failOp("NAVIGATE_TIMEOUT", "page load timed out")
	}
}

func (m *BrowserManager) opExec(s *session, script string) map[string]any {
	return m.execScript(s, script, true)
}

// opEvalValue evaluates a snippet without activating the session —
// used by the periodic state refresh on hidden sessions.
func (m *BrowserManager) opEvalValue(s *session,
	script string) map[string]any {
	return m.execScript(s, script, false)
}

func (m *BrowserManager) execScript(s *session, script string,
	activate bool) map[string]any {
	s.opMu.Lock()
	defer s.opMu.Unlock()
	seq := atomic.AddUint64(&s.execSeq, 1)
	ch := make(chan execResult, 1)
	s.execWaitMu.Lock()
	s.execWait[seq] = ch
	s.execWaitMu.Unlock()
	encoded, _ := json.Marshal(script)
	wrapped := fmt.Sprintf(`(function(){try{var __r=eval(%s);var __f=function(v){try{var __s=JSON.stringify({__aicollab_exec:%d,ok:true,result:(v===undefined?null:v)});chrome.webview.postMessage(__s)}catch(e){chrome.webview.postMessage(JSON.stringify({__aicollab_exec:%d,ok:false,error:String(e)}))}};if(__r&&typeof __r.then==="function"){__r.then(__f,function(e){chrome.webview.postMessage(JSON.stringify({__aicollab_exec:%d,ok:false,error:String(e)}))})}else{__f(__r)}}catch(e){chrome.webview.postMessage(JSON.stringify({__aicollab_exec:%d,ok:false,error:String(e)}))}})()`,
		string(encoded), seq, seq, seq, seq)
	m.post(func() { s.chromium.Eval(wrapped) })
	if activate {
		m.activate(s.id)
	}
	select {
	case res := <-ch:
		// TEMP-DEBUG: dump every exec result for live diagnosis.
		ident := script
		if len(ident) > 48 {
			ident = ident[:48]
		}
		rj, _ := json.Marshal(res.result)
		if len(rj) > 1600 {
			rj = rj[:1600]
		}
		fmt.Fprintf(os.Stderr, "exec-debug ok=%v err=%q script=%.48q result=%s\n",
			res.ok, res.err, ident, string(rj))
		if !res.ok {
			return failOp("EXEC_FAILED", res.err)
		}
		return map[string]any{"ok": true, "result": res.result}
	case <-time.After(opReplyTimeout):
		return failOp("EXEC_TIMEOUT", "script execution timed out")
	}
}
