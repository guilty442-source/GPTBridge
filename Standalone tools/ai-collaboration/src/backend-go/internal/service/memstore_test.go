package service

import (
	"context"
	"encoding/json"
	"sort"
	"sync"

	"gptbridge.local/ai-collaboration-backend/internal/repo"
)

// memStore is an in-memory Repository for tests — mirrors the PG
// semantics the service relies on (ordering, selection, pending rows).
type memStore struct {
	mu        sync.Mutex
	agents    []map[string]any
	messages  []map[string]any
	responses map[string][]map[string]any // message_id -> responses
	memory    []map[string]any
	tasks     []map[string]any
	collab    map[string]map[string]any
	results   map[string][]map[string]any // task_id -> results
}

func newMemStore() *memStore {
	m := &memStore{
		responses: map[string][]map[string]any{},
		collab:    map[string]map[string]any{},
		results:   map[string][]map[string]any{},
	}
	seeds := []struct {
		id, name, provider, url string
		caps                    []any
	}{
		{"chatgpt", "ChatGPT", "chatgpt", "https://chatgpt.com/", []any{"general", "comprehensive", "orchestration"}},
		{"claude", "Claude", "claude", "https://claude.ai/", []any{"general", "longform"}},
		{"gemini", "Gemini", "gemini", "https://gemini.google.com/", []any{"general", "search"}},
		{"grok", "Grok", "grok", "https://grok.com/", []any{"general", "social_media"}},
		{"deepseek", "DeepSeek", "deepseek", "https://chat.deepseek.com/", []any{"general", "reasoning"}},
		{"perplexity", "Perplexity", "perplexity", "https://www.perplexity.ai/", []any{"general", "advanced_search"}},
	}
	for i, s := range seeds {
		m.agents = append(m.agents, map[string]any{
			"agent_id": s.id, "name": s.name, "provider": s.provider,
			"home_url": s.url, "general_url": s.url, "investment_url": s.url,
			"star_training_url": "", "general_enabled": true, "investment_enabled": true,
			"business_capabilities": s.caps, "enabled": true, "selected": true,
			"status": "idle", "last_error": "", "session_state": "closed",
			"login_state": "unknown", "adapter_version": "", "sort_seq": i + 1,
			"updated_at": repo.UtcNow(),
		})
	}
	return m
}

func clone(m map[string]any) map[string]any {
	raw, _ := json.Marshal(m)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return out
}

func (m *memStore) DBPath() string { return "postgresql:gptbridge_collab" }

func intOf(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case float64:
		return int(n)
	}
	return 0
}

func (m *memStore) ListAgents(ctx context.Context) ([]map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := append([]map[string]any(nil), m.agents...)
	sort.SliceStable(out, func(i, j int) bool {
		if intOf(out[i]["sort_seq"]) == intOf(out[j]["sort_seq"]) {
			return out[i]["agent_id"].(string) < out[j]["agent_id"].(string)
		}
		return intOf(out[i]["sort_seq"]) < intOf(out[j]["sort_seq"])
	})
	for i := range out {
		out[i] = clone(out[i])
	}
	return out, nil
}

func (m *memStore) GetAgents(ctx context.Context, ids []string) ([]map[string]any, error) {
	all, err := m.ListAgents(ctx)
	if err != nil {
		return nil, err
	}
	byID := map[string]map[string]any{}
	for _, a := range all {
		byID[a["agent_id"].(string)] = a
	}
	var out []map[string]any
	for _, id := range ids {
		if a, ok := byID[id]; ok {
			out = append(out, a)
		}
	}
	return out, nil
}

func (m *memStore) GetAgent(ctx context.Context, agentID string) (map[string]any, error) {
	list, err := m.GetAgents(ctx, []string{agentID})
	if err != nil || len(list) == 0 {
		return nil, err
	}
	return list[0], nil
}

func (m *memStore) SaveAgentSelection(ctx context.Context, ids []string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	set := map[string]bool{}
	for _, id := range ids {
		set[id] = true
	}
	for _, a := range m.agents {
		a["selected"] = set[a["agent_id"].(string)]
	}
	return nil
}

func (m *memStore) MaxSortSeq(ctx context.Context) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	max := 0
	for _, a := range m.agents {
		if intOf(a["sort_seq"]) > max {
			max = intOf(a["sort_seq"])
		}
	}
	return max, nil
}

func (m *memStore) AgentNameExists(ctx context.Context, name string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, a := range m.agents {
		if a["name"] == name {
			return true, nil
		}
	}
	return false, nil
}

func (m *memStore) AgentIDExists(ctx context.Context, id string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, a := range m.agents {
		if a["agent_id"] == id {
			return true, nil
		}
	}
	return false, nil
}

func (m *memStore) AddAgent(ctx context.Context, a map[string]any) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	row := clone(a)
	row["general_url"] = row["home_url"]
	row["investment_url"] = row["home_url"]
	row["star_training_url"] = ""
	row["general_enabled"] = true
	row["investment_enabled"] = true
	row["enabled"] = true
	row["selected"] = false
	row["status"] = "idle"
	row["last_error"] = ""
	row["session_state"] = "closed"
	row["login_state"] = "unknown"
	row["adapter_version"] = ""
	row["updated_at"] = repo.UtcNow()
	m.agents = append(m.agents, row)
	return nil
}

func (m *memStore) SaveAgentBusinessSettings(ctx context.Context, agentID, generalURL, investmentURL, starTrainingURL string, generalEnabled, investmentEnabled bool, caps []string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, a := range m.agents {
		if a["agent_id"] == agentID {
			a["home_url"] = generalURL
			a["general_url"] = generalURL
			a["investment_url"] = investmentURL
			a["star_training_url"] = starTrainingURL
			a["general_enabled"] = generalEnabled
			a["investment_enabled"] = investmentEnabled
			out := make([]any, len(caps))
			for i, c := range caps {
				out[i] = c
			}
			a["business_capabilities"] = out
			a["updated_at"] = repo.UtcNow()
			return nil
		}
	}
	return nil
}

func (m *memStore) UpdateAgentStatus(ctx context.Context, agentID, status, lastError string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, a := range m.agents {
		if a["agent_id"] == agentID {
			a["status"] = status
			a["last_error"] = lastError
			a["updated_at"] = repo.UtcNow()
		}
	}
	return nil
}

func (m *memStore) UpdateAgentRuntimeState(ctx context.Context, agentID string, session, login, version *string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, a := range m.agents {
		if a["agent_id"] == agentID {
			if session != nil {
				a["session_state"] = *session
			}
			if login != nil {
				a["login_state"] = *login
			}
			if version != nil {
				a["adapter_version"] = *version
			}
			a["updated_at"] = repo.UtcNow()
		}
	}
	return nil
}

func (m *memStore) CreateGroupMessage(ctx context.Context, content string, selected []string, scope, requestID, generation string) (map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
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
	m.messages = append(m.messages, msg)
	for _, agentID := range selected {
		m.responses[mid] = append(m.responses[mid], map[string]any{
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
	return clone(msg), nil
}

func (m *memStore) UpdateResponse(ctx context.Context, messageID, agentID string, u repo.ResponseUpdate) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, r := range m.responses[messageID] {
		if r["agent_id"] != agentID {
			continue
		}
		r["status"] = u.Status
		r["content"] = u.Content
		r["error"] = u.Error
		r["error_code"] = u.ErrorCode
		if u.ExecutionProvider != "" {
			r["execution_provider"] = u.ExecutionProvider
		}
		if u.Transport != "" {
			r["transport"] = u.Transport
		}
		if u.Fallback != nil {
			r["fallback"] = u.Fallback
		}
		if u.MemoryCandidates != nil {
			r["memory_candidates"] = u.MemoryCandidates
		}
		if u.ResponseState != "" {
			r["response_state"] = u.ResponseState
		}
		if repo.TaskStatuses[u.Status] {
			// not the right set; terminal check below
		}
		if u.Status == "completed" || u.Status == "failed" || u.Status == "cancelled" {
			r["completed_at"] = repo.UtcNow()
		}
		r["updated_at"] = repo.UtcNow()
	}
	return nil
}

func (m *memStore) CancelPendingResponses(ctx context.Context, messageID string) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	n := 0
	for _, r := range m.responses[messageID] {
		st := r["status"].(string)
		if st != "completed" && st != "failed" && st != "cancelled" {
			r["status"] = "cancelled"
			r["error"] = "REQUEST_CANCELLED"
			r["error_code"] = "REQUEST_CANCELLED"
			n++
		}
	}
	return n, nil
}

func (m *memStore) ListMessages(ctx context.Context, limit int, messageID string) ([]map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []map[string]any
	for _, msg := range m.messages {
		if messageID != "" && msg["message_id"].(string) != messageID {
			continue
		}
		c := clone(msg)
		responses := []any{}
		for _, r := range m.responses[msg["message_id"].(string)] {
			responses = append(responses, clone(r))
		}
		c["responses"] = responses
		out = append(out, c)
	}
	if messageID == "" && len(out) > limit {
		out = out[len(out)-limit:]
	}
	return out, nil
}

func (m *memStore) GetMessage(ctx context.Context, messageID string) (map[string]any, error) {
	list, err := m.ListMessages(ctx, 1, messageID)
	if err != nil || len(list) == 0 {
		return nil, err
	}
	return list[0], nil
}

func (m *memStore) ListMemoryItems(ctx context.Context) ([]map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := append([]map[string]any(nil), m.memory...)
	for i := range out {
		out[i] = clone(out[i])
	}
	return out, nil
}

func (m *memStore) AddMemoryItem(ctx context.Context, kind, title, content, scope, srcAgent, srcMsg, hash string) (map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	item := map[string]any{
		"memory_id": repo.NewID(16), "kind": kind, "title": title,
		"content": content, "business_scope": scope,
		"source_agent_id": srcAgent, "content_hash": hash,
		"source_message_id": srcMsg,
		"created_at":        repo.UtcNow(), "updated_at": repo.UtcNow(),
	}
	m.memory = append(m.memory, item)
	return clone(item), nil
}

func (m *memStore) ListTasks(ctx context.Context) ([]map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := append([]map[string]any(nil), m.tasks...)
	for i := range out {
		out[i] = clone(out[i])
	}
	return out, nil
}

func (m *memStore) CreateTask(ctx context.Context, title, srcMsg string, participants []string) (map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	pa := make([]any, len(participants))
	for i, p := range participants {
		pa[i] = p
	}
	task := map[string]any{
		"task_id": repo.NewID(16), "title": title, "status": "pending",
		"source_message_id": srcMsg, "participant_agents": pa,
		"files": []any{}, "conclusion": "",
		"created_at": repo.UtcNow(), "updated_at": repo.UtcNow(),
	}
	m.tasks = append(m.tasks, task)
	return clone(task), nil
}

func (m *memStore) CreateCollabTask(ctx context.Context, requestID, mode string, providers []string, original, generation, attemptID string) (map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	id := repo.NewID(16)
	if len(original) > 64000 {
		original = original[:64000]
	}
	sel := make([]any, len(providers))
	for i, p := range providers {
		sel[i] = p
	}
	m.collab[id] = map[string]any{
		"task_id": id, "request_id": requestID, "mode": mode,
		"selected_providers": sel, "original_request": original,
		"task_generation": generation, "attempt_id": attemptID,
		"overall_status": "created", "comparison": map[string]any{},
		"synthesis": map[string]any{}, "summary_reference": "",
		"fault_reference": "", "created_at": repo.UtcNow(),
		"started_at": "", "completed_at": "", "updated_at": repo.UtcNow(),
	}
	return clone(m.collab[id]), nil
}

func (m *memStore) UpdateCollabTask(ctx context.Context, taskID string, u repo.CollabTaskUpdate) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	t, ok := m.collab[taskID]
	if !ok {
		return nil
	}
	if u.Status != "" {
		t["overall_status"] = u.Status
	}
	if u.RequestID != "" {
		t["request_id"] = u.RequestID
	}
	if u.AttemptID != "" {
		t["attempt_id"] = u.AttemptID
	}
	if u.Comparison != nil {
		t["comparison"] = u.Comparison
	}
	if u.Synthesis != nil {
		t["synthesis"] = u.Synthesis
	}
	if u.SummaryReference != nil {
		t["summary_reference"] = *u.SummaryReference
	}
	if u.FaultReference != nil {
		t["fault_reference"] = *u.FaultReference
	}
	if u.Started && t["started_at"] == "" {
		t["started_at"] = repo.UtcNow()
	}
	if u.Completed {
		t["completed_at"] = repo.UtcNow()
	}
	t["updated_at"] = repo.UtcNow()
	return nil
}

func (m *memStore) GetCollabTask(ctx context.Context, taskID string) (map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	t, ok := m.collab[taskID]
	if !ok {
		return nil, nil
	}
	out := clone(t)
	results := []any{}
	for _, r := range m.results[taskID] {
		results = append(results, clone(r))
	}
	out["provider_results"] = results
	return out, nil
}

func (m *memStore) ListCollabTasks(ctx context.Context, limit int) ([]map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var ids []string
	for id := range m.collab {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	var out []map[string]any
	for _, id := range ids {
		out = append(out, clone(m.collab[id]))
	}
	return out, nil
}

func (m *memStore) InterruptedCollabTasks(ctx context.Context, generation string) ([]string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var ids []string
	for id, t := range m.collab {
		st := t["overall_status"].(string)
		if (st == "validating" || st == "running" || st == "aggregating") && t["task_generation"] != generation {
			ids = append(ids, id)
			completed := false
			for _, r := range m.results[id] {
				if r["response_status"] == "completed" {
					completed = true
				}
			}
			if completed {
				t["overall_status"] = "partial"
			} else {
				t["overall_status"] = "failed"
			}
			t["fault_reference"] = "RUNTIME_RESTART"
			t["completed_at"] = repo.UtcNow()
		}
	}
	return ids, nil
}

func (m *memStore) UpsertCollabResult(ctx context.Context, rec map[string]any) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	taskID := rec["task_id"].(string)
	key := rec["provider_id"].(string) + "|" + rec["attempt_id"].(string)
	list := m.results[taskID]
	for _, r := range list {
		if r["provider_id"].(string)+"|"+r["attempt_id"].(string) == key {
			r["response_text"] = rec["response_text"]
			r["response_status"] = rec["response_status"]
			r["capture_method"] = rec["capture_method"]
			r["captured_at"] = rec["captured_at"]
			r["completion_evidence"] = rec["completion_evidence"]
			r["suggested_actions"] = rec["suggested_actions"]
			return nil
		}
	}
	row := clone(rec)
	row["created_at"] = repo.UtcNow()
	m.results[taskID] = append(list, row)
	return nil
}

func (m *memStore) ListCollabResults(ctx context.Context, taskID string) ([]map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []map[string]any
	for _, r := range m.results[taskID] {
		out = append(out, clone(r))
	}
	return out, nil
}

func (m *memStore) CancelCollabResults(ctx context.Context, taskID string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, r := range m.results[taskID] {
		st := r["response_status"].(string)
		if st == "pending" || st == "running" || st == "awaiting-user" {
			r["response_status"] = "cancelled"
			r["completion_evidence"] = "cancel_remote_state=unconfirmed"
		}
	}
	return nil
}

func (m *memStore) CompletedResultProviders(ctx context.Context, taskID, attemptID string) (map[string]bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := map[string]bool{}
	for _, r := range m.results[taskID] {
		if r["response_status"] == "completed" && (attemptID == "" || r["attempt_id"] == attemptID) {
			out[r["provider_id"].(string)] = true
		}
	}
	return out, nil
}
