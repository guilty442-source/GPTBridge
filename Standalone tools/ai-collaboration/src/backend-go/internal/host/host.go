// Package host is the governed tool host for ai-collaboration:
// /health /metrics /shutdown HTTP surface plus the token/instance-
// authenticated WebSocket command path — parity with ToolHostServer.
// The browser is owned by the tool window; backend→window DOM ops are
// delegated over the same WS via the ai_collab_browser_op event and
// its ai_collab_browser_op_result reply command.
package host

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"gptbridge.local/ai-collaboration-backend/internal/govenv"
	"gptbridge.local/ai-collaboration-backend/internal/proxy"
	"gptbridge.local/ai-collaboration-backend/internal/service"
	"gptbridge.local/ai-collaboration-backend/internal/wsproto"
)

// BrowserOpTimeout bounds one delegated DOM op.  Must exceed the UI's
// session-creation budget (embedTimeout = 90s) so a cold WebView2
// environment build does not surface as an op timeout while the UI is
// still legitimately attaching.
const BrowserOpTimeout = 120 * time.Second

// Host owns the governed tool backend.
type Host struct {
	env     *govenv.Environment
	service *service.Service
	version string

	listener net.Listener
	http     *http.Server
	shutdown chan struct{}
	shOnce   sync.Once
	started  time.Time

	mu      sync.Mutex
	clients map[*wsproto.Conn]struct{}
	primary *wsproto.Conn // most recently connected UI socket

	opMu      sync.Mutex
	opPending map[string]chan map[string]any

	rejected      atomic.Int64
	inflight      sync.Map // request_id -> chan struct{} (cancellation)
	transport     *proxy.Client
	channelHealth sync.Map     // channel -> *ChannelHealth
	lastNotif     atomic.Value // map[string]any
}

// ChannelHealth mirrors ChannelHealth.AsJson.
type ChannelHealth struct {
	ChannelID           string
	LastOK              bool
	LastRequestAt       string
	ConsecutiveFailures int
}

func (h *ChannelHealth) Degraded() bool { return h.ConsecutiveFailures >= 3 }

func (h *ChannelHealth) asJSON() map[string]any {
	return map[string]any{
		"channel_id": h.ChannelID, "last_ok": h.LastOK,
		"last_request_at":      h.LastRequestAt,
		"consecutive_failures": h.ConsecutiveFailures,
		"degraded":             h.Degraded(),
	}
}

// New builds the host around the governed env; the service is attached
// later via AttachService (the service depends on the WSBrowserOps
// bridge which needs the host).
func New(env *govenv.Environment, svc *service.Service, version string) *Host {
	h := &Host{
		env: env, service: svc, version: version,
		shutdown:  make(chan struct{}),
		clients:   map[*wsproto.Conn]struct{}{},
		opPending: map[string]chan map[string]any{},
		started:   time.Now(),
	}
	return h
}

// AttachService binds the command executor after construction.
func (h *Host) AttachService(svc *service.Service) {
	h.service = svc
}

// ShutdownToken signals cooperative shutdown.
func (h *Host) ShutdownToken() <-chan struct{} { return h.shutdown }

// RequestShutdown initiates shutdown.
func (h *Host) RequestShutdown() {
	h.shOnce.Do(func() {
		close(h.shutdown)
		if h.listener != nil {
			_ = h.listener.Close()
		}
	})
}

// Start binds the loopback listener and serves until shutdown.
func (h *Host) Start() error {
	ln, err := net.Listen("tcp4", fmt.Sprintf("127.0.0.1:%d", h.env.Port))
	if err != nil {
		return err
	}
	h.listener = ln
	mux := http.NewServeMux()
	mux.HandleFunc("/health", h.handleHealth)
	mux.HandleFunc("/metrics", h.handleMetrics)
	mux.HandleFunc("/shutdown", h.handleShutdown)
	mux.HandleFunc("/", h.handleWS)
	h.http = &http.Server{Handler: mux}
	go func() { _ = h.http.Serve(ln) }()
	go h.workerLoop()
	return nil
}

// ---------------- HTTP surface ----------------

func (h *Host) healthSnapshot() map[string]any {
	channels := map[string]any{}
	h.channelHealth.Range(func(k, v any) bool {
		channels[k.(string)] = v.(*ChannelHealth).asJSON()
		return true
	})
	return map[string]any{
		"ok": true, "role": "governed-tool-runtime",
		"sovereign_id": "main-system", "authority": "sub-sovereign-tool",
		"scope": "tool-local", "duty": []string{"execute-governed-commands"},
		"subordinate_to": []string{"main-system"},
		"version":        h.version, "tool_id": h.env.ToolID,
		"runtime_scope":         "independent-tool",
		"governance_ready":      true,
		"workspace_instance_id": h.env.WorkspaceInstanceID(),
		"channels":              []string{"system"},
		"channel_routes":        map[string]any{"system": "system-channel/" + h.env.ToolID},
		"channel_health":        channels,
		"runtime_host":          "go-toolhost",
		"transport":             h.transportState(),
	}
}

func (h *Host) transportState() string {
	if h.env.SidecarExecutable == "" {
		return "deferred"
	}
	if h.transport != nil && h.transport.Disconnected() {
		return "disconnected"
	}
	if h.transport != nil {
		return "connected"
	}
	return "deferred"
}

func (h *Host) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, h.healthSnapshot())
}

func (h *Host) handleMetrics(w http.ResponseWriter, r *http.Request) {
	channels := map[string]any{}
	h.channelHealth.Range(func(k, v any) bool {
		channels[k.(string)] = v.(*ChannelHealth).asJSON()
		return true
	})
	var lastNotif any
	if v := h.lastNotif.Load(); v != nil {
		lastNotif = v
	}
	writeJSON(w, 200, map[string]any{
		"channel_health":          channels,
		"worker_queue_size":       0,
		"processing_channels":     []string{"system"},
		"notification_queue_size": 0,
		"last_notification":       lastNotif,
		"uptime_seconds":          time.Since(h.started).Seconds(),
	})
}

func (h *Host) handleShutdown(w http.ResponseWriter, r *http.Request) {
	supplied := r.Header.Get("X-GPTBridge-Shutdown-Token")
	if h.env.ShutdownToken == "" ||
		subtle.ConstantTimeCompare([]byte(supplied), []byte(h.env.ShutdownToken)) != 1 {
		writeText(w, 403, "Forbidden")
		return
	}
	h.RequestShutdown()
	writeText(w, 200, "OK")
}

// ---------------- WebSocket command path ----------------

func (h *Host) handleWS(w http.ResponseWriter, r *http.Request) {
	token := lowerTrim(r.URL.Query().Get("token"))
	instance := r.URL.Query().Get("instance")
	if subtle.ConstantTimeCompare([]byte(token), []byte(h.env.SessionToken)) != 1 ||
		instance != h.env.WorkspaceInstanceID() {
		writeText(w, 403, "Forbidden")
		return
	}
	if !wsproto.IsUpgrade(r) {
		writeText(w, 400, "Bad Request")
		return
	}
	conn, err := wsproto.Accept(w, r)
	if err != nil {
		return
	}
	h.mu.Lock()
	h.clients[conn] = struct{}{}
	h.primary = conn
	h.mu.Unlock()
	defer func() {
		h.mu.Lock()
		delete(h.clients, conn)
		if h.primary == conn {
			h.primary = nil
			for c := range h.clients {
				h.primary = c
				break
			}
		}
		h.mu.Unlock()
		conn.Close()
	}()

	for {
		raw, err := conn.ReadMessage()
		if err != nil {
			return
		}
		go h.dispatchWS(conn, raw)
	}
}

// dispatchWS handles one {command, payload} frame — each command runs
// on its own goroutine so long-running collab work never blocks the
// socket read loop.
func (h *Host) dispatchWS(conn *wsproto.Conn, raw []byte) {
	var frame struct {
		Command string         `json:"command"`
		Payload map[string]any `json:"payload"`
	}
	command := ""
	requestID := ""
	if err := json.Unmarshal(raw, &frame); err == nil {
		command = frame.Command
		if frame.Payload != nil {
			if v, ok := frame.Payload["request_id"].(string); ok {
				requestID = v
			}
		}
	}
	if command == "" || frame.Payload == nil {
		h.sendResult(conn, "error", map[string]any{"ok": false})
		return
	}
	if command == "toolbox_cancel_tool_run" {
		cancelled := false
		if requestID != "" {
			cancelled = h.service.Cancel(context.Background(), requestID)
		}
		h.sendResult(conn, "toolbox_cancel_tool_run_result", map[string]any{
			"ok": cancelled, "cancelled": cancelled,
			"tool_id": h.env.ToolID, "request_id": requestID,
		})
		return
	}
	// Browser-op replies land on the bridge, not the service.
	if command == "ai_collab_browser_op_result" {
		h.completeOp(frame.Payload)
		return
	}
	payload := frame.Payload
	// The renderer is the source UI — the governed actor is the tool
	// itself when the frame does not carry a channel-injected actor.
	if _, ok := payload["_authorized_requester_actor"]; !ok {
		payload["_authorized_requester_actor"] = "governance/tool/ai-collaboration"
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if requestID != "" {
		done := make(chan struct{})
		h.inflight.Store(requestID, done)
		go func() {
			select {
			case <-done:
				cancel()
			case <-h.shutdown:
				cancel()
			case <-conn.Done():
				// socket loss does not cancel; tasks persist
			}
		}()
	}
	event, result := h.service.Handle(ctx, command, payload)
	if requestID != "" {
		if v, ok := h.inflight.Load(requestID); ok {
			close(v.(chan struct{}))
			h.inflight.Delete(requestID)
		}
	}
	h.sendResult(conn, event, result)
}

func (h *Host) sendResult(conn *wsproto.Conn, event string, payload map[string]any) {
	_ = conn.WriteJSON(map[string]any{"event": event, "payload": payload})
}

// ---------------- browser-op bridge ----------------

// completeOp resolves a pending browser op from a UI reply.
func (h *Host) completeOp(payload map[string]any) {
	opID, _ := payload["op_id"].(string)
	if opID == "" {
		return
	}
	h.opMu.Lock()
	ch, ok := h.opPending[opID]
	delete(h.opPending, opID)
	h.opMu.Unlock()
	if !ok {
		return
	}
	res := map[string]any{}
	for k, v := range payload {
		if k != "op_id" {
			res[k] = v
		}
	}
	select {
	case ch <- res:
	default:
	}
}

// BrowserOp sends one op to the connected UI and awaits its result.
func (h *Host) BrowserOp(op, sessionID, targetURL, script string) (map[string]any, error) {
	h.mu.Lock()
	conn := h.primary
	h.mu.Unlock()
	if conn == nil {
		return nil, errors.New("EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE")
	}
	var b [8]byte
	_, _ = rand.Read(b[:])
	opID := "op-" + hex.EncodeToString(b[:])
	ch := make(chan map[string]any, 1)
	h.opMu.Lock()
	h.opPending[opID] = ch
	h.opMu.Unlock()
	defer func() {
		h.opMu.Lock()
		delete(h.opPending, opID)
		h.opMu.Unlock()
	}()
	err := conn.WriteJSON(map[string]any{
		"event": "ai_collab_browser_op",
		"payload": map[string]any{
			"op_id": opID, "op": op, "session_id": sessionID,
			"url": targetURL, "script": script,
		},
	})
	if err != nil {
		return nil, err
	}
	select {
	case res := <-ch:
		return res, nil
	case <-time.After(BrowserOpTimeout):
		return nil, errors.New("EMBEDDED_BROWSER_OP_TIMEOUT")
	case <-h.shutdown:
		return nil, errors.New("SHUTDOWN")
	}
}

// WSBrowserOps adapts Host.BrowserOp to service.BrowserOps.
type WSBrowserOps struct{ H *Host }

func (b *WSBrowserOps) CreateSession(ownerModule, targetURL, sessionID string) (map[string]any, error) {
	return b.H.BrowserOp("create", sessionID, targetURL, "")
}
func (b *WSBrowserOps) Navigate(sessionID, targetURL string) (map[string]any, error) {
	return b.H.BrowserOp("navigate", sessionID, targetURL, "")
}
func (b *WSBrowserOps) ExecuteScript(sessionID, script string) (map[string]any, error) {
	return b.H.BrowserOp("exec", sessionID, "", script)
}
func (b *WSBrowserOps) GetURL(sessionID string) (map[string]any, error) {
	return b.H.BrowserOp("url", sessionID, "", "")
}
func (b *WSBrowserOps) Close(sessionID string) (map[string]any, error) {
	return b.H.BrowserOp("close", sessionID, "", "")
}

// ---------------- worker loop (claim/respond via proxy) ----------------

func (h *Host) recordChannel(channelID string, ok bool) {
	v, _ := h.channelHealth.LoadOrStore(channelID, &ChannelHealth{ChannelID: channelID})
	ch := v.(*ChannelHealth)
	ch.LastOK = ok
	ch.LastRequestAt = time.Now().UTC().Format("2006-01-02T15:04:05Z")
	if ok {
		ch.ConsecutiveFailures = 0
	} else {
		ch.ConsecutiveFailures++
	}
}

// workerLoop mirrors GovernedToolHost.RunWorkerAsync over the proxy
// claim channel; deferred (inert) when no sidecar is configured.
func (h *Host) workerLoop() {
	if h.env.SidecarExecutable == "" {
		return
	}
	transport, err := proxy.Start(h.env.SidecarExecutable, h.env.ToolRoot)
	if err != nil {
		h.recordChannel("system", false)
		return
	}
	h.transport = transport
	go func() {
		<-transport.Done()
		h.RequestShutdown()
	}()
	if _, err := transport.Hello(h.env.ToolID, h.env.WorkspaceInstanceID(),
		map[string]string{"system": "process"}, nil); err != nil {
		h.recordChannel("system", false)
		return
	}
	idlePoll := 250 * time.Millisecond
	maxIdle := 500 * time.Millisecond
	var lastStamp string
	for {
		select {
		case <-h.shutdown:
			return
		default:
		}
		request, err := transport.Claim("system")
		if err != nil {
			h.recordChannel("system", false)
			select {
			case <-h.shutdown:
				return
			case <-time.After(500 * time.Millisecond):
			}
			continue
		}
		if request == nil {
			stamp, serr := transport.NotificationStamp("system")
			if serr == nil && len(stamp) > 0 && string(stamp) != lastStamp {
				lastStamp = string(stamp)
				h.lastNotif.Store(map[string]any{
					"payload":     "transport-store-changed",
					"received_at": time.Now().UTC().Format("2006-01-02T15:04:05Z"),
				})
				idlePoll = 250 * time.Millisecond
				continue
			}
			select {
			case <-h.shutdown:
				return
			case <-time.After(idlePoll):
			}
			if idlePoll < maxIdle {
				idlePoll += idlePoll / 2
				if idlePoll > maxIdle {
					idlePoll = maxIdle
				}
			}
			continue
		}
		idlePoll = 250 * time.Millisecond
		h.handleClaimed(transport, request)
	}
}

func (h *Host) handleClaimed(transport *proxy.Client, request map[string]any) {
	requestID, _ := request["request_id"].(string)
	payload, _ := request["payload"].(map[string]any)
	command := ""
	var result map[string]any
	if payload == nil {
		result = deniedResult(h.env.ToolID, requestID)
	} else {
		if v, ok := payload["_governed_command"].(string); ok {
			command = v
			delete(payload, "_governed_command")
		}
		if command == "" {
			result = deniedResult(h.env.ToolID, requestID)
		} else {
			if actor, ok := request["requester_actor"].(string); ok {
				payload["_authorized_requester_actor"] = actor
			}
			if command == "toolbox_run_local_cleanup" {
				if payload["_authorized_requester_actor"] == "governance/main-system" {
					result = map[string]any{
						"ok": true, "tool_id": h.env.ToolID,
						"operation": "local-self-cleanup",
						"delegated": true, "host": "go-toolhost",
					}
				} else {
					result = deniedResult(h.env.ToolID, requestID)
				}
			} else {
				_, result = h.service.Handle(context.Background(), command, payload)
			}
		}
	}
	result["request_id"] = requestID
	if _, err := transport.Respond("system", requestID, result); err != nil {
		h.recordChannel("system", false)
		return
	}
	h.recordChannel("system", true)
}

func deniedResult(toolID, requestID string) map[string]any {
	return map[string]any{
		"ok": false, "tool_id": toolID, "request_id": requestID,
		"error_code": "PERMISSION_DENIED", "message": "PERMISSION_DENIED",
	}
}

// ---------------- misc ----------------

func writeJSON(w http.ResponseWriter, status int, body map[string]any) {
	raw, err := json.Marshal(body)
	if err != nil {
		writeText(w, 500, "internal")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(raw)
}

func writeText(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "text/plain")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(body))
}

func lowerTrim(s string) string {
	out := []byte(s)
	for i, c := range out {
		if c >= 'A' && c <= 'Z' {
			out[i] = c + 32
		}
	}
	return string(trimBytes(out))
}

func trimBytes(b []byte) []byte {
	for len(b) > 0 && (b[0] == ' ' || b[0] == '\t' || b[0] == '\n' || b[0] == '\r') {
		b = b[1:]
	}
	for len(b) > 0 {
		c := b[len(b)-1]
		if c == ' ' || c == '\t' || c == '\n' || c == '\r' {
			b = b[:len(b)-1]
		} else {
			break
		}
	}
	return b
}
