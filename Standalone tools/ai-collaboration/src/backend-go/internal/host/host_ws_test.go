// host_ws_test.go — wire-level end-to-end test for the governed
// send/receive path: real Host + real loopback WebSocket + real
// Service.  A scripted WS client plays the tool-window role: it answers
// ai_collab_browser_op events (the same replies a healthy embedded
// webview would produce) so the full command → DOM-op → response →
// result-event pipeline is exercised over real sockets.
package host

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"gptbridge.local/ai-collaboration-backend/internal/govenv"
	"gptbridge.local/ai-collaboration-backend/internal/repo"
	"gptbridge.local/ai-collaboration-backend/internal/service"
)

// ---------------- minimal Repository stub ----------------

type stubRepo struct {
	mu        sync.Mutex
	agents    []map[string]any
	messages  []map[string]any
	responses map[string][]map[string]any
	memory    []map[string]any
}

func newStubRepo() *stubRepo {
	return &stubRepo{
		responses: map[string][]map[string]any{},
		agents: []map[string]any{{
			"agent_id": "chatgpt", "name": "ChatGPT", "provider": "chatgpt",
			"home_url": "https://chatgpt.com/", "general_url": "https://chatgpt.com/",
			"investment_url": "https://chatgpt.com/", "star_training_url": "",
			"general_enabled": true, "investment_enabled": true,
			"business_capabilities": []any{"general", "comprehensive", "orchestration"},
			"enabled": true, "selected": true, "status": "idle",
			"last_error": "", "session_state": "closed", "login_state": "unknown",
			"adapter_version": "", "sort_seq": 1, "updated_at": repo.UtcNow(),
		}},
	}
}

func cloneRow(m map[string]any) map[string]any {
	raw, _ := json.Marshal(m)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return out
}

func (r *stubRepo) DBPath() string { return "stub:mem" }

func (r *stubRepo) ListAgents(ctx context.Context) ([]map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]map[string]any, 0, len(r.agents))
	for _, a := range r.agents {
		out = append(out, cloneRow(a))
	}
	return out, nil
}

func (r *stubRepo) GetAgents(ctx context.Context, ids []string) ([]map[string]any, error) {
	all, err := r.ListAgents(ctx)
	if err != nil {
		return nil, err
	}
	want := map[string]bool{}
	for _, id := range ids {
		want[id] = true
	}
	var out []map[string]any
	for _, a := range all {
		if want[a["agent_id"].(string)] {
			out = append(out, a)
		}
	}
	return out, nil
}

func (r *stubRepo) GetAgent(ctx context.Context, id string) (map[string]any, error) {
	list, err := r.GetAgents(ctx, []string{id})
	if err != nil || len(list) == 0 {
		return nil, err
	}
	return list[0], nil
}

func (r *stubRepo) SaveAgentSelection(ctx context.Context, ids []string) error { return nil }
func (r *stubRepo) MaxSortSeq(ctx context.Context) (int, error)               { return 1, nil }
func (r *stubRepo) AgentNameExists(ctx context.Context, name string) (bool, error) {
	return false, nil
}
func (r *stubRepo) AgentIDExists(ctx context.Context, id string) (bool, error) {
	return false, nil
}
func (r *stubRepo) AddAgent(ctx context.Context, a map[string]any) error { return nil }
func (r *stubRepo) SaveAgentBusinessSettings(ctx context.Context, agentID, g, i, s string, ge, ie bool, caps []string) error {
	return nil
}

func (r *stubRepo) UpdateAgentStatus(ctx context.Context, agentID, status, lastError string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, a := range r.agents {
		if a["agent_id"] == agentID {
			a["status"] = status
			a["last_error"] = lastError
		}
	}
	return nil
}
func (r *stubRepo) UpdateAgentRuntimeState(ctx context.Context, agentID string, s, l, v *string) error {
	return nil
}

func (r *stubRepo) CreateGroupMessage(ctx context.Context, content string, selected []string, scope, requestID, generation string) (map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	mid := repo.NewID(16)
	sel := make([]any, len(selected))
	for i, s := range selected {
		sel[i] = s
	}
	msg := map[string]any{
		"message_id": mid, "role": "user", "content": content,
		"selected_agents": sel, "business_scope": scope,
		"request_id": requestID, "runtime_generation": generation,
		"created_at": repo.UtcNow(),
	}
	r.messages = append(r.messages, msg)
	for _, agentID := range selected {
		r.responses[mid] = append(r.responses[mid], map[string]any{
			"response_id": repo.NewID(16), "message_id": mid,
			"agent_id": agentID, "status": "pending", "content": "",
			"error": "", "error_code": "", "execution_provider": "",
			"transport": "", "fallback": map[string]any{},
			"memory_candidates": []any{}, "request_id": requestID,
			"runtime_generation": generation, "response_state": "",
			"result_reference": "", "created_at": repo.UtcNow(),
			"completed_at": "", "updated_at": repo.UtcNow(),
		})
	}
	return cloneRow(msg), nil
}

func (r *stubRepo) UpdateResponse(ctx context.Context, messageID, agentID string, u repo.ResponseUpdate) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, row := range r.responses[messageID] {
		if row["agent_id"] != agentID {
			continue
		}
		row["status"] = u.Status
		row["content"] = u.Content
		row["error"] = u.Error
		row["error_code"] = u.ErrorCode
		if u.ExecutionProvider != "" {
			row["execution_provider"] = u.ExecutionProvider
		}
		if u.Transport != "" {
			row["transport"] = u.Transport
		}
		if u.Fallback != nil {
			row["fallback"] = u.Fallback
		}
		if u.MemoryCandidates != nil {
			row["memory_candidates"] = u.MemoryCandidates
		}
		if u.ResponseState != "" {
			row["response_state"] = u.ResponseState
		}
		row["updated_at"] = repo.UtcNow()
	}
	return nil
}

func (r *stubRepo) CancelPendingResponses(ctx context.Context, messageID string) (int, error) {
	return 0, nil
}

func (r *stubRepo) ListMessages(ctx context.Context, limit int, messageID string) ([]map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []map[string]any
	for _, msg := range r.messages {
		if messageID != "" && msg["message_id"].(string) != messageID {
			continue
		}
		c := cloneRow(msg)
		var rows []any
		for _, rr := range r.responses[msg["message_id"].(string)] {
			rows = append(rows, cloneRow(rr))
		}
		c["responses"] = rows
		out = append(out, c)
	}
	return out, nil
}

func (r *stubRepo) GetMessage(ctx context.Context, messageID string) (map[string]any, error) {
	list, err := r.ListMessages(ctx, 1, messageID)
	if err != nil || len(list) == 0 {
		return nil, err
	}
	return list[0], nil
}

func (r *stubRepo) ListMemoryItems(ctx context.Context) ([]map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]map[string]any, 0, len(r.memory))
	for _, m := range r.memory {
		out = append(out, cloneRow(m))
	}
	return out, nil
}

func (r *stubRepo) AddMemoryItem(ctx context.Context, kind, title, content, scope, srcAgent, srcMsg, hash string) (map[string]any, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	item := map[string]any{
		"memory_id": repo.NewID(16), "kind": kind, "title": title,
		"content": content, "business_scope": scope,
		"source_agent_id": srcAgent, "content_hash": hash,
		"source_message_id": srcMsg,
		"created_at":        repo.UtcNow(), "updated_at": repo.UtcNow(),
	}
	r.memory = append(r.memory, item)
	return cloneRow(item), nil
}

func (r *stubRepo) ListTasks(ctx context.Context) ([]map[string]any, error) {
	return []map[string]any{}, nil
}
func (r *stubRepo) CreateTask(ctx context.Context, title, srcMsg string, participants []string) (map[string]any, error) {
	return map[string]any{"task_id": repo.NewID(16)}, nil
}
func (r *stubRepo) CreateCollabTask(ctx context.Context, requestID, mode string, providers []string, original, generation, attemptID string) (map[string]any, error) {
	return map[string]any{"task_id": repo.NewID(16)}, nil
}
func (r *stubRepo) UpdateCollabTask(ctx context.Context, taskID string, u repo.CollabTaskUpdate) error {
	return nil
}
func (r *stubRepo) GetCollabTask(ctx context.Context, taskID string) (map[string]any, error) {
	return nil, nil
}
func (r *stubRepo) ListCollabTasks(ctx context.Context, limit int) ([]map[string]any, error) {
	return []map[string]any{}, nil
}
func (r *stubRepo) InterruptedCollabTasks(ctx context.Context, generation string) ([]string, error) {
	return nil, nil
}
func (r *stubRepo) UpsertCollabResult(ctx context.Context, rec map[string]any) error {
	return nil
}
func (r *stubRepo) ListCollabResults(ctx context.Context, taskID string) ([]map[string]any, error) {
	return nil, nil
}
func (r *stubRepo) CancelCollabResults(ctx context.Context, taskID string) error { return nil }
func (r *stubRepo) CompletedResultProviders(ctx context.Context, taskID, attemptID string) (map[string]bool, error) {
	return map[string]bool{}, nil
}

// ---------------- minimal WS client (masked frames, RFC 6455) ----------------

type wsTestClient struct {
	conn net.Conn
	rw   *bufio.ReadWriter
}

func dialWS(t *testing.T, port int, token, instance string) *wsTestClient {
	t.Helper()
	conn, err := net.DialTimeout("tcp4", fmt.Sprintf("127.0.0.1:%d", port), 5*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	rw := bufio.NewReadWriter(bufio.NewReader(conn), bufio.NewWriter(conn))
	var kb [16]byte
	_, _ = rand.Read(kb[:])
	key := base64.StdEncoding.EncodeToString(kb[:])
	fmt.Fprintf(rw, "GET /?token=%s&instance=%s HTTP/1.1\r\n"+
		"Host: 127.0.0.1:%d\r\nUpgrade: websocket\r\n"+
		"Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"+
		"Sec-WebSocket-Version: 13\r\n\r\n", token, instance, port, key)
	if err := rw.Flush(); err != nil {
		t.Fatalf("handshake write: %v", err)
	}
	// Read HTTP response headers; expect 101.
	status, err := rw.ReadString('\n')
	if err != nil || !strings.Contains(status, "101") {
		t.Fatalf("handshake status=%q err=%v", status, err)
	}
	for {
		line, err := rw.ReadString('\n')
		if err != nil || line == "\r\n" {
			break
		}
	}
	return &wsTestClient{conn: conn, rw: rw}
}

func (c *wsTestClient) send(command string, payload map[string]any) error {
	raw, err := json.Marshal(map[string]any{"command": command, "payload": payload})
	if err != nil {
		return err
	}
	return c.writeFrame(raw)
}

func (c *wsTestClient) writeFrame(payload []byte) error {
	var hdr []byte
	n := len(payload)
	switch {
	case n < 126:
		hdr = []byte{0x81, 0x80 | byte(n)}
	case n <= 0xffff:
		hdr = []byte{0x81, 0x80 | 126, byte(n >> 8), byte(n)}
	default:
		hdr = make([]byte, 10)
		hdr[0] = 0x81
		hdr[1] = 0x80 | 127
		binary.BigEndian.PutUint64(hdr[2:], uint64(n))
	}
	var mask [4]byte
	_, _ = rand.Read(mask[:])
	masked := make([]byte, n)
	for i := range payload {
		masked[i] = payload[i] ^ mask[i%4]
	}
	if _, err := c.rw.Write(hdr); err != nil {
		return err
	}
	if _, err := c.rw.Write(mask[:]); err != nil {
		return err
	}
	if _, err := c.rw.Write(masked); err != nil {
		return err
	}
	return c.rw.Flush()
}

// readMessage returns the next complete text frame; pings are answered
// with pong and close frames return an error.
func (c *wsTestClient) readMessage() ([]byte, error) {
	var msg []byte
	for {
		header := make([]byte, 2)
		if _, err := readFull(c.rw, header); err != nil {
			return nil, err
		}
		fin := header[0]&0x80 != 0
		op := header[0] & 0x0f
		length := uint64(header[1] & 0x7f)
		if length == 126 {
			ext := make([]byte, 2)
			if _, err := readFull(c.rw, ext); err != nil {
				return nil, err
			}
			length = uint64(binary.BigEndian.Uint16(ext))
		} else if length == 127 {
			ext := make([]byte, 8)
			if _, err := readFull(c.rw, ext); err != nil {
				return nil, err
			}
			length = binary.BigEndian.Uint64(ext)
		}
		payload := make([]byte, length)
		if _, err := readFull(c.rw, payload); err != nil {
			return nil, err
		}
		switch op {
		case 0x9: // ping → pong
			_ = c.writeFrameOp(0xA, payload)
		case 0x8:
			return nil, fmt.Errorf("closed")
		case 0x1, 0x0:
			msg = append(msg, payload...)
			if fin {
				return msg, nil
			}
		}
	}
}

func (c *wsTestClient) writeFrameOp(op byte, payload []byte) error {
	var hdr []byte
	n := len(payload)
	if n < 126 {
		hdr = []byte{0x80 | op, 0x80 | byte(n)}
	} else {
		hdr = []byte{0x80 | op, 0x80 | 126, byte(n >> 8), byte(n)}
	}
	var mask [4]byte
	_, _ = rand.Read(mask[:])
	masked := make([]byte, n)
	for i := range payload {
		masked[i] = payload[i] ^ mask[i%4]
	}
	if _, err := c.rw.Write(hdr); err != nil {
		return err
	}
	if _, err := c.rw.Write(mask[:]); err != nil {
		return err
	}
	if _, err := c.rw.Write(masked); err != nil {
		return err
	}
	return c.rw.Flush()
}

func readFull(r *bufio.ReadWriter, b []byte) (int, error) {
	got := 0
	for got < len(b) {
		n, err := r.Read(b[got:])
		got += n
		if err != nil {
			return got, err
		}
	}
	return got, nil
}

// ---------------- fake embedded-browser (tool-window role) ----------------

// cannedReply is what a healthy provider webview would return for the
// probe script once a real reply finished rendering.
const cannedReply = "【單元測試】外部 AI 回覆內容：1+1=2。"

type opLog struct {
	mu   sync.Mutex
	ops  []string
	fill string
}

func (l *opLog) add(op string) {
	l.mu.Lock()
	l.ops = append(l.ops, op)
	l.mu.Unlock()
}

// answerOp mimics the tool-window DOMOp dispatcher on a healthy,
// already-logged-in provider page (url host matches the adapter).
func answerOp(p map[string]any, log *opLog) map[string]any {
	op, _ := p["op"].(string)
	script, _ := p["script"].(string)
	log.add(op)
	switch op {
	case "create":
		return map[string]any{"ok": true, "id": p["session_id"], "backend": "test-webview2"}
	case "navigate":
		return map[string]any{"ok": true, "url": p["url"]}
	case "url":
		return map[string]any{"ok": true, "url": "https://chatgpt.com/"}
	case "close":
		return map[string]any{"ok": true}
	case "exec":
		switch {
		case strings.Contains(script, "readyState"):
			return map[string]any{"ok": true, "result": map[string]any{"ready": true}}
		case strings.Contains(script, "flagged"):
			return map[string]any{"ok": true, "result": map[string]any{"flagged": false, "marker": ""}}
		case strings.Contains(script, "found:"):
			log.mu.Lock()
			log.fill = script
			log.mu.Unlock()
			return map[string]any{"ok": true, "result": map[string]any{"found": true}}
		case strings.Contains(script, "sent:"):
			return map[string]any{"ok": true, "result": map[string]any{"sent": true, "via": "click"}}
		default:
			// probe script: completed, stable, non-empty content.
			return map[string]any{"ok": true, "result": map[string]any{
				"content": cannedReply, "generating": false,
				"verification": false, "marker": "",
			}}
		}
	}
	return map[string]any{"ok": false, "error_code": "UNSUPPORTED_OP"}
}

// ---------------- the test ----------------

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("port: %v", err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

// TestSendReceiveOverWS drives ai_nexus_send_message through the real
// host: command in → browser ops out → op results back → response row
// completed → send_message_result emitted.  Every hop crosses the real
// loopback socket and the real service/browser-session pipeline.
func TestSendReceiveOverWS(t *testing.T) {
	token := strings.Repeat("a1", 32) // 64-hex
	env := &govenv.Environment{
		ToolID:        "ai-collaboration",
		ProjectRoot:   t.TempDir(),
		ToolRoot:      t.TempDir(),
		ToolDataRoot:  t.TempDir(),
		SessionToken:  token,
		Port:          freePort(t),
		ShutdownToken: "shutdown-test-token",
	}
	h := New(env, nil, "9.9.9-test")
	bs := service.NewBrowserSession(&WSBrowserOps{H: h})
	bs.PollInterval = time.Millisecond
	svc := service.New("9.9.9-test", t.TempDir(), newStubRepo(), bs)
	h.AttachService(svc)
	if err := h.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	defer h.RequestShutdown()

	client := dialWS(t, env.Port, token, env.WorkspaceInstanceID())
	defer client.conn.Close()

	log := &opLog{}
	resultCh := make(chan map[string]any, 4)
	errCh := make(chan error, 1)

	go func() {
		for {
			raw, err := client.readMessage()
			if err != nil {
				errCh <- err
				return
			}
			var frame struct {
				Event   string         `json:"event"`
				Payload map[string]any `json:"payload"`
			}
			if json.Unmarshal(raw, &frame) != nil {
				continue
			}
			switch frame.Event {
			case "ai_collab_browser_op":
				res := answerOp(frame.Payload, log)
				res["op_id"] = frame.Payload["op_id"]
				_ = client.send("ai_collab_browser_op_result", res)
			case "ai_nexus_send_message_result":
				resultCh <- frame.Payload
			}
		}
	}()

	prompt := "測試提示：1+1=?"
	if err := client.send("ai_nexus_send_message", map[string]any{
		"content":   prompt,
		"agent_ids": []any{"chatgpt"},
		"request_id": "e2e-test-1",
		"_authorized_requester_actor": "governance/tool/ai-collaboration",
	}); err != nil {
		t.Fatalf("send command: %v", err)
	}

	select {
	case res := <-resultCh:
		if ok, _ := res["ok"].(bool); !ok {
			t.Fatalf("send_message failed: %v", res)
		}
		if res["workflow_status"] != "completed" {
			t.Fatalf("workflow_status=%v", res["workflow_status"])
		}
		msg, _ := res["group_message"].(map[string]any)
		rows, _ := msg["responses"].([]any)
		if len(rows) != 1 {
			t.Fatalf("responses=%v", rows)
		}
		row, _ := rows[0].(map[string]any)
		if row["status"] != "completed" {
			t.Fatalf("response status=%v", row["status"])
		}
		if row["content"] != cannedReply {
			t.Fatalf("content=%q", row["content"])
		}
		if row["transport"] != "embedded-browser-view" {
			t.Fatalf("transport=%v", row["transport"])
		}
		if row["execution_provider"] != "chatgpt" {
			t.Fatalf("provider=%v", row["execution_provider"])
		}
	case err := <-errCh:
		t.Fatalf("ws read: %v", err)
	case <-time.After(30 * time.Second):
		t.Fatalf("timed out waiting for send_message_result; ops=%v", log.ops)
	}

	// The send must have really flowed through the op bridge: session
	// created, prompt filled (script embeds the literal prompt), send
	// clicked, response probed.
	log.mu.Lock()
	defer log.mu.Unlock()
	joined := strings.Join(log.ops, ",")
	if !strings.Contains(joined, "create") || !strings.Contains(joined, "exec") {
		t.Fatalf("expected create+exec ops, got %v", log.ops)
	}
	if !strings.Contains(log.fill, "found:") || !strings.Contains(log.fill, "測試提示") {
		t.Fatalf("fill script did not carry the prompt over the wire")
	}
}

// TestBrowserOpUnavailableNoUI fails closed when no tool window is
// connected — the bridge must report EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE
// rather than hang.
func TestBrowserOpUnavailableNoUI(t *testing.T) {
	env := &govenv.Environment{
		ToolID:       "ai-collaboration",
		ProjectRoot:  t.TempDir(),
		ToolRoot:     t.TempDir(),
		SessionToken: strings.Repeat("b2", 32),
		Port:         freePort(t),
	}
	h := New(env, nil, "test")
	if err := h.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	defer h.RequestShutdown()
	_, err := h.BrowserOp("exec", "s1", "", "1")
	if err == nil || !strings.Contains(err.Error(), "EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE") {
		t.Fatalf("err=%v", err)
	}
}
