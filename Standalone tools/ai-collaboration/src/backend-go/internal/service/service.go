// Package service ports the retired AiCollaborationService (Python) to
// Go: the 16 ai_nexus_* commands, requester gates, idempotent send
// dedupe, runtime generation, interrupted-task tombstoning, and the
// <command>_result event contract.
package service

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"gptbridge.local/ai-collaboration-backend/internal/repo"
)

const maxParallelAI = 6

// COMMANDS — the exact 16-command ownership set.
var Commands = map[string]bool{
	"ai_nexus_get_state":                      true,
	"ai_nexus_open_agent":                     true,
	"ai_nexus_authorize_agent":                true,
	"ai_nexus_open_selected_agents":           true,
	"ai_nexus_set_agent_selection":            true,
	"ai_nexus_add_agent":                      true,
	"ai_nexus_update_agent_business_settings": true,
	"ai_nexus_send_message":                   true,
	"ai_nexus_collab_start":                   true,
	"ai_nexus_collab_cancel":                  true,
	"ai_nexus_collab_manual_result":           true,
	"ai_nexus_collab_resume":                  true,
	"ai_nexus_complete_browser_response":      true,
	"ai_nexus_add_memory":                     true,
	"ai_nexus_create_task":                    true,
	"ai_nexus_export_report":                  true,
}

// Repository is the store contract the service depends on (repo.PG in
// production; an in-memory implementation in tests).
type Repository interface {
	DBPath() string
	ListAgents(ctx context.Context) ([]map[string]any, error)
	GetAgents(ctx context.Context, ids []string) ([]map[string]any, error)
	GetAgent(ctx context.Context, agentID string) (map[string]any, error)
	SaveAgentSelection(ctx context.Context, ids []string) error
	MaxSortSeq(ctx context.Context) (int, error)
	AgentNameExists(ctx context.Context, name string) (bool, error)
	AgentIDExists(ctx context.Context, id string) (bool, error)
	AddAgent(ctx context.Context, a map[string]any) error
	SaveAgentBusinessSettings(ctx context.Context, agentID, generalURL, investmentURL, starTrainingURL string, generalEnabled, investmentEnabled bool, capabilities []string) error
	UpdateAgentStatus(ctx context.Context, agentID, status, lastError string) error
	UpdateAgentRuntimeState(ctx context.Context, agentID string, sessionState, loginState, adapterVersion *string) error
	CreateGroupMessage(ctx context.Context, content string, selectedAgents []string, scope, requestID, generation string) (map[string]any, error)
	UpdateResponse(ctx context.Context, messageID, agentID string, u repo.ResponseUpdate) error
	CancelPendingResponses(ctx context.Context, messageID string) (int, error)
	ListMessages(ctx context.Context, limit int, messageID string) ([]map[string]any, error)
	GetMessage(ctx context.Context, messageID string) (map[string]any, error)
	ListMemoryItems(ctx context.Context) ([]map[string]any, error)
	AddMemoryItem(ctx context.Context, kind, title, content, scope, sourceAgentID, sourceMessageID, contentHash string) (map[string]any, error)
	ListTasks(ctx context.Context) ([]map[string]any, error)
	CreateTask(ctx context.Context, title, sourceMessageID string, participants []string) (map[string]any, error)
	CreateCollabTask(ctx context.Context, requestID, mode string, providers []string, originalRequest, generation, attemptID string) (map[string]any, error)
	UpdateCollabTask(ctx context.Context, taskID string, u repo.CollabTaskUpdate) error
	GetCollabTask(ctx context.Context, taskID string) (map[string]any, error)
	ListCollabTasks(ctx context.Context, limit int) ([]map[string]any, error)
	InterruptedCollabTasks(ctx context.Context, generation string) ([]string, error)
	UpsertCollabResult(ctx context.Context, rec map[string]any) error
	ListCollabResults(ctx context.Context, taskID string) ([]map[string]any, error)
	CancelCollabResults(ctx context.Context, taskID string) error
	CompletedResultProviders(ctx context.Context, taskID, attemptID string) (map[string]bool, error)
}

// Service is the AiCollaborationService port.
type Service struct {
	Version    string
	ToolRoot   string
	Repo       Repository
	Browser    *BrowserSession
	RuntimeGen string

	sendMu      sync.Mutex
	taskSlots   chan struct{}
	browserWait map[string]chan string // "messageID|agentID" -> completion
	bwMu        sync.Mutex
	requestMsgs map[string]string // request_id -> message_id
	rmMu        sync.Mutex
	inflight    map[string]*dedupeFuture
	dedupe      map[string]dedupeEntry
	dedupeMu    sync.Mutex
	collabTasks map[string]*collabTrack
	ctMu        sync.Mutex
	poolOnce    sync.Once
	pool        *ProviderRuntimePool
}

type dedupeEntry struct {
	at     time.Time
	result map[string]any
}

type dedupeFuture struct {
	done chan struct{}
	res  map[string]any
}

type collabTrack struct {
	RequestID string
	Cancelled bool
}

// Owns reports whether the command belongs to this service.
func (s *Service) Owns(command string) bool { return Commands[command] }

// New constructs the service and tombstones interrupted tasks.
func New(version, toolRoot string, repository Repository, browser *BrowserSession) *Service {
	s := &Service{
		Version:     version,
		ToolRoot:    toolRoot,
		Repo:        repository,
		Browser:     browser,
		RuntimeGen:  repo.NewID(12),
		taskSlots:   make(chan struct{}, maxParallelAI),
		browserWait: map[string]chan string{},
		requestMsgs: map[string]string{},
		inflight:    map[string]*dedupeFuture{},
		dedupe:      map[string]dedupeEntry{},
		collabTasks: map[string]*collabTrack{},
	}
	if repository != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		_, _ = repository.InterruptedCollabTasks(ctx, s.RuntimeGen)
		cancel()
	}
	return s
}

func (s *Service) runtimePool() *ProviderRuntimePool {
	s.poolOnce.Do(func() {
		s.pool = NewProviderRuntimePool(s.Browser)
	})
	return s.pool
}

// Shutdown releases browser sessions.
func (s *Service) Shutdown() {
	if s.Browser != nil {
		s.Browser.Shutdown()
	}
}

// Handle dispatches a governed command and returns (event, result).
// Mirrors handle(): _governed_request_id → request_id propagation,
// unknown → UNSUPPORTED_COMMAND, permission → PERMISSION_DENIED,
// other → message with REQUEST_REJECTED normalization.
func (s *Service) Handle(ctx context.Context, command string, payload map[string]any) (string, map[string]any) {
	event := command + "_result"
	if !s.Owns(command) {
		return event, map[string]any{
			"ok": false, "error_code": "UNSUPPORTED_COMMAND",
			"message": "不支援的 AI 指令",
		}
	}
	if rid, ok := payload["_governed_request_id"].(string); ok && rid != "" {
		payload["request_id"] = rid
	}
	result, err := s.dispatch(ctx, command, payload)
	if err != nil {
		if errors.Is(err, errPermission) {
			result = map[string]any{"ok": false, "message": "PERMISSION_DENIED"}
		} else {
			result = map[string]any{"ok": false, "message": err.Error()}
		}
	}
	if ok, _ := result["ok"].(bool); !ok {
		if code := str(result, "error_code"); code == "" {
			if str(result, "message") == "PERMISSION_DENIED" {
				result["error_code"] = "PERMISSION_DENIED"
			} else {
				result["error_code"] = "REQUEST_REJECTED"
			}
		}
	}
	if rid, ok := payload["request_id"].(string); ok && rid != "" {
		result["request_id"] = rid
	}
	return event, result
}

func (s *Service) dispatch(ctx context.Context, command string, p map[string]any) (map[string]any, error) {
	switch command {
	case "ai_nexus_get_state":
		return s.getState(ctx)
	case "ai_nexus_open_agent":
		return s.openAgent(ctx, p)
	case "ai_nexus_authorize_agent":
		return s.authorizeAgent(ctx, p)
	case "ai_nexus_open_selected_agents":
		return s.openSelectedAgents(ctx, p)
	case "ai_nexus_set_agent_selection":
		return s.setAgentSelection(ctx, p)
	case "ai_nexus_add_agent":
		return s.addAgent(ctx, p)
	case "ai_nexus_update_agent_business_settings":
		return s.updateAgentBusinessSettings(ctx, p)
	case "ai_nexus_send_message":
		return s.sendMessage(ctx, p)
	case "ai_nexus_collab_start":
		return s.collabStart(ctx, p)
	case "ai_nexus_collab_cancel":
		return s.collabCancel(ctx, p)
	case "ai_nexus_collab_manual_result":
		return s.collabManualResult(ctx, p)
	case "ai_nexus_collab_resume":
		return s.collabResume(ctx, p)
	case "ai_nexus_complete_browser_response":
		return s.completeBrowserResponse(ctx, p)
	case "ai_nexus_add_memory":
		return s.addMemory(ctx, p)
	case "ai_nexus_create_task":
		return s.createTask(ctx, p)
	case "ai_nexus_export_report":
		return s.exportReport(ctx)
	}
	return nil, fmt.Errorf("unsupported")
}

// ---------------- helpers ----------------

var errPermission = errors.New("PERMISSION_DENIED")

func str(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	if v, ok := m[key].(string); ok {
		return v
	}
	return ""
}

func strList(v any) []string {
	var out []string
	switch t := v.(type) {
	case []any:
		for _, item := range t {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
	case []string:
		out = append(out, t...)
	}
	return out
}

func boolOf(m map[string]any, key string, def bool) bool {
	v, ok := m[key]
	if !ok {
		return def
	}
	if b, ok := v.(bool); ok {
		return b
	}
	return def
}

// requesterToolID — the _requester_tool_id permission gate.
func (s *Service) requesterToolID(payload map[string]any) (string, error) {
	actor := str(payload, "_authorized_requester_actor")
	switch actor {
	case "governance/tool/ai-assistant":
		return "", errPermission
	case "governance/tool/xingcheng":
		return "xingcheng", nil
	case "governance/tool/ai-collaboration", "governance/main-system":
		return "ai-collaboration", nil
	case "":
		return "xingcheng", nil // in-process/test path only
	default:
		return "", errPermission
	}
}

// agentForBusiness re-scopes an agent to a business scope: the scope
// must be enabled and carry a non-empty URL.
func (s *Service) agentForBusiness(agent map[string]any, scope string) (map[string]any, error) {
	if scope == "star-training" {
		url := str(agent, "star_training_url")
		if url == "" {
			return nil, fmt.Errorf("星澄訓練 URL 未設定")
		}
		out := cloneMap(agent)
		out["home_url"] = url
		out["conversation_scope"] = "star-training"
		return out, nil
	}
	key := scope + "_url"
	enabledKey := scope + "_enabled"
	if en, ok := agent[enabledKey].(bool); ok && !en {
		return nil, fmt.Errorf("此 AI 未啟用 %s 業務範圍", scope)
	}
	url := str(agent, key)
	if url == "" {
		return nil, fmt.Errorf("此 AI 未設定 %s URL", scope)
	}
	out := cloneMap(agent)
	out["home_url"] = url
	return out, nil
}

func cloneMap(m map[string]any) map[string]any {
	out := make(map[string]any, len(m)+2)
	for k, v := range m {
		out[k] = v
	}
	return out
}

// validatedExternalURL — mirrors _validated_external_url.
func validatedExternalURL(value string) (string, error) {
	v := strings.TrimSpace(value)
	u, err := url.Parse(v)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil {
		return "", fmt.Errorf("URL 必須為 https:// 且不可含帳號密碼")
	}
	host := strings.ToLower(u.Hostname())
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return "", fmt.Errorf("URL 不允許本機位址")
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		if !isGlobalIP(ip) {
			return "", fmt.Errorf("URL 不允許內部或保留位址")
		}
	} else if strings.HasSuffix(host, ".local") || strings.HasSuffix(host, ".internal") {
		return "", fmt.Errorf("URL 不允許內部位址")
	}
	return v, nil
}

func isGlobalIP(ip netip.Addr) bool {
	return ip.IsGlobalUnicast() && !ip.IsPrivate() && !ip.IsLoopback() &&
		!ip.IsLinkLocalUnicast() && !ip.IsMulticast() && !ip.IsUnspecified()
}

// ---------------- dedupe ----------------

const dedupeWindowSeconds = 120
const dedupeCap = 64
const dedupeEvict = 16
const requestMapCap = 256

func (s *Service) dedupeHit(payload map[string]any) (map[string]any, bool) {
	key := str(payload, "idempotency_key")
	if key == "" {
		return nil, false
	}
	s.dedupeMu.Lock()
	defer s.dedupeMu.Unlock()
	entry, ok := s.dedupe[key]
	if !ok || time.Since(entry.at) > dedupeWindowSeconds*time.Second {
		return nil, false
	}
	out := cloneMap(entry.result)
	out["deduplicated"] = true
	return out, true
}

func (s *Service) recordResult(payload map[string]any, result map[string]any) {
	key := str(payload, "idempotency_key")
	if key == "" {
		return
	}
	s.dedupeMu.Lock()
	defer s.dedupeMu.Unlock()
	if len(s.dedupe) > dedupeCap {
		type kv struct {
			k string
			t time.Time
		}
		var items []kv
		for k, e := range s.dedupe {
			items = append(items, kv{k, e.at})
		}
		sort.Slice(items, func(i, j int) bool { return items[i].t.Before(items[j].t) })
		for i := 0; i < dedupeEvict && i < len(items); i++ {
			delete(s.dedupe, items[i].k)
		}
	}
	s.dedupe[key] = dedupeEntry{at: time.Now(), result: cloneMap(result)}
}

// withDedupe wraps send/collab handlers with in-flight + recorded dedupe.
func (s *Service) withDedupe(payload map[string]any, inner func() (map[string]any, error)) (map[string]any, error) {
	if res, ok := s.dedupeHit(payload); ok {
		return res, nil
	}
	key := str(payload, "idempotency_key")
	if key != "" {
		s.dedupeMu.Lock()
		if f, ok := s.inflight[key]; ok {
			s.dedupeMu.Unlock()
			<-f.done
			out := cloneMap(f.res)
			out["deduplicated"] = true
			return out, nil
		}
		f := &dedupeFuture{done: make(chan struct{})}
		s.inflight[key] = f
		s.dedupeMu.Unlock()
		res, err := inner()
		f.res = res
		close(f.done)
		s.dedupeMu.Lock()
		delete(s.inflight, key)
		s.dedupeMu.Unlock()
		if err == nil {
			s.recordResult(payload, res)
		}
		return res, err
	}
	res, err := inner()
	if err == nil {
		s.recordResult(payload, res)
	}
	return res, err
}

func (s *Service) trackRequest(requestID, messageID string) {
	if requestID == "" {
		return
	}
	s.rmMu.Lock()
	defer s.rmMu.Unlock()
	if len(s.requestMsgs) > requestMapCap {
		s.requestMsgs = map[string]string{}
	}
	s.requestMsgs[requestID] = messageID
}

// Cancel mirrors cancel(request_id) — the toolbox_cancel_tool_run path.
func (s *Service) Cancel(ctx context.Context, requestID string) bool {
	s.rmMu.Lock()
	messageID, ok := s.requestMsgs[requestID]
	s.rmMu.Unlock()
	if !ok || messageID == "" {
		return false
	}
	if task, err := s.Repo.GetCollabTask(ctx, messageID); err == nil && task != nil {
		tid := str(task, "task_id")
		s.ctMu.Lock()
		if t, ok := s.collabTasks[tid]; ok {
			t.Cancelled = true
		}
		s.ctMu.Unlock()
		s.runtimePool().CancelAll(requestID)
		fault := "REQUEST_CANCELLED"
		_ = s.Repo.UpdateCollabTask(ctx, tid, repo.CollabTaskUpdate{
			Status: "cancelled", FaultReference: &fault, Completed: true,
		})
		return true
	}
	n, _ := s.Repo.CancelPendingResponses(ctx, messageID)
	cancelled := n > 0
	s.bwMu.Lock()
	for key, ch := range s.browserWait {
		if strings.HasPrefix(key, messageID+"|") {
			select {
			case ch <- "":
			default:
			}
			delete(s.browserWait, key)
			cancelled = true
		}
	}
	s.bwMu.Unlock()
	if agents, err := s.Repo.ListAgents(ctx); err == nil {
		for _, a := range agents {
			switch str(a, "status") {
			case "running", "awaiting-user", "waiting":
				_ = s.Repo.UpdateAgentStatus(ctx, str(a, "agent_id"), "idle", "")
			}
		}
	}
	return cancelled
}

var _ = net.IPv4
