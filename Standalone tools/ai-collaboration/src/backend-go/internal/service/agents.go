// agents.go — agent commands and get_state; parity with
// collab_svc_agents / collab_repo_agents semantics.
package service

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"gptbridge.local/ai-collaboration-backend/internal/domain"
	"gptbridge.local/ai-collaboration-backend/internal/repo"
)

var allowedBusinessCapabilities = map[string]bool{
	"general": true, "comprehensive": true, "orchestration": true,
	"search": true, "advanced_search": true, "calculation": true,
	"longform": true, "reasoning": true, "social_media": true,
	"trends": true, "breaking_news": true, "google_retrieval": true,
}

func (s *Service) getState(ctx context.Context) (map[string]any, error) {
	agents, err := s.Repo.ListAgents(ctx)
	if err != nil {
		return nil, err
	}
	messages, err := s.Repo.ListMessages(ctx, 50, "")
	if err != nil {
		return nil, err
	}
	collabTasks, err := s.Repo.ListCollabTasks(ctx, 50)
	if err != nil {
		return nil, err
	}
	memoryItems, err := s.Repo.ListMemoryItems(ctx)
	if err != nil {
		return nil, err
	}
	tasks, err := s.Repo.ListTasks(ctx)
	if err != nil {
		return nil, err
	}
	var health []any
	if s.pool != nil {
		for _, h := range s.pool.Health() {
			health = append(health, h)
		}
	}
	if health == nil {
		health = []any{}
	}
	return map[string]any{
		"ok": true, "version": s.Version,
		"runtime_generation": s.RuntimeGen,
		"request_dedupe":     true,
		"provider_execution": map[string]any{
			"default_mode":                       defaultSessionMode,
			"release_after_request":              true,
			"uses_external_api":                  false,
			"browser_automation_for_inference":   true,
			"browser_only":                       true,
			"terminal_fallback":                  false,
			"task_planner":                       "star-main-native-model-only",
			"fixed_task_owners":                  domain.FixedTaskOwners,
			"maximum_parallel_ai":                maxParallelAI,
			"browser_wait_cycles_before_chatgpt": browserWaitCycles,
			"final_coordinator":                  "chatgpt-always",
			"providers":                          s.providerStatus(ctx),
		},
		"agents":          agents,
		"messages":        messages,
		"collab_tasks":    collabTasks,
		"provider_health": health,
		"memory_items":    memoryItems,
		"tasks":           tasks,
		"diagnostics":     s.diagnostics(agents, messages, memoryItems, tasks),
		"database_path":   s.Repo.DBPath(),
		"workspace_path":  s.ToolRoot,
		"safety_notice":   "所有外部 AI 協作皆在治理規則下進行：僅使用 Embedded BrowserView，不經由 CLI 或 API。",
	}, nil
}

// providerStatus mirrors AiCollaborationProviderSession.provider_status.
func (s *Service) providerStatus(ctx context.Context) []any {
	var providers []string
	for p := range browserPrimaryProviders {
		providers = append(providers, p)
	}
	sort.Strings(providers)
	var out []any
	for _, p := range providers {
		adapter := adapterFor(p)
		if adapter == nil {
			continue
		}
		sessionState := map[string]any{"session_state": "closed", "login_state": "unknown"}
		url := ""
		if agents, err := s.Repo.ListAgents(ctx); err == nil {
			for _, a := range agents {
				if str(a, "provider") == p {
					if st := s.Browser.SessionState(str(a, "agent_id")); st != nil {
						sessionState = st
						url = str(st, "url")
					}
					break
				}
			}
		}
		out = append(out, map[string]any{
			"provider": p, "provider_identity": adapter.ProviderIdentity,
			"display_name": adapter.DisplayName, "registered_url": "",
			"supported_capabilities":      []any{"browser_chat", "manual_result_import"},
			"session_state":               str(sessionState, "session_state"),
			"login_state":                 str(sessionState, "login_state"),
			"session_url":                 url,
			"send_capability":             adapter.SendCapability,
			"response_capture_capability": adapter.ResponseCaptureCapability,
			"adapter_version":             adapter.AdapterVersion,
			"status":                      "ready", "mode": defaultSessionMode,
			"installed": true, "uses_api_key": false,
			"background_resident": false, "browser_only": true,
			"automation": true, "foreground": true,
			"shared_browser_context": true, "terminal_fallback": false,
		})
	}
	if out == nil {
		out = []any{}
	}
	return out
}

func (s *Service) openAgent(ctx context.Context, p map[string]any) (map[string]any, error) {
	agentID := str(p, "agent_id")
	if agentID == "" {
		return nil, fmt.Errorf("找不到指定的 AI")
	}
	scope := str(p, "business_scope")
	if scope == "" {
		scope = "general"
	}
	if scope != "general" && scope != "investment" {
		return nil, fmt.Errorf("不支援的業務範圍")
	}
	agent, err := s.Repo.GetAgent(ctx, agentID)
	if err != nil {
		return nil, err
	}
	if agent == nil {
		return nil, fmt.Errorf("找不到指定的 AI")
	}
	scoped, err := s.agentForBusiness(agent, scope)
	if err != nil {
		return nil, err
	}
	res := s.Browser.OpenAgent(scoped)
	if ok, _ := res["ok"].(bool); ok {
		_ = s.Repo.UpdateAgentStatus(ctx, agentID, "opened", "")
		open := "open"
		version := str(res, "adapter_version")
		_ = s.Repo.UpdateAgentRuntimeState(ctx, agentID, &open, nil, &version)
	}
	out := cloneMap(res)
	out["agent_id"] = agentID
	return out, nil
}

func (s *Service) authorizeAgent(ctx context.Context, p map[string]any) (map[string]any, error) {
	agentID := str(p, "agent_id")
	if agentID == "" {
		return nil, fmt.Errorf("找不到指定的 AI")
	}
	agent, err := s.Repo.GetAgent(ctx, agentID)
	if err != nil {
		return nil, err
	}
	if agent == nil {
		return nil, fmt.Errorf("找不到指定的 AI")
	}
	provider := strings.ToLower(str(agent, "provider"))
	if !browserPrimaryProviders[provider] {
		return map[string]any{
			"ok": false, "agent_id": agentID,
			"message": "UNSUPPORTED_PROVIDER_AUTHORIZATION",
		}, nil
	}
	open := s.Browser.OpenAgent(agent)
	res := cloneMap(open)
	res["authorization_provider"] = provider + "-browser-session"
	res["mode"] = defaultSessionMode
	res["uses_api_key"] = false
	res["interactive_browser"] = true
	res["automatic_inference_fallback"] = nil
	if ok, _ := res["ok"].(bool); ok {
		status := "authorizing"
		if ib, _ := res["interactive_browser"].(bool); ib {
			status = "opened"
		}
		_ = s.Repo.UpdateAgentStatus(ctx, agentID, status, "")
	}
	res["agent_id"] = agentID
	return res, nil
}

func (s *Service) openSelectedAgents(ctx context.Context, p map[string]any) (map[string]any, error) {
	scope := str(p, "business_scope")
	if scope == "" {
		scope = "general"
	}
	ids := strList(p["agent_ids"])
	var agents []map[string]any
	var err error
	if len(ids) > 0 {
		agents, err = s.Repo.GetAgents(ctx, ids)
	} else {
		all, lerr := s.Repo.ListAgents(ctx)
		err = lerr
		for _, a := range all {
			if sel, _ := a["selected"].(bool); sel {
				if en, _ := a["enabled"].(bool); en {
					agents = append(agents, a)
				}
			}
		}
	}
	if err != nil {
		return nil, err
	}
	if len(agents) == 0 {
		return map[string]any{"ok": false, "message": "請先選取至少一個 AI"}, nil
	}
	var results []any
	opened := 0
	for _, agent := range agents {
		scoped, serr := s.agentForBusiness(agent, scope)
		agentID := str(agent, "agent_id")
		if serr != nil {
			_ = s.Repo.UpdateAgentStatus(ctx, agentID, "failed", serr.Error())
			results = append(results, map[string]any{
				"agent_id": agentID, "ok": false, "message": serr.Error(),
			})
			continue
		}
		res := s.Browser.OpenAgent(scoped)
		entry := cloneMap(res)
		entry["agent_id"] = agentID
		results = append(results, entry)
		if ok, _ := res["ok"].(bool); ok {
			opened++
			_ = s.Repo.UpdateAgentStatus(ctx, agentID, "opened", "")
			open := "open"
			version := str(res, "adapter_version")
			_ = s.Repo.UpdateAgentRuntimeState(ctx, agentID, &open, nil, &version)
		} else {
			msg := str(res, "message")
			if msg == "" {
				msg = str(res, "error")
			}
			_ = s.Repo.UpdateAgentStatus(ctx, agentID, "failed", msg)
		}
	}
	state, err := s.getState(ctx)
	if err != nil {
		return nil, err
	}
	state["ok"] = opened > 0
	state["opened"] = opened
	state["results"] = results
	state["message"] = fmt.Sprintf("已嘗試開啟 %d / %d 個 AI 分頁", opened, len(agents))
	return state, nil
}

func (s *Service) setAgentSelection(ctx context.Context, p map[string]any) (map[string]any, error) {
	ids := strList(p["agent_ids"])
	agents, err := s.Repo.ListAgents(ctx)
	if err != nil {
		return nil, err
	}
	known := map[string]bool{}
	for _, a := range agents {
		known[str(a, "agent_id")] = true
	}
	var selected []string
	for _, id := range ids {
		if known[id] {
			selected = append(selected, id)
		}
	}
	if err := s.Repo.SaveAgentSelection(ctx, selected); err != nil {
		return nil, err
	}
	agents, err = s.Repo.ListAgents(ctx)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"ok": true, "agents": agents, "selected_count": len(selected),
		"message": fmt.Sprintf("已選取 %d 個 AI", len(selected)),
	}, nil
}

var slugRe = regexp.MustCompile(`[^a-z0-9]+`)

func (s *Service) addAgent(ctx context.Context, p map[string]any) (map[string]any, error) {
	name := strings.TrimSpace(str(p, "name"))
	if name == "" {
		return nil, fmt.Errorf("請輸入 AI 名稱")
	}
	provider := str(p, "provider")
	if provider == "" {
		provider = "custom"
	}
	homeURL, err := validatedExternalURL(str(p, "home_url"))
	if err != nil {
		return nil, err
	}
	if exists, err := s.Repo.AgentNameExists(ctx, name); err != nil {
		return nil, err
	} else if exists {
		return nil, fmt.Errorf("已存在同名 AI")
	}
	slug := strings.Trim(slugRe.ReplaceAllString(strings.ToLower(name), "-"), "-")
	if slug == "" {
		slug = "custom-ai"
	}
	base := "custom-" + slug
	agentID := base
	for i := 2; ; i++ {
		if exists, err := s.Repo.AgentIDExists(ctx, agentID); err != nil {
			return nil, err
		} else if !exists {
			break
		}
		agentID = fmt.Sprintf("%s-%d", base, i)
	}
	maxSeq, err := s.Repo.MaxSortSeq(ctx)
	if err != nil {
		return nil, err
	}
	row := map[string]any{
		"agent_id": agentID, "name": name, "provider": provider,
		"home_url": homeURL, "business_capabilities": []string{"general"},
		"sort_seq": maxSeq + 1,
	}
	if err := s.Repo.AddAgent(ctx, row); err != nil {
		return nil, err
	}
	agent, err := s.Repo.GetAgent(ctx, agentID)
	if err != nil {
		return nil, err
	}
	agents, err := s.Repo.ListAgents(ctx)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"ok": true, "agent": agent, "agents": agents,
		"message": fmt.Sprintf("已新增 AI：%s", name),
	}, nil
}

func (s *Service) updateAgentBusinessSettings(ctx context.Context, p map[string]any) (map[string]any, error) {
	agentID := str(p, "agent_id")
	if agentID == "" {
		return nil, fmt.Errorf("找不到指定的 AI")
	}
	agent, err := s.Repo.GetAgent(ctx, agentID)
	if err != nil {
		return nil, err
	}
	if agent == nil {
		return nil, fmt.Errorf("找不到指定的 AI")
	}
	generalURL, err := validatedExternalURL(str(p, "general_url"))
	if err != nil {
		return nil, err
	}
	investmentURL, err := validatedExternalURL(str(p, "investment_url"))
	if err != nil {
		return nil, err
	}
	starURL := ""
	if agentID == "chatgpt" {
		if v := strings.TrimSpace(str(p, "star_training_url")); v != "" {
			if starURL, err = validatedExternalURL(v); err != nil {
				return nil, err
			}
		} else if existing := str(agent, "star_training_url"); existing != "" {
			starURL = existing
		} else {
			starURL = generalURL
		}
	}
	var caps []string
	for _, c := range strList(p["business_capabilities"]) {
		if allowedBusinessCapabilities[c] {
			caps = append(caps, c)
		}
	}
	if _, present := p["business_capabilities"]; present && len(caps) == 0 {
		return nil, fmt.Errorf("至少需要一個有效的業務能力")
	}
	if caps == nil {
		for _, c := range toStrings(agent["business_capabilities"]) {
			caps = append(caps, c)
		}
	}
	if err := s.Repo.SaveAgentBusinessSettings(ctx, agentID, generalURL,
		investmentURL, starURL,
		boolOf(p, "general_enabled", true), boolOf(p, "investment_enabled", true),
		caps); err != nil {
		return nil, err
	}
	updated, err := s.Repo.GetAgent(ctx, agentID)
	if err != nil {
		return nil, err
	}
	agents, err := s.Repo.ListAgents(ctx)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"ok": true, "agent": updated, "agents": agents,
		"message": fmt.Sprintf("%s 業務 URL 已更新", str(agent, "name")),
	}, nil
}

func toStrings(v any) []string {
	var out []string
	switch t := v.(type) {
	case []any:
		for _, item := range t {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
	case []string:
		out = t
	}
	return out
}

var _ = repo.UtcNow
