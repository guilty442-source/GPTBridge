// browser.go — provider adapter tables, embedded-browser session and
// provider runtime pool. Parity with the retired provider_adapters /
// browser_automation / provider_runtime / provider_session modules.
// The browser itself is owned by the tool window (Qt WebEngine); every
// DOM op is delegated through the BrowserOps bridge — browser-only,
// no API keys, no terminal fallback, no cross-provider substitution.
package service

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
	"sync"
	"time"

	"gptbridge.local/ai-collaboration-backend/internal/domain"
	"gptbridge.local/ai-collaboration-backend/internal/repo"
)

const (
	browserOwnerModule     = "ai-collaboration"
	responseTimeoutSeconds = 120
	pageLoadWaitSeconds    = 30
	responseStablePolls    = 3
	browserWaitCycles      = 3
	browserWaitSeconds     = 20
	defaultSessionMode     = "embedded-browser-view"
)

var browserPrimaryProviders = map[string]bool{
	"chatgpt": true, "claude": true, "gemini": true,
	"grok": true, "deepseek": true, "perplexity": true,
}

var verificationMarkers = []string{
	"captcha", "cloudflare", "verify you are human",
	"checking your browser", "are you a robot",
	"驗證碼", "請完成人機驗證", "人機驗證",
}

// ProviderAdapter mirrors the retired ProviderAdapter record.
type ProviderAdapter struct {
	ProviderIdentity          string
	DisplayName               string
	InputSelectors            []string
	SendSelectors             []string
	ResponseSelectors         []string
	GeneratingSelectors       []string
	ExpectedHosts             []string
	SendCapability            string
	ResponseCaptureCapability string
	AdapterVersion            string
}

func (a *ProviderAdapter) matchesHost(rawURL string) bool {
	u, err := url.Parse(rawURL)
	if err != nil {
		return false
	}
	host := strings.ToLower(u.Hostname())
	for _, expected := range a.ExpectedHosts {
		expected = strings.ToLower(expected)
		if host == expected || strings.HasSuffix(host, "."+expected) {
			return true
		}
	}
	return false
}

var providerAdapters = map[string]*ProviderAdapter{
	"chatgpt": {
		ProviderIdentity: "chatgpt", DisplayName: "ChatGPT",
		InputSelectors:      []string{"#prompt-textarea", `[contenteditable="true"]`, "textarea"},
		SendSelectors:       []string{`button[data-testid="send-button"]`, `button[data-testid*="send" i]`, `button[aria-label*="Send" i]`, `button[aria-label*="傳送" i]`},
		ResponseSelectors:   []string{`[data-message-author-role="assistant"]`},
		GeneratingSelectors: []string{`button[data-testid="stop-button"]`, `button[aria-label*="Stop" i]`, `button[aria-label*="停止" i]`},
		ExpectedHosts:       []string{"chatgpt.com", "chat.openai.com"},
	},
	"claude": {
		ProviderIdentity: "claude", DisplayName: "Claude",
		InputSelectors:      []string{`div[contenteditable="true"].ProseMirror`, `div[contenteditable="true"]`, "textarea"},
		SendSelectors:       []string{`button[aria-label*="Send" i]`, `button[data-testid*="send" i]`},
		ResponseSelectors:   []string{`[data-testid*="assistant" i]`, ".font-claude-response", `[class*="assistant" i]`},
		GeneratingSelectors: []string{`button[aria-label*="Stop" i]`, `button[data-testid*="stop" i]`},
		ExpectedHosts:       []string{"claude.ai"},
	},
	"gemini": {
		ProviderIdentity: "gemini", DisplayName: "Gemini",
		InputSelectors:      []string{"rich-textarea .ql-editor", `div[contenteditable="true"]`, "textarea"},
		SendSelectors:       []string{"button.send-button", `button[aria-label*="Send" i]`, `button[aria-label*="傳送"]`},
		ResponseSelectors:   []string{"model-response", ".model-response-text", `[class*="response" i]`},
		GeneratingSelectors: []string{`button[aria-label*="Stop" i]`, ".stop-button"},
		ExpectedHosts:       []string{"gemini.google.com"},
	},
	"grok": {
		ProviderIdentity: "grok", DisplayName: "Grok",
		InputSelectors:      []string{"textarea", `div[contenteditable="true"]`},
		SendSelectors:       []string{`button[aria-label*="Submit" i]`, `button[aria-label*="Send" i]`, `button[type="submit"]`},
		ResponseSelectors:   []string{`[data-testid*="assistant" i]`, `[data-testid="message-bubble"]`, `[class*="assistant" i]`},
		GeneratingSelectors: []string{`button[aria-label*="Stop" i]`},
		ExpectedHosts:       []string{"grok.com", "x.ai"},
	},
	"deepseek": {
		ProviderIdentity: "deepseek", DisplayName: "DeepSeek",
		InputSelectors:      []string{"textarea", `div[contenteditable="true"]`},
		SendSelectors:       []string{`button[aria-label*="Send" i]`, `button[type="submit"]`},
		ResponseSelectors:   []string{".ds-markdown", `[class*="assistant" i]`, ".markdown"},
		GeneratingSelectors: []string{`[class*="stop" i]`},
		ExpectedHosts:       []string{"chat.deepseek.com", "deepseek.com"},
	},
	"perplexity": {
		ProviderIdentity: "perplexity", DisplayName: "Perplexity",
		InputSelectors:      []string{"textarea", `div[contenteditable="true"]`},
		SendSelectors:       []string{`button[aria-label*="Submit" i]`, `button[aria-label*="Send" i]`, `button[type="submit"]`},
		ResponseSelectors:   []string{`[data-testid*="answer" i]`, `[class*="answer" i]`, ".prose"},
		GeneratingSelectors: []string{`button[aria-label*="Stop" i]`, `[class*="stop" i]`},
		ExpectedHosts:       []string{"perplexity.ai", "www.perplexity.ai"},
	},
}

func adapterFor(provider string) *ProviderAdapter {
	return providerAdapters[strings.ToLower(provider)]
}

func init() {
	for _, a := range providerAdapters {
		a.SendCapability = "dom-fill-submit"
		a.ResponseCaptureCapability = "dom-state-poll"
		a.AdapterVersion = domain.AdapterVersion
	}
}

// ---------------- injected JS (verbatim ports) ----------------

func jsString(s string) string {
	raw, _ := json.Marshal(s)
	return string(raw)
}

func jsSelectorList(selectors []string) string {
	raw, _ := json.Marshal(selectors)
	return string(raw)
}

// fillScript iterates input selectors and sets the prompt.
func fillScript(adapter *ProviderAdapter, prompt string) string {
	return fmt.Sprintf(`(() => {
const selectors = %s;
const prompt = %s;
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
})()`, jsSelectorList(adapter.InputSelectors), jsString(prompt))
}

// submitScript clicks the first enabled send control, else Enter key.
func submitScript(adapter *ProviderAdapter) string {
	return fmt.Sprintf(`(() => {
const sends = %s;
const inputs = %s;
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
})()`, jsSelectorList(adapter.SendSelectors), jsSelectorList(adapter.InputSelectors))
}

// probeScript reads the last non-empty response element + generating flag.
func probeScript(adapter *ProviderAdapter) string {
	markers, _ := json.Marshal(verificationMarkers)
	return fmt.Sprintf(`(() => {
const respSelectors = %s;
const genSelectors = %s;
const markers = %s;
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
const pageText = ((document.title || '') + ' ' + (document.body ? document.body.innerText : '')).toLowerCase();
let verification = false, marker = '';
for (const m of markers) { if (pageText.includes(m.toLowerCase())) { verification = true; marker = m; break; } }
return {content, generating, verification, marker};
})()`, jsSelectorList(adapter.ResponseSelectors), jsSelectorList(adapter.GeneratingSelectors), string(markers))
}

const pageReadyScript = `(() => ({ready: document.readyState === 'complete'}))()`

func verifyDetectScript() string {
	markers, _ := json.Marshal(verificationMarkers)
	return fmt.Sprintf(`(() => {
const markers = %s;
const text = ((document.title || '') + ' ' + (document.body ? document.body.innerText : '')).toLowerCase();
for (const m of markers) { if (text.includes(m.toLowerCase())) return {flagged: true, marker: m}; }
return {flagged: false, marker: ''};
})()`, string(markers))
}

// ---------------- BrowserOps bridge ----------------

// BrowserOps is the governed seam to the tool window's webview — the
// Qt UI executes these ops and returns results over the WS channel.
type BrowserOps interface {
	CreateSession(ownerModule, targetURL, sessionID string) (map[string]any, error)
	Navigate(sessionID, targetURL string) (map[string]any, error)
	ExecuteScript(sessionID, script string) (map[string]any, error)
	GetURL(sessionID string) (map[string]any, error)
	Close(sessionID string) (map[string]any, error)
}

func opsOK(res map[string]any, err error) (map[string]any, bool) {
	if err != nil || res == nil {
		return nil, false
	}
	ok, _ := res["ok"].(bool)
	return res, ok
}

// BrowserSession is the BrowserAutomationSession port.
type BrowserSession struct {
	ops         BrowserOps
	mu          sync.Mutex
	sessions    map[string]string // session_id -> url
	cancelFlags map[string]bool
	lastBackend string
	// PollInterval is the wait between response polls (1 s production).
	PollInterval time.Duration
}

func NewBrowserSession(ops BrowserOps) *BrowserSession {
	return &BrowserSession{
		ops:          ops,
		sessions:     map[string]string{},
		cancelFlags:  map[string]bool{},
		PollInterval: time.Second,
	}
}

// SessionState mirrors session_state(agent_id).
func (b *BrowserSession) SessionState(agentID string) map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	sessionID := "ai-collaboration-" + agentID
	url, ok := b.sessions[sessionID]
	if !ok {
		return map[string]any{"session_state": "closed", "login_state": "unknown"}
	}
	if url == "" {
		return map[string]any{"session_state": "expired", "login_state": "unknown"}
	}
	return map[string]any{
		"session_state": "open", "login_state": "unknown",
		"url": url, "backend": "webview-op",
	}
}

// OpenAgent mirrors open_agent.
func (b *BrowserSession) OpenAgent(agent map[string]any) map[string]any {
	agentID := str(agent, "agent_id")
	home := str(agent, "home_url")
	sessionID := "ai-collaboration-" + agentID
	res, err := b.ensureSession(agentID, home, sessionID)
	if err != nil {
		return browserFailure(err)
	}
	_ = res
	return map[string]any{
		"ok": true, "url": home, "mode": defaultSessionMode,
		"automation": true, "foreground": true,
		"open_target": defaultSessionMode, "shared_browser_context": true,
		"session_id":        sessionID,
		"provider_identity": str(agent, "provider"),
		"adapter_version":   domain.AdapterVersion,
	}
}

func (b *BrowserSession) ensureSession(agentID, homeURL, sessionID string) (map[string]any, error) {
	if agentID == "" || !strings.HasPrefix(homeURL, "https://") {
		return nil, fmt.Errorf("INVALID_BROWSER_AGENT")
	}
	b.mu.Lock()
	if _, ok := b.sessions[sessionID]; ok {
		b.mu.Unlock()
		return map[string]any{"ok": true, "id": sessionID}, nil
	}
	b.mu.Unlock()
	res, err := b.ops.CreateSession(browserOwnerModule, homeURL, sessionID)
	if err != nil {
		return nil, fmt.Errorf("EMBEDDED_BROWSER_SESSION_FAILED:%v", err)
	}
	if ok, _ := res["ok"].(bool); !ok {
		msg := str(res, "message")
		if msg == "" {
			msg = "EMBEDDED_BROWSER_SESSION_FAILED"
		}
		return nil, fmt.Errorf("%s", msg)
	}
	b.mu.Lock()
	b.sessions[sessionID] = homeURL
	b.mu.Unlock()
	return res, nil
}

// PrepareSend mirrors prepare_send: ensure session, provider page, page
// ready, verification detect. Returns (sessionID, adapter, earlyResult).
func (b *BrowserSession) PrepareSend(agent map[string]any) (string, *ProviderAdapter, map[string]any) {
	provider := str(agent, "provider")
	adapter := adapterFor(provider)
	if adapter == nil {
		return "", nil, failedResult(provider, "UNSUPPORTED_BROWSER_PROVIDER", "")
	}
	home := str(agent, "home_url")
	if !strings.HasPrefix(home, "https://") {
		return "", nil, failedResult(provider, "INVALID_EXTERNAL_URL", "")
	}
	sessionID := "ai-collaboration-" + str(agent, "agent_id")
	if _, err := b.ensureSession(str(agent, "agent_id"), home, sessionID); err != nil {
		return "", nil, failedResult(provider, "EMBEDDED_BROWSER_SESSION_FAILED", err.Error())
	}
	// Provider page: navigate when the current URL is off-host.
	urlRes, err := b.ops.GetURL(sessionID)
	if err == nil {
		current := str(urlRes, "url")
		if !adapter.matchesHost(current) {
			nav, err := b.ops.Navigate(sessionID, home)
			if err != nil || !isOK(nav) {
				return "", nil, failedResult(provider, "EMBEDDED_BROWSER_SESSION_FAILED", "")
			}
			b.mu.Lock()
			b.sessions[sessionID] = home
			b.mu.Unlock()
		}
	}
	// Page-ready wait: 60 × 0.5 s.
	for i := 0; i < 60; i++ {
		res, err := b.ops.ExecuteScript(sessionID, pageReadyScript)
		if err == nil && isOK(res) {
			if m, ok := res["result"].(map[string]any); ok {
				if ready, _ := m["ready"].(bool); ready {
					break
				}
			}
		}
		time.Sleep(500 * time.Millisecond)
	}
	// Verification detect.
	if res, err := b.ops.ExecuteScript(sessionID, verifyDetectScript()); err == nil && isOK(res) {
		if m, ok := res["result"].(map[string]any); ok {
			if flagged, _ := m["flagged"].(bool); flagged {
				return "", nil, verificationResult(provider, str(m, "marker"), false)
			}
		}
	}
	return sessionID, adapter, nil
}

// SubmitPrompt mirrors submit_prompt — fill (retry ≤30 s) then submit.
func (b *BrowserSession) SubmitPrompt(sessionID, provider, prompt string, adapter *ProviderAdapter) map[string]any {
	b.mu.Lock()
	delete(b.cancelFlags, sessionID)
	b.mu.Unlock()
	found := false
	for i := 0; i < pageLoadWaitSeconds; i++ {
		res, err := b.ops.ExecuteScript(sessionID, fillScript(adapter, prompt))
		if err == nil && isOK(res) {
			if m, ok := res["result"].(map[string]any); ok {
				if f, _ := m["found"].(bool); f {
					found = true
					break
				}
			}
		}
		time.Sleep(time.Second)
	}
	if !found {
		return waitingResult(provider, "BROWSER_LOGIN_OR_INPUT_REQUIRED", false, "")
	}
	res, err := b.ops.ExecuteScript(sessionID, submitScript(adapter))
	if err != nil || !isOK(res) {
		return waitingResult(provider, "BROWSER_SEND_CONTROL_NOT_FOUND", true, "")
	}
	if m, ok := res["result"].(map[string]any); ok {
		if sent, _ := m["sent"].(bool); sent {
			return nil
		}
	}
	return waitingResult(provider, "BROWSER_SEND_CONTROL_NOT_FOUND", true, "")
}

// WaitForResponse mirrors wait_for_response: ≤120 polls × ~1 s, stable
// polling, cancel flag, verification re-detect.
func (b *BrowserSession) WaitForResponse(sessionID string, adapter *ProviderAdapter) (string, string, string) {
	lastContent := ""
	stable := 0
	for i := 0; i < responseTimeoutSeconds; i++ {
		b.mu.Lock()
		cancelled := b.cancelFlags[sessionID]
		b.mu.Unlock()
		if cancelled {
			return "response_cancelled", lastContent, ""
		}
		res, err := b.ops.ExecuteScript(sessionID, probeScript(adapter))
		if err == nil && isOK(res) {
			if m, ok := res["result"].(map[string]any); ok {
				if v, _ := m["verification"].(bool); v {
					return "response_failed", "", str(m, "marker")
				}
				content, _ := m["content"].(string)
				generating, _ := m["generating"].(bool)
				if generating {
					stable = 0
					if content != "" {
						lastContent = content
					}
				} else {
					if content != "" && content == lastContent {
						stable++
						if stable >= responseStablePolls && len(content) > 10 {
							return "response_completed", content, ""
						}
					} else {
						stable = 0
						if content != "" {
							lastContent = content
						}
					}
				}
			}
		}
		time.Sleep(b.PollInterval)
	}
	return "response_timeout", lastContent, ""
}

// RequestCancel sets the cancel flag for a session.
func (b *BrowserSession) RequestCancel(sessionID string) {
	b.mu.Lock()
	b.cancelFlags[sessionID] = true
	b.mu.Unlock()
}

// ProbeReady is a DOM fact: any input selector present.
func (b *BrowserSession) ProbeReady(sessionID string, adapter *ProviderAdapter) bool {
	script := fmt.Sprintf(`(() => {
for (const sel of %s) { if (document.querySelector(sel)) return {ready: true}; }
return {ready: false};
})()`, jsSelectorList(adapter.InputSelectors))
	res, err := b.ops.ExecuteScript(sessionID, script)
	if err != nil || !isOK(res) {
		return false
	}
	if m, ok := res["result"].(map[string]any); ok {
		ready, _ := m["ready"].(bool)
		return ready
	}
	return false
}

// Shutdown closes all live sessions.
func (b *BrowserSession) Shutdown() {
	b.mu.Lock()
	ids := make([]string, 0, len(b.sessions))
	for id := range b.sessions {
		ids = append(ids, id)
	}
	b.sessions = map[string]string{}
	b.mu.Unlock()
	for _, id := range ids {
		_, _ = b.ops.Close(id)
	}
}

func isOK(res map[string]any) bool {
	ok, _ := res["ok"].(bool)
	return ok
}

// ---------------- result shapes ----------------

func baseResult(provider string) map[string]any {
	return map[string]any{
		"provider":          provider,
		"transport":         defaultSessionMode,
		"uses_api_key":      false,
		"memory_candidates": []any{},
		"adapter_version":   domain.AdapterVersion,
		"fallback": map[string]any{
			"used": false, "browser_only": true,
			"cross_provider_substitution": false,
		},
	}
}

func completedResult(provider, content, responseState string) map[string]any {
	r := baseResult(provider)
	r["status"] = "completed"
	r["content"] = content
	r["error"] = ""
	r["error_code"] = ""
	r["response_state"] = responseState
	r["browser_handoff"] = map[string]any{
		"submitted": true, "send_method": "embedded-js-click",
		"response_captured": true, "response_state": responseState,
	}
	return r
}

func waitingResult(provider, code string, submitted bool, marker string) map[string]any {
	r := baseResult(provider)
	r["status"] = "awaiting-user"
	r["content"] = ""
	r["error"] = code
	r["error_code"] = code
	r["browser_handoff"] = map[string]any{
		"submitted": submitted, "send_method": "embedded-js-click",
		"response_captured": false, "manual_import_available": true,
	}
	return r
}

func verificationResult(provider, marker string, submitted bool) map[string]any {
	r := baseResult(provider)
	r["status"] = "waiting_verification"
	r["content"] = ""
	r["error"] = "BROWSER_VERIFICATION_REQUIRED:" + marker
	r["error_code"] = "BROWSER_VERIFICATION_REQUIRED"
	r["response_state"] = "response_failed"
	r["browser_handoff"] = map[string]any{
		"submitted": submitted, "send_method": "embedded-js-click",
		"response_captured": false, "verification_marker": marker,
		"manual_import_available": true,
	}
	return r
}

func failedResult(provider, code, detail string) map[string]any {
	r := baseResult(provider)
	r["status"] = "failed"
	errText := code
	if detail != "" {
		errText = code + ":" + detail
	}
	r["error"] = errText
	r["error_code"] = code
	r["content"] = ""
	return r
}

func browserFailure(err error) map[string]any {
	msg := err.Error()
	code := "BROWSER_ERROR"
	if strings.HasPrefix(msg, "EMBEDDED_BROWSER_SESSION_FAILED") {
		code = "EMBEDDED_BROWSER_SESSION_FAILED"
	} else if strings.HasPrefix(msg, "BROWSER_VERIFICATION_REQUIRED") {
		code = "BROWSER_VERIFICATION_REQUIRED"
	}
	return map[string]any{"ok": false, "error_code": code, "message": msg}
}

// ---------------- provider runtime pool ----------------

var providerTerminalStates = map[string]bool{
	"completed": true, "failed": true, "cancelled": true,
}

// ProviderRuntime mirrors ProviderRuntime — one per provider, requests
// serialize through the runtime's own lock.
type ProviderRuntime struct {
	provider    string
	adapter     *ProviderAdapter
	browser     *BrowserSession
	registered  string
	mu          sync.Mutex
	state       string
	generation  int
	requestID   string
	lastSuccess string
	lastError   string
	faultRef    string
	transitions []map[string]any
}

func newProviderRuntime(provider, registeredURL string, browser *BrowserSession) *ProviderRuntime {
	return &ProviderRuntime{
		provider: provider, adapter: adapterFor(provider),
		browser: browser, registered: registeredURL, state: "uninitialized",
	}
}

func (rt *ProviderRuntime) transition(to, requestID, faultRef string) {
	rt.transitions = append(rt.transitions, map[string]any{
		"provider_id": rt.provider, "from_state": rt.state, "to_state": to,
		"request_id": requestID, "generation": rt.generation,
		"timestamp": repo.UtcNow(), "fault_reference": faultRef,
	})
	if len(rt.transitions) > 64 {
		rt.transitions = rt.transitions[len(rt.transitions)-64:]
	}
	rt.state = to
}

// Submit mirrors ProviderRuntime.submit.
func (rt *ProviderRuntime) Submit(agent map[string]any, prompt, requestID string) map[string]any {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	rt.generation++
	rt.requestID = requestID
	rt.transition("submitting", requestID, "")
	sessionID, adapter, early := rt.browser.PrepareSend(agent)
	if adapter == nil && early != nil {
		return rt.earlyFinish(early)
	}
	if early != nil {
		return rt.earlyFinish(early)
	}
	if res := rt.browser.SubmitPrompt(sessionID, rt.provider, prompt, adapter); res != nil {
		return rt.earlyFinish(res)
	}
	rt.transition("generating", requestID, "")
	rt.transition("capturing", requestID, "")
	state, content, marker := rt.browser.WaitForResponse(sessionID, adapter)
	result := rt.captureOutcome(sessionID, state, content, marker)
	return rt.finish(result)
}

func (rt *ProviderRuntime) earlyFinish(res map[string]any) map[string]any {
	code := str(res, "error_code")
	status := str(res, "status")
	switch {
	case code == "BROWSER_LOGIN_OR_INPUT_REQUIRED" || code == "BROWSER_VERIFICATION_REQUIRED" || status == "waiting_verification":
		rt.transition("login_required", rt.requestID, code)
	case code != "":
		rt.transition("failed", rt.requestID, code)
	default:
		rt.transition("ready", rt.requestID, "")
	}
	res["provider_runtime"] = rt.health()
	return res
}

func (rt *ProviderRuntime) captureOutcome(sessionID, state, content, marker string) map[string]any {
	if marker != "" {
		return verificationResult(rt.provider, marker, true)
	}
	switch state {
	case "response_cancelled":
		r := waitingResult(rt.provider, "REQUEST_CANCELLED", true, "")
		r["status"] = "cancelled"
		return r
	case "response_completed":
		if content != "" {
			return completedResult(rt.provider, content, state)
		}
		return waitingResult(rt.provider, "BROWSER_RESPONSE_CAPTURE_REQUIRED", true, "")
	default:
		r := waitingResult(rt.provider, "BROWSER_RESPONSE_CAPTURE_REQUIRED", true, "")
		r["response_state"] = state
		return r
	}
}

func (rt *ProviderRuntime) finish(res map[string]any) map[string]any {
	status := str(res, "status")
	code := str(res, "error_code")
	switch {
	case status == "completed":
		rt.transition("completed", rt.requestID, "")
		rt.lastSuccess = repo.UtcNow()
		rt.lastError = ""
		rt.faultRef = ""
	case status == "cancelled":
		rt.transition("cancelled", rt.requestID, "REQUEST_CANCELLED")
	case status == "awaiting-user" || status == "waiting_verification":
		if code == "BROWSER_LOGIN_OR_INPUT_REQUIRED" || status == "waiting_verification" {
			rt.transition("login_required", rt.requestID, code)
		} else {
			rt.transition("ready", rt.requestID, code)
		}
		rt.lastError = code
		rt.faultRef = code
	default:
		rt.transition("failed", rt.requestID, code)
		rt.lastError = code
		rt.faultRef = code
	}
	res["provider_runtime"] = rt.health()
	return res
}

// Cancel mirrors runtime.cancel(request_id).
func (rt *ProviderRuntime) Cancel(requestID string) map[string]any {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	rt.browser.RequestCancel("ai-collaboration-" + rt.provider)
	if !providerTerminalStates[rt.state] {
		rt.transition("cancelled", requestID, "REQUEST_CANCELLED")
	}
	return map[string]any{
		"ok": true, "provider_id": rt.provider,
		"cancel_remote_state": "unconfirmed",
	}
}

func (rt *ProviderRuntime) health() map[string]any {
	health := "idle"
	switch rt.state {
	case "failed":
		health = "failed"
	case "ready", "completed":
		health = "healthy"
	case "submitting", "generating", "capturing", "opening":
		health = "busy"
	}
	var caps []any
	if rt.adapter != nil {
		caps = []any{rt.adapter.SendCapability, rt.adapter.ResponseCaptureCapability}
	} else {
		caps = []any{}
	}
	display := rt.provider
	if rt.adapter != nil {
		display = rt.adapter.DisplayName
	}
	version := ""
	if rt.adapter != nil {
		version = rt.adapter.AdapterVersion
	}
	sessionState := "closed"
	return map[string]any{
		"provider_id": rt.provider, "display_name": display,
		"registered_url": rt.registered, "adapter_version": version,
		"supported_capabilities": caps, "state": rt.state,
		"health_state": health, "request_id": rt.requestID,
		"generation": rt.generation, "session_state": sessionState,
		"last_success": rt.lastSuccess, "last_error": rt.lastError,
		"fault_reference": rt.faultRef,
	}
}

// Health returns the health snapshot (lock-free read of last fields).
func (rt *ProviderRuntime) Health() map[string]any {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	return rt.health()
}

// ProviderRuntimePool mirrors ProviderRuntimePool.
type ProviderRuntimePool struct {
	browser  *BrowserSession
	mu       sync.Mutex
	runtimes map[string]*ProviderRuntime
}

func NewProviderRuntimePool(browser *BrowserSession) *ProviderRuntimePool {
	return &ProviderRuntimePool{browser: browser, runtimes: map[string]*ProviderRuntime{}}
}

// RuntimeFor returns (or lazily creates) the runtime for a provider;
// nil when the provider has no adapter.
func (p *ProviderRuntimePool) RuntimeFor(provider, registeredURL string) *ProviderRuntime {
	key := strings.ToLower(provider)
	if adapterFor(key) == nil {
		return nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	rt, ok := p.runtimes[key]
	if !ok {
		rt = newProviderRuntime(key, registeredURL, p.browser)
		p.runtimes[key] = rt
	}
	if rt.registered == "" && registeredURL != "" {
		rt.registered = registeredURL
	}
	return rt
}

// Health returns every runtime's health snapshot.
func (p *ProviderRuntimePool) Health() []map[string]any {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]map[string]any, 0, len(p.runtimes))
	for _, rt := range p.runtimes {
		out = append(out, rt.Health())
	}
	return out
}

// CancelAll cancels every runtime.
func (p *ProviderRuntimePool) CancelAll(requestID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, rt := range p.runtimes {
		rt.Cancel(requestID)
	}
}
