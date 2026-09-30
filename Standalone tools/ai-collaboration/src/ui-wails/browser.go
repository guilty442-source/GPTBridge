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
	goruntime "runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"github.com/wailsapp/go-webview2/pkg/edge"
)

const (
	opReplyTimeout = 30 * time.Second
	embedTimeout   = 15 * time.Second
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
	var err error
	var s *session
	m.run(func() {
		parent, perr := m.findParent()
		if perr != nil {
			err = perr
			return
		}
		hw, herr := createChildWindow(parent,
			scaleCoord(parent, b.X), scaleCoord(parent, b.Y),
			scaleCoord(parent, b.W), scaleCoord(parent, b.H))
		if herr != nil {
			err = herr
			return
		}
		cr := edge.NewChromium()
		_ = os.MkdirAll(m.dataDir, 0o755)
		cr.DataPath = m.dataDir
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
			case s.navWait <- true:
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
		cr.SetErrorCallback(func(error) {})
		if !cr.Embed(hw) {
			destroyWindow(hw)
			err = errors.New("WEBVIEW2_EMBED_FAILED")
			s = nil
			return
		}
		m.mu.Lock()
		m.sessions[id] = s
		m.mu.Unlock()
	})
	if err != nil {
		return nil, err
	}
	if !m.waitReady(s) {
		m.closeSession(id)
		return nil, errors.New("WEBVIEW2_INIT_TIMEOUT")
	}
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

func (m *BrowserManager) waitReady(s *session) bool {
	deadline := time.Now().Add(embedTimeout)
	for time.Now().Before(deadline) {
		if s.chromium.GetController() != nil {
			return true
		}
		time.Sleep(25 * time.Millisecond)
	}
	return false
}

func (m *BrowserManager) activate(id string) {
	m.mu.Lock()
	m.active = id
	var hidden []uintptr
	for otherID, s := range m.sessions {
		if otherID != id {
			hidden = append(hidden, s.hwnd)
		}
	}
	m.mu.Unlock()
	// Only one session is visible at a time — the activated one.
	m.post(func() {
		m.mu.Lock()
		for _, s := range m.sessions {
			_ = s.chromium.Hide()
		}
		m.mu.Unlock()
		for _, hw := range hidden {
			showWindow(hw, false)
		}
	})
}

func (m *BrowserManager) applyBounds(s *session, b bounds) {
	s.bounds = b
	if b.valid {
		m.post(func() { moveWindow(s.hwnd, b.X, b.Y, b.W, b.H) })
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
	m.post(func() { destroyWindow(s.hwnd) })
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
		s, err := m.ensureSession(id, url, toBounds(p))
		if err != nil {
			return map[string]any{"ok": false, "message": err.Error()}
		}
		return map[string]any{"ok": true, "id": s.id}
	case "embedded-browser:state":
		if s := m.lookup(id); s != nil {
			return map[string]any{"ok": true, "url": s.url,
				"loading":   s.loading,
				"canGoBack": true, "canGoForward": false}
		}
		return map[string]any{"ok": false}
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
			return map[string]any{"ok": true, "url": s.url}
		}
		return map[string]any{"ok": false}
	}
	return map[string]any{"ok": false, "message": "unknown channel: " + channel}
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
		s, err := m.ensureSession(sid, url, b)
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
	case <-wait:
		return map[string]any{"ok": true, "url": s.url}
	case <-time.After(opReplyTimeout):
		return failOp("NAVIGATE_TIMEOUT", "page load timed out")
	}
}

func (m *BrowserManager) opExec(s *session, script string) map[string]any {
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
	m.activate(s.id)
	select {
	case res := <-ch:
		if !res.ok {
			return failOp("EXEC_FAILED", res.err)
		}
		return map[string]any{"ok": true, "result": res.result}
	case <-time.After(opReplyTimeout):
		return failOp("EXEC_TIMEOUT", "script execution timed out")
	}
}
