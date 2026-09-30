//go:build windows

// browser_sendrecv_test.go — real WebView2 send/receive proof for the
// ai-collaboration browser-op lane.  A data: URL hosts a minimal
// provider page using the same DOM contract as the ChatGPT adapter
// (#prompt-textarea contenteditable input, [data-testid=send-button],
// [data-message-author-role=assistant] replies, stop-button while
// generating).  The fill/submit/probe scripts below are parity copies
// of the backend's injected scripts (backend-go/internal/service/
// browser.go): the same selectors and the same result shapes, so a
// pass here proves the DOM ops the backend emits really drive a page.
package main

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// fakeProviderHTML mimics a provider chat page: a send click echoes the
// prompt into an assistant element after a short "generation" delay.
const fakeProviderHTML = `<!doctype html><html><body>
<div id="prompt-textarea" contenteditable="true"></div>
<button data-testid="send-button" aria-label="Send">Send</button>
<div id="feed"></div>
<script>
document.querySelector('[data-testid=send-button]').addEventListener('click', function () {
	var q = document.getElementById('prompt-textarea').textContent;
	var stop = document.createElement('button');
	stop.setAttribute('data-testid', 'stop-button');
	stop.setAttribute('aria-label', 'Stop');
	document.body.appendChild(stop);
	setTimeout(function () {
		var r = document.createElement('div');
		r.setAttribute('data-message-author-role', 'assistant');
		r.textContent = 'ECHO-REPLY::' + q;
		document.getElementById('feed').appendChild(r);
		setTimeout(function () { stop.remove(); }, 60);
	}, 200);
});
</script></body></html>`

// Parity copies of the backend injected scripts (same selectors, same
// result shapes) — the values the WS bridge would carry for chatgpt.
func testFillScript(prompt string) string {
	pj, _ := jsonString(prompt)
	return `(() => {
const selectors = ["#prompt-textarea", "[contenteditable=\"true\"]", "textarea"];
const prompt = ` + pj + `;
for (const sel of selectors) {
	const el = document.querySelector(sel);
	if (!el) continue;
	if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
		el.value = prompt;
		el.dispatchEvent(new Event('input', {bubbles: true}));
	} else {
		el.textContent = prompt;
		el.dispatchEvent(new InputEvent('input', {bubbles: true}));
	}
	el.focus();
	return {found: true};
}
return {found: false};
})()`
}

const testSubmitScript = `(() => {
const sends = ["button[data-testid=\"send-button\"]", "button[data-testid*=\"send\" i]", "button[aria-label*=\"Send\" i]"];
const inputs = ["#prompt-textarea", "[contenteditable=\"true\"]", "textarea"];
for (const sel of sends) {
	const el = document.querySelector(sel);
	if (el && !el.disabled) { el.click(); return {sent: true, via: 'click'}; }
}
for (const sel of inputs) {
	const el = document.querySelector(sel);
	if (!el) continue;
	el.focus();
	for (const type of ['keydown', 'keypress', 'keyup']) {
		el.dispatchEvent(new KeyboardEvent(type, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
	}
	return {sent: true, via: 'enter-key'};
}
return {sent: false};
})()`

const testProbeScript = `(() => {
const respSelectors = ["[data-message-author-role=\"assistant\"]"];
const genSelectors = ["button[data-testid=\"stop-button\"]", "button[aria-label*=\"Stop\" i]"];
let content = '';
for (const sel of respSelectors) {
	const els = document.querySelectorAll(sel);
	for (let i = els.length - 1; i >= 0; i--) {
		const text = (els[i].innerText || els[i].textContent || '').trim();
		if (text) { content = text; break; }
	}
	if (content) break;
}
let generating = false;
for (const sel of genSelectors) {
	const els = document.querySelectorAll(sel);
	for (const el of els) { if (el.offsetParent !== null) { generating = true; break; } }
	if (generating) break;
}
return {content, generating, verification: false, marker: ''};
})()`

func jsonString(s string) (string, error) {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String(), nil
}

// TestBrowserSendReceive runs the real op sequence the backend performs
// for one provider send: create → navigate → exec(fill) → exec(submit)
// → exec(probe) until stable — all through real WebView2 DOM ops.
func TestBrowserSendReceive(t *testing.T) {
	dataRoot := filepath.Join(os.TempDir(),
		"aicollab-sendrecv-test-"+strconv.Itoa(int(pid())))
	m := NewBrowserManager(uint32(pid()), "aicollab-ui-test-parent",
		dataRoot, func(map[string]any) {})

	var parent uintptr
	var perr error
	m.run(func() { parent, perr = createPopupWindow() })
	if perr != nil || parent == 0 {
		t.Fatalf("create parent window: %v", perr)
	}
	m.parent = parent
	defer func() { m.run(func() { destroyWindow(parent) }) }()
	defer m.Shutdown()
	defer func() {
		for i := 0; i < 20; i++ {
			if err := os.RemoveAll(dataRoot); err == nil {
				return
			}
			time.Sleep(250 * time.Millisecond)
		}
		_ = os.RemoveAll(dataRoot)
	}()

	res := m.DOMOp(map[string]any{
		"op": "create", "session_id": "fake-chatgpt",
		"url": "data:text/html," + url.PathEscape(fakeProviderHTML),
	})
	wantOK(t, "create", res)

	// Fill: prompt crosses into the page DOM.
	prompt := "對星澄說：1+1=?"
	res = m.DOMOp(map[string]any{"op": "exec", "session_id": "fake-chatgpt",
		"script": testFillScript(prompt)})
	wantOK(t, "fill", res)
	if r, _ := res["result"].(map[string]any); r["found"] != true {
		t.Fatalf("fill not found: %v", res)
	}

	// Submit: send control really clicked.
	res = m.DOMOp(map[string]any{"op": "exec", "session_id": "fake-chatgpt",
		"script": testSubmitScript})
	wantOK(t, "submit", res)
	if r, _ := res["result"].(map[string]any); r["sent"] != true {
		t.Fatalf("submit not sent: %v", res)
	}

	// Probe: poll until the assistant reply is present and stable.
	last := ""
	stable := 0
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		res = m.DOMOp(map[string]any{"op": "exec", "session_id": "fake-chatgpt",
			"script": testProbeScript})
		wantOK(t, "probe", res)
		r, _ := res["result"].(map[string]any)
		content, _ := r["content"].(string)
		generating, _ := r["generating"].(bool)
		if generating {
			stable = 0
		} else if content != "" && content == last {
			stable++
			if stable >= 3 {
				break
			}
		} else {
			stable = 0
		}
		if content != "" {
			last = content
		}
		time.Sleep(50 * time.Millisecond)
	}
	want := "ECHO-REPLY::" + prompt
	if last != want || stable < 3 {
		t.Fatalf("reply not captured: last=%q stable=%d want=%q", last, stable, want)
	}
	fmt.Printf("captured provider reply: %q\n", last)

	res = m.DOMOp(map[string]any{"op": "close", "session_id": "fake-chatgpt"})
	wantOK(t, "close", res)
}
