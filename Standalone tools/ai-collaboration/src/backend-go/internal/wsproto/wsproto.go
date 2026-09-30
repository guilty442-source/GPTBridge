// Package wsproto implements a minimal RFC 6455 WebSocket server for the
// governed tool host: text frames only, client-masked reads, unmasked
// writes, ping→pong, close handshake. Loopback-only, token-gated at the
// HTTP layer; bounded message and write sizes.
package wsproto

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"sync"
	"time"
)

const (
	opContinuation = 0x0
	opText         = 0x1
	opBinary       = 0x2
	opClose        = 0x8
	opPing         = 0x9
	opPong         = 0xA

	// MaxMessage bounds a single reassembled text frame sequence.
	MaxMessage = 8 << 20
	// maxFrame bounds a single frame payload before fragmentation.
	maxFrame = 1 << 20
)

var (
	ErrClosed   = errors.New("websocket closed")
	ErrNotWS    = errors.New("not a websocket upgrade request")
	guidSha1    = []byte("258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
	textPlain   = "text/plain"
	errBadFrame = errors.New("protocol error")
)

// Conn is a server-side WebSocket connection.
type Conn struct {
	net  net.Conn
	rw   *bufio.ReadWriter
	wmu  sync.Mutex
	done chan struct{}
	once sync.Once
}

// IsUpgrade reports whether r is a websocket upgrade request.
func IsUpgrade(r *http.Request) bool {
	return headerContainsToken(r.Header, "Connection", "upgrade") &&
		headerContainsToken(r.Header, "Upgrade", "websocket")
}

func headerContainsToken(h http.Header, name, token string) bool {
	for _, v := range h[http.CanonicalHeaderKey(name)] {
		for _, part := range splitComma(v) {
			if equalFoldASCII(part, token) {
				return true
			}
		}
	}
	return false
}

func splitComma(s string) []string {
	var out []string
	start := 0
	for i := 0; i <= len(s); i++ {
		if i == len(s) || s[i] == ',' {
			out = append(out, trimSpace(s[start:i]))
			start = i + 1
		}
	}
	return out
}

func trimSpace(s string) string {
	a, b := 0, len(s)
	for a < b && (s[a] == ' ' || s[a] == '\t') {
		a++
	}
	for b > a && (s[b-1] == ' ' || s[b-1] == '\t') {
		b--
	}
	return s[a:b]
}

func equalFoldASCII(a, b string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := 0; i < len(a); i++ {
		ca, cb := a[i], b[i]
		if ca >= 'A' && ca <= 'Z' {
			ca += 32
		}
		if cb >= 'A' && cb <= 'Z' {
			cb += 32
		}
		if ca != cb {
			return false
		}
	}
	return true
}

// Accept performs the server-side upgrade handshake on w. The caller is
// responsible for authentication before calling Accept.
func Accept(w http.ResponseWriter, r *http.Request) (*Conn, error) {
	key := r.Header.Get("Sec-WebSocket-Key")
	if key == "" {
		return nil, ErrNotWS
	}
	h, ok := w.(http.Hijacker)
	if !ok {
		return nil, errors.New("hijack unsupported")
	}
	conn, rw, err := h.Hijack()
	if err != nil {
		return nil, err
	}
	sum := sha1.Sum(append([]byte(key), guidSha1...))
	accept := base64.StdEncoding.EncodeToString(sum[:])
	fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\n"+
		"Upgrade: websocket\r\nConnection: Upgrade\r\n"+
		"Sec-WebSocket-Accept: %s\r\n\r\n", accept)
	if err := rw.Flush(); err != nil {
		conn.Close()
		return nil, err
	}
	return &Conn{net: conn, rw: rw, done: make(chan struct{})}, nil
}

// ReadMessage returns the next complete text message. Ping frames are
// answered automatically; a close frame returns ErrClosed.
func (c *Conn) ReadMessage() ([]byte, error) {
	var message []byte
	for {
		op, fin, payload, err := c.readFrame()
		if err != nil {
			return nil, err
		}
		switch op {
		case opPing:
			_ = c.writeFrame(opPong, payload)
		case opPong:
			// ignored
		case opClose:
			_ = c.writeFrame(opClose, nil)
			c.Close()
			return nil, ErrClosed
		case opText, opContinuation, opBinary:
			if op == opText && len(message) > 0 {
				return nil, errBadFrame
			}
			message = append(message, payload...)
			if len(message) > MaxMessage {
				return nil, errBadFrame
			}
			if fin {
				if op == opBinary {
					continue // contract is text-only
				}
				return message, nil
			}
		default:
			return nil, errBadFrame
		}
	}
}

func (c *Conn) readFrame() (byte, bool, []byte, error) {
	header := make([]byte, 2)
	if _, err := io.ReadFull(c.rw, header); err != nil {
		return 0, false, nil, err
	}
	fin := header[0]&0x80 != 0
	op := header[0] & 0x0f
	masked := header[1]&0x80 != 0
	length := uint64(header[1] & 0x7f)
	if length == 126 {
		ext := make([]byte, 2)
		if _, err := io.ReadFull(c.rw, ext); err != nil {
			return 0, false, nil, err
		}
		length = uint64(binary.BigEndian.Uint16(ext))
	} else if length == 127 {
		ext := make([]byte, 8)
		if _, err := io.ReadFull(c.rw, ext); err != nil {
			return 0, false, nil, err
		}
		length = binary.BigEndian.Uint64(ext)
	}
	if length > maxFrame {
		return 0, false, nil, errBadFrame
	}
	var mask [4]byte
	if masked {
		if _, err := io.ReadFull(c.rw, mask[:]); err != nil {
			return 0, false, nil, err
		}
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(c.rw, payload); err != nil {
		return 0, false, nil, err
	}
	if masked {
		for i := range payload {
			payload[i] ^= mask[i%4]
		}
	}
	return op, fin, payload, nil
}

func (c *Conn) writeFrame(op byte, payload []byte) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	var header []byte
	b0 := byte(0x80) | op
	switch {
	case len(payload) < 126:
		header = []byte{b0, byte(len(payload))}
	case len(payload) <= 0xffff:
		header = make([]byte, 4)
		header[0] = b0
		header[1] = 126
		binary.BigEndian.PutUint16(header[2:], uint16(len(payload)))
	default:
		header = make([]byte, 10)
		header[0] = b0
		header[1] = 127
		binary.BigEndian.PutUint64(header[2:], uint64(len(payload)))
	}
	if _, err := c.rw.Write(header); err != nil {
		return err
	}
	if _, err := c.rw.Write(payload); err != nil {
		return err
	}
	return c.rw.Flush()
}

// WriteText sends one unfragmented text frame.
func (c *Conn) WriteText(payload []byte) error {
	return c.writeFrame(opText, payload)
}

// WriteJSON marshals v and sends it as a text frame.
func (c *Conn) WriteJSON(v any) error {
	raw, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return c.WriteText(raw)
}

// Close terminates the connection.
func (c *Conn) Close() {
	c.once.Do(func() {
		close(c.done)
		_ = c.net.SetDeadline(time.Now())
		_ = c.net.Close()
	})
}

// Done closes when the connection terminates.
func (c *Conn) Done() <-chan struct{} { return c.done }

var _ = textPlain
