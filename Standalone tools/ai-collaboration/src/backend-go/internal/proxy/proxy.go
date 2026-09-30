// Package proxy is a star-governed-transport-proxy/v1 stdio sidecar
// client — parity with TransportProxyClient.cs. Wire protocol: UTF-8
// JSONL, one request/response per line, correlated by caller-supplied
// id. All failures surface as *Error with the spec's closed code set.
package proxy

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"os/exec"
	"sync"
	"sync/atomic"
	"time"
)

func timeAfter(seconds int) <-chan time.Time {
	return time.After(time.Duration(seconds) * time.Second)
}

const maxLineBytes = 2 << 20
const stderrTailBytes = 4096

// Error is a proxy failure carrying the spec's error code.
type Error struct {
	Code    string
	Message string
}

func (e *Error) Error() string { return e.Code + ":" + e.Message }

// Client owns the sidecar process and its JSONL request stream.
type Client struct {
	cmd    *exec.Cmd
	stdin  io.Writer
	wmu    sync.Mutex
	pmu    sync.Mutex
	pend   map[string]chan callResult
	tailMu sync.Mutex
	tail   bytes.Buffer
	done   chan struct{}
	once   sync.Once
	dead   atomic.Bool
}

type callResult struct {
	result json.RawMessage
	err    error
}

// OnDisconnect is invoked (once) when the sidecar stream terminates.
var ErrDisconnected = &Error{Code: "PROXY_DISCONNECTED", Message: "transport proxy terminated"}

// Start spawns the native sidecar; it inherits the governed environment
// verbatim (including GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP, which the
// sidecar consumes like load_authentication).
func Start(executable, toolRoot string) (*Client, error) {
	cmd := exec.Command(executable)
	cmd.Dir = toolRoot
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, &Error{Code: "PROXY_DISCONNECTED", Message: "sidecar spawn failed: " + err.Error()}
	}
	c := &Client{cmd: cmd, stdin: stdin, pend: map[string]chan callResult{}, done: make(chan struct{})}
	go c.readLoop(stdout)
	go c.stderrDrain(stderr)
	return c, nil
}

// Disconnected reports whether the sidecar stream has terminated.
func (c *Client) Disconnected() bool { return c.dead.Load() }

// StderrTail returns the bounded tail of sidecar stderr — the only
// diagnostic surface when the proxy dies silently.
func (c *Client) StderrTail() string {
	c.tailMu.Lock()
	defer c.tailMu.Unlock()
	return c.tail.String()
}

// Done closes when the sidecar terminates.
func (c *Client) Done() <-chan struct{} { return c.done }

func (c *Client) readLoop(r io.Reader) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 64<<10), maxLineBytes)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var msg struct {
			ID     string          `json:"id"`
			OK     bool            `json:"ok"`
			Result json.RawMessage `json:"result"`
			Error  *struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		}
		if err := json.Unmarshal(line, &msg); err != nil || msg.ID == "" {
			continue
		}
		c.pmu.Lock()
		ch, ok := c.pend[msg.ID]
		if ok {
			delete(c.pend, msg.ID)
		}
		c.pmu.Unlock()
		if !ok {
			continue
		}
		if msg.OK {
			ch <- callResult{result: msg.Result}
		} else {
			pe := &Error{Code: "TRANSPORT_ERROR"}
			if msg.Error != nil {
				pe.Code = msg.Error.Code
				pe.Message = msg.Error.Message
			}
			ch <- callResult{err: pe}
		}
	}
	c.fail()
}

func (c *Client) stderrDrain(r io.Reader) {
	buf := make([]byte, 1024)
	for {
		n, err := r.Read(buf)
		if n > 0 {
			c.tailMu.Lock()
			c.tail.Write(buf[:n])
			if c.tail.Len() > stderrTailBytes {
				excess := c.tail.Len() - stderrTailBytes
				rest := append([]byte(nil), c.tail.Bytes()[excess:]...)
				c.tail.Reset()
				c.tail.Write(rest)
			}
			c.tailMu.Unlock()
		}
		if err != nil {
			return
		}
	}
}

func (c *Client) fail() {
	c.once.Do(func() {
		c.dead.Store(true)
		c.pmu.Lock()
		for id, ch := range c.pend {
			delete(c.pend, id)
			ch <- callResult{err: ErrDisconnected}
		}
		c.pmu.Unlock()
		close(c.done)
	})
}

func callID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// Call issues one op envelope and awaits its correlated response.
func (c *Client) Call(op string, args map[string]any) (json.RawMessage, error) {
	if c.dead.Load() {
		return nil, ErrDisconnected
	}
	id := callID()
	env := map[string]any{"v": 1, "id": id, "op": op, "args": args}
	raw, err := json.Marshal(env)
	if err != nil {
		return nil, err
	}
	ch := make(chan callResult, 1)
	c.pmu.Lock()
	c.pend[id] = ch
	c.pmu.Unlock()

	c.wmu.Lock()
	_, err = c.stdin.Write(append(raw, '\n'))
	c.wmu.Unlock()
	if err != nil {
		c.pmu.Lock()
		delete(c.pend, id)
		c.pmu.Unlock()
		return nil, &Error{Code: "PROXY_DISCONNECTED", Message: err.Error()}
	}
	res := <-ch
	return res.result, res.err
}

// Hello performs the op=hello handshake declaring channel modes.
func (c *Client) Hello(toolID, workspaceInstanceID string, channels map[string]string, submit map[string]map[string]string) (map[string]any, error) {
	args := map[string]any{
		"tool_id":               toolID,
		"workspace_instance_id": workspaceInstanceID,
		"channels":              channels,
	}
	if len(submit) > 0 {
		args["submit"] = submit
	}
	raw, err := c.Call("hello", args)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, &Error{Code: "BAD_ENVELOPE", Message: "hello result"}
	}
	return out, nil
}

// Claim returns the next queued request object on a channel, or nil.
func (c *Client) Claim(channel string) (map[string]any, error) {
	raw, err := c.Call("claim", map[string]any{"channel": channel})
	if err != nil {
		return nil, err
	}
	var wrapper struct {
		Request map[string]any `json:"request"`
	}
	if err := json.Unmarshal(raw, &wrapper); err != nil {
		return nil, &Error{Code: "BAD_ENVELOPE", Message: "claim result"}
	}
	return wrapper.Request, nil
}

// Respond submits a command result back through the store.
func (c *Client) Respond(channel, requestID string, response map[string]any) (bool, error) {
	raw, err := c.Call("respond", map[string]any{
		"channel": channel, "request_id": requestID, "response": response,
	})
	if err != nil {
		return false, err
	}
	var ok bool
	return ok, json.Unmarshal(raw, &ok)
}

// RequestCancelled reports whether the requester marked the store row
// cancelled (cancel_tool_execution semantics).
func (c *Client) RequestCancelled(channel, requestID string) (bool, error) {
	raw, err := c.Call("request_cancelled", map[string]any{
		"channel": channel, "request_id": requestID,
	})
	if err != nil {
		return false, err
	}
	var ok bool
	return ok, json.Unmarshal(raw, &ok)
}

// NotificationStamp returns the channel's write stamp for wake probes.
func (c *Client) NotificationStamp(channel string) (json.RawMessage, error) {
	return c.Call("notification_stamp", map[string]any{"channel": channel})
}

// Close closes stdin, then kills the sidecar after a bounded wait.
func (c *Client) Close() {
	if c.dead.Load() {
		return
	}
	if pw, ok := c.stdin.(io.Closer); ok {
		_ = pw.Close()
	}
	waited := make(chan struct{})
	go func() {
		_ = c.cmd.Wait()
		close(waited)
	}()
	select {
	case <-waited:
	case <-c.done:
		_ = c.cmd.Process.Kill()
	case <-timeAfter(5):
		_ = c.cmd.Process.Kill()
	}
}
