// messaging.go — ai_nexus_send_message (general / fixed / tasks paths),
// browser-wait completion plumbing and auto-memory writeback.
// Parity with collab_svc_messaging.
package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"gptbridge.local/ai-collaboration-backend/internal/domain"
	"gptbridge.local/ai-collaboration-backend/internal/repo"
)

func (s *Service) sendMessage(ctx context.Context, p map[string]any) (map[string]any, error) {
	requester, err := s.requesterToolID(p)
	if err != nil {
		return nil, err
	}
	return s.withDedupe(p, func() (map[string]any, error) {
		tasks := toMapList(p["tasks"])
		if requester == "ai-collaboration" && len(tasks) > 0 {
			return nil, errPermission
		}
		if len(tasks) > 0 {
			return s.sendFixedTasks(ctx, p, tasks, requester)
		}
		if requester == "ai-collaboration" {
			return s.sendGeneralMessage(ctx, p)
		}
		return s.sendFixedMessage(ctx, p, requester)
	})
}

// ---------------- general path ----------------

func (s *Service) sendGeneralMessage(ctx context.Context, p map[string]any) (map[string]any, error) {
	content := strings.TrimSpace(str(p, "content"))
	if content == "" {
		return nil, fmt.Errorf("請輸入要交給 AI 協作的內容")
	}
	if str(p, "business_task") != "" && str(p, "business_task") != "general" {
		return nil, errPermission
	}
	if str(p, "research_pipeline") != "" {
		return nil, errPermission
	}
	ids := dedupeStrings(strList(p["agent_ids"]))
	if len(ids) == 0 {
		return nil, fmt.Errorf("單次請求至少需一個 AI")
	}
	if len(ids) > maxParallelAI {
		return nil, fmt.Errorf("單次請求最多支援 6 個 AI")
	}
	agents, err := s.Repo.GetAgents(ctx, ids)
	if err != nil {
		return nil, err
	}
	if len(agents) != len(ids) {
		return nil, fmt.Errorf("找不到指定的 AI")
	}
	for _, a := range agents {
		if en, _ := a["enabled"].(bool); !en {
			return nil, fmt.Errorf("所選 AI 未啟用")
		}
		ok := false
		for _, c := range toStrings(a["business_capabilities"]) {
			if c == "general" {
				ok = true
			}
		}
		if !ok {
			return nil, fmt.Errorf("所選 AI 不支援 general 業務")
		}
	}
	scope := str(p, "business_scope")
	if scope == "" {
		scope = "general"
	}
	requestID := str(p, "request_id")

	s.sendMu.Lock()
	message, err := s.Repo.CreateGroupMessage(ctx, content, ids, scope, requestID, s.RuntimeGen)
	s.sendMu.Unlock()
	if err != nil {
		return nil, err
	}
	messageID := str(message, "message_id")
	s.trackRequest(requestID, messageID)

	memCtx, _ := domain.SanitizeMemoryContext(p["memory_context"])
	var wg sync.WaitGroup
	for _, agent := range agents {
		wg.Add(1)
		s.taskSlots <- struct{}{}
		go func(a map[string]any) {
			defer wg.Done()
			defer func() { <-s.taskSlots }()
			s.runAgentMessage(ctx, messageID, a, content, "general", scope,
				requestID, "ai-collaboration", memCtx, true, false)
		}(agent)
	}
	wg.Wait()
	_ = s.browserCloseBackground()

	fresh, err := s.Repo.GetMessage(ctx, messageID)
	if err != nil {
		return nil, err
	}
	autoMemory := s.autoMemoryFromGroupMessage(ctx, fresh)
	return s.generalMessageResult(ctx, fresh, autoMemory, ids, "ai-collaboration")
}

func (s *Service) generalMessageResult(ctx context.Context, message map[string]any, autoMemory map[string]any, requested []string, requester string) (map[string]any, error) {
	messages, err := s.Repo.ListMessages(ctx, 50, "")
	if err != nil {
		return nil, err
	}
	agents, err := s.Repo.ListAgents(ctx)
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
	awaiting := 0
	var candidates []any
	for _, r := range toMapList(message["responses"]) {
		if str(r, "status") == "awaiting-user" {
			awaiting++
		}
		for _, c := range toMapList(r["memory_candidates"]) {
			candidates = append(candidates, c)
		}
	}
	if candidates == nil {
		candidates = []any{}
	}
	status := "completed"
	message_text := fmt.Sprintf("單次請求已送達 %d 個 AI 協作者", len(requested))
	if awaiting > 0 {
		status = "awaiting-browser-results"
		message_text = fmt.Sprintf("已送出請求；等待 %d 個 AI 的瀏覽器回覆，可稍後手動匯入。", awaiting)
	} else {
		failed := 0
		for _, r := range toMapList(message["responses"]) {
			if str(r, "status") == "failed" {
				failed++
			}
		}
		if failed == len(toMapList(message["responses"])) && failed > 0 {
			status = "attention-required"
		}
	}
	var autoMem any
	if autoMemory != nil {
		autoMem = autoMemory
	}
	var requestedAny []any
	for _, id := range requested {
		requestedAny = append(requestedAny, id)
	}
	return map[string]any{
		"ok": true, "message": message_text,
		"group_message": message, "auto_memory_item": autoMem,
		"messages": messages, "agents": agents,
		"memory_items": memoryItems, "tasks": tasks,
		"execution_mode":     "parallel-user-selected-general",
		"business_task":      "general",
		"selected_agents":    requestedAny,
		"requested_by":       "ai-collaboration",
		"response_recipient": "ai-collaboration",
		"memory_interchange": map[string]any{
			"mode":                   "tool-local-candidate-collection",
			"direct_database_access": false,
			"candidate_count":        len(candidates),
			"candidates":             candidates,
		},
		"external_coordinator": nil,
		"final_coordinator":    nil,
		"workflow_status":      status,
		"channel_coordinator":  "ai-collaboration",
		"fixed_workflow":       []any{},
	}, nil
}

// runAgentMessage mirrors _run_agent_message: envelope → provider send
// (or browser-wait) → response row update + agent status.
func (s *Service) runAgentMessage(ctx context.Context, messageID string, agent map[string]any, content, businessTask, scope, requestID, requester string, memCtx []map[string]any, writeback, waitForBrowser bool) {
	provider := str(agent, "provider")
	envelope, err := domain.BuildAITaskEnvelope(provider, scope, businessTask, content, requester, memCtx, writeback)
	if err != nil {
		_ = s.Repo.UpdateResponse(ctx, messageID, str(agent, "agent_id"), repo.ResponseUpdate{
			Status: "failed", Error: "PERMISSION_DENIED", ErrorCode: "PERMISSION_DENIED",
		})
		return
	}
	_ = s.Repo.UpdateResponse(ctx, messageID, str(agent, "agent_id"), repo.ResponseUpdate{
		Status: "running",
	})
	_ = s.Repo.UpdateAgentStatus(ctx, str(agent, "agent_id"), "running", "")
	var result map[string]any
	if !browserPrimaryProviders[provider] {
		result = failedResult(provider, "UNSUPPORTED_BROWSER_PROVIDER", "")
	} else if waitForBrowser {
		result = s.runWithBrowserWait(ctx, messageID, agent, str(envelope, "content"))
	} else {
		result = s.sendTask(agent, envelope)
	}
	s.storeAgentResult(ctx, messageID, agent, result, envelope)
}

func (s *Service) storeAgentResult(ctx context.Context, messageID string, agent map[string]any, result, envelope map[string]any) {
	agentID := str(agent, "agent_id")
	status := str(result, "status")
	content := str(result, "content")
	var candidates []any
	if mc, ok := result["memory_candidates"].([]any); ok && len(mc) > 0 {
		candidates = mc
	} else if status == "completed" && content != "" {
		if cand, err := domain.BuildMemoryCandidate(envelope, agentID, content); err == nil {
			candidates = []any{cand}
		}
	}
	_ = s.Repo.UpdateResponse(ctx, messageID, agentID, repo.ResponseUpdate{
		Status:            status,
		Content:           content,
		Error:             str(result, "error"),
		ErrorCode:         str(result, "error_code"),
		ExecutionProvider: str(result, "provider"),
		Transport:         str(result, "transport"),
		Fallback:          result["fallback"],
		MemoryCandidates:  candidates,
		ResponseState:     str(result, "response_state"),
	})
	agentStatus := status
	switch status {
	case "completed", "failed", "cancelled", "awaiting-user", "waiting_verification":
	default:
		agentStatus = "idle"
	}
	_ = s.Repo.UpdateAgentStatus(ctx, agentID, agentStatus, str(result, "error"))
}

// sendTask mirrors AiCollaborationProviderSession.send_task.
func (s *Service) sendTask(agent map[string]any, task map[string]any) map[string]any {
	provider := str(agent, "provider")
	if !browserPrimaryProviders[provider] {
		return failedResult(provider, "UNSUPPORTED_BROWSER_PROVIDER", "")
	}
	result := s.sendPrompt(agent, str(task, "content"))
	if str(result, "status") == "completed" && str(result, "content") != "" {
		if policy, ok := task["memory_policy"].(map[string]any); ok && str(policy, "writeback") == "candidate-only" {
			source := str(agent, "agent_id")
			if source == "" {
				source = provider
			}
			if cand, err := domain.BuildMemoryCandidate(task, source, str(result, "content")); err == nil {
				result["memory_candidates"] = []any{cand}
			}
		}
	}
	return result
}

// sendPrompt is the legacy composite: prepare → submit → wait.
func (s *Service) sendPrompt(agent map[string]any, prompt string) map[string]any {
	provider := str(agent, "provider")
	sessionID, adapter, early := s.Browser.PrepareSend(agent)
	if early != nil {
		return early
	}
	if res := s.Browser.SubmitPrompt(sessionID, provider, prompt, adapter); res != nil {
		return res
	}
	state, content, marker := s.Browser.WaitForResponse(sessionID, adapter)
	rt := s.runtimePool().RuntimeFor(provider, str(agent, "home_url"))
	_ = rt
	return s.captureOutcome(provider, sessionID, state, content, marker)
}

func (s *Service) captureOutcome(provider, sessionID, state, content, marker string) map[string]any {
	if marker != "" {
		return verificationResult(provider, marker, true)
	}
	switch state {
	case "response_cancelled":
		r := waitingResult(provider, "REQUEST_CANCELLED", true, "")
		r["status"] = "cancelled"
		return r
	case "response_completed":
		if content != "" {
			return completedResult(provider, content, state)
		}
	}
	r := waitingResult(provider, "BROWSER_RESPONSE_CAPTURE_REQUIRED", true, "")
	r["response_state"] = state
	return r
}

// runWithBrowserWait mirrors _wait_for_browser_or_fallback: the owner
// step runs wait_for_browser=True against the browser completion channel.
func (s *Service) runWithBrowserWait(ctx context.Context, messageID string, agent map[string]any, content string) map[string]any {
	provider := str(agent, "provider")
	agentID := str(agent, "agent_id")
	key := messageID + "|" + agentID
	for cycle := 0; cycle < browserWaitCycles; cycle++ {
		result := s.sendPrompt(agent, content)
		status := str(result, "status")
		if status == "completed" || status == "cancelled" {
			return result
		}
		if status != "awaiting-user" && status != "waiting_verification" {
			return result
		}
		// Wait for a browser-imported response for up to
		// BROWSER_WAIT_SECONDS.
		ch := make(chan string, 1)
		s.bwMu.Lock()
		s.browserWait[key] = ch
		s.bwMu.Unlock()
		select {
		case imported := <-ch:
			s.bwMu.Lock()
			delete(s.browserWait, key)
			s.bwMu.Unlock()
			if strings.TrimSpace(imported) != "" {
				return completedResult(provider, imported, "response_completed")
			}
			return result
		case <-time.After(browserWaitSeconds * time.Second):
			s.bwMu.Lock()
			delete(s.browserWait, key)
			s.bwMu.Unlock()
		case <-ctx.Done():
			s.bwMu.Lock()
			delete(s.browserWait, key)
			s.bwMu.Unlock()
			r := waitingResult(provider, "REQUEST_CANCELLED", true, "")
			r["status"] = "cancelled"
			return r
		}
	}
	return s.sendTerminalFallback(agent, "BROWSER_WAIT_EXHAUSTED_AFTER_THREE_ATTEMPTS")
}

// sendTerminalFallback mirrors send_terminal_fallback.
func (s *Service) sendTerminalFallback(agent map[string]any, reason string) map[string]any {
	provider := str(agent, "provider")
	r := failedResult(provider, reason, "")
	return r
}

// ---------------- fixed path ----------------

func (s *Service) sendFixedMessage(ctx context.Context, p map[string]any, requester string) (map[string]any, error) {
	businessTask := str(p, "business_task")
	owner, ok := domain.FixedTaskOwners[businessTask]
	if !ok {
		return nil, fmt.Errorf("不支援的業務類型")
	}
	pipeline := str(p, "research_pipeline")
	if pipeline != "" && pipeline != "google-gemini" {
		return nil, fmt.Errorf("不支援的研究管線")
	}
	scope := str(p, "business_scope")
	if scope == "" {
		scope = "general"
	}
	content := strings.TrimSpace(str(p, "content"))
	if content == "" {
		return nil, fmt.Errorf("請輸入要交給 AI 協作的內容")
	}
	requestID := str(p, "request_id")
	var agents []map[string]any
	if pipeline == "google-gemini" {
		if scope != "investment" {
			return nil, fmt.Errorf("Google-Gemini 管線僅適用於投資業務")
		}
		gs, err := s.Repo.GetAgents(ctx, []string{"gemini"})
		if err != nil {
			return nil, err
		}
		if len(gs) == 0 || str(gs[0], "provider") != "gemini" {
			return nil, fmt.Errorf("Google-Gemini 管線需要 Gemini")
		}
		agents = gs
	} else {
		gs, err := s.Repo.GetAgents(ctx, []string{owner})
		if err != nil {
			return nil, err
		}
		for _, a := range gs {
			if en, _ := a["enabled"].(bool); en {
				agents = append(agents, a)
			}
		}
		if len(agents) == 0 {
			return map[string]any{"ok": false, "message": "指定的責任 AI 目前不可用"}, nil
		}
	}
	// ChatGPT final coordinator must exist and be enabled.
	cg, err := s.Repo.GetAgents(ctx, []string{"chatgpt"})
	if err != nil {
		return nil, err
	}
	if len(cg) == 0 {
		return map[string]any{"ok": false, "message": "ChatGPT 最終協調器不可用"}, nil
	}
	chatgpt := cg[0]
	if en, _ := chatgpt["enabled"].(bool); !en {
		return map[string]any{"ok": false, "message": "ChatGPT 最終協調器不可用"}, nil
	}
	if businessTask == "training-candidate-authoring" {
		scoped, serr := s.agentForBusiness(chatgpt, "star-training")
		if serr != nil {
			return nil, serr
		}
		chatgpt = scoped
	}

	selected := []string{}
	for _, a := range agents {
		selected = append(selected, str(a, "agent_id"))
	}
	if owner != "chatgpt" {
		selected = append(selected, "chatgpt")
	}

	s.sendMu.Lock()
	message, err := s.Repo.CreateGroupMessage(ctx, content, selected, scope, requestID, s.RuntimeGen)
	s.sendMu.Unlock()
	if err != nil {
		return nil, err
	}
	messageID := str(message, "message_id")
	s.trackRequest(requestID, messageID)

	memCtx, _ := domain.SanitizeMemoryContext(p["memory_context"])
	// Owner step.
	for _, agent := range agents {
		if pipeline == "google-gemini" {
			s.runGoogleGeminiPipeline(ctx, messageID, agent, content, scope, requestID, requester, memCtx)
		} else {
			s.runAgentMessage(ctx, messageID, agent, content, businessTask, scope, requestID, "xingcheng", memCtx, true, true)
		}
	}
	// ChatGPT final coordination — skipped while any specialist is
	// awaiting a browser result.
	if owner != "chatgpt" {
		fresh, _ := s.Repo.GetMessage(ctx, messageID)
		awaiting := false
		for _, r := range toMapList(fresh["responses"]) {
			st := str(r, "status")
			if st == "awaiting-user" || st == "waiting_verification" {
				awaiting = true
			}
		}
		if awaiting {
			_ = s.Repo.UpdateResponse(ctx, messageID, "chatgpt", repo.ResponseUpdate{
				Status: "waiting", Error: "FIXED_OWNER_BROWSER_RESULT_REQUIRED",
				ErrorCode: "FIXED_OWNER_BROWSER_RESULT_REQUIRED",
				Transport: "fixed-workflow-wait",
			})
			_ = s.Repo.UpdateAgentStatus(ctx, "chatgpt", "waiting", "")
		} else {
			s.runChatGPTFinalCoordination(ctx, messageID, chatgpt, content, scope, requestID, memCtx)
		}
	}
	fresh, err := s.Repo.GetMessage(ctx, messageID)
	if err != nil {
		return nil, err
	}
	autoMemory := s.autoMemoryFromGroupMessage(ctx, fresh)
	return s.fixedResultPayload(ctx, fresh, autoMemory, pipeline, businessTask, owner, requester)
}

// runChatGPTFinalCoordination feeds the owner responses into ChatGPT.
func (s *Service) runChatGPTFinalCoordination(ctx context.Context, messageID string, chatgpt map[string]any, original, scope, requestID string, memCtx []map[string]any) {
	fresh, err := s.Repo.GetMessage(ctx, messageID)
	if err != nil {
		return
	}
	var parts []string
	for _, r := range toMapList(fresh["responses"]) {
		if str(r, "agent_id") == "chatgpt" {
			continue
		}
		if str(r, "status") != "completed" {
			continue
		}
		body := str(r, "content")
		if len(body) > 4000 {
			body = body[:4000]
		}
		parts = append(parts, fmt.Sprintf("【%s】\n%s", str(r, "agent_id"), body))
	}
	prompt := "以下是原始需求與各 AI 的回覆，請整合為最終答案。\n\n原始需求：" + original + "\n\n" + strings.Join(parts, "\n\n")
	s.runAgentMessage(ctx, messageID, chatgpt, prompt, "orchestration", scope, requestID, "xingcheng", memCtx, true, true)
}

// runGoogleGeminiPipeline mirrors _run_google_gemini_pipeline — the
// gemini owner step of the google-gemini research pipeline.
func (s *Service) runGoogleGeminiPipeline(ctx context.Context, messageID string, agent map[string]any, content, scope, requestID, requester string, memCtx []map[string]any) {
	s.runAgentMessage(ctx, messageID, agent, content, "search", scope, requestID, requester, memCtx, true, true)
}

func (s *Service) fixedResultPayload(ctx context.Context, message map[string]any, autoMemory map[string]any, pipeline, businessTask, owner, requester string) (map[string]any, error) {
	messages, err := s.Repo.ListMessages(ctx, 50, "")
	if err != nil {
		return nil, err
	}
	agents, err := s.Repo.ListAgents(ctx)
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
	fixedOwner := owner
	if pipeline == "google-gemini" {
		fixedOwner = "gemini"
	}
	var finalResponse any = map[string]any{}
	for _, r := range toMapList(message["responses"]) {
		if str(r, "agent_id") == "chatgpt" {
			finalResponse = r
		}
	}
	workflow := "awaiting-fixed-owner"
	if fr, ok := finalResponse.(map[string]any); ok && str(fr, "status") == "completed" {
		workflow = "completed"
	}
	groupMessage := message
	if pipeline == "google-gemini" {
		filtered := cloneMap(message)
		filtered["selected_agents"] = []any{"gemini", "chatgpt"}
		var responses []any
		for _, r := range toMapList(message["responses"]) {
			id := str(r, "agent_id")
			if id == "gemini" || id == "chatgpt" {
				responses = append(responses, r)
			}
		}
		filtered["responses"] = responses
		filtered["google_raw_results_exposed"] = false
		filtered["processor"] = "gemini"
		groupMessage = filtered
	}
	var candidates []any
	for _, r := range toMapList(message["responses"]) {
		for _, c := range toMapList(r["memory_candidates"]) {
			candidates = append(candidates, c)
		}
	}
	if candidates == nil {
		candidates = []any{}
	}
	var autoMem any
	if autoMemory != nil {
		autoMem = autoMemory
	}
	pipelineOut := pipeline
	if pipelineOut == "" {
		pipelineOut = "fixed-responsibility"
	}
	return map[string]any{
		"ok": true, "message": "AI 協作已按責任分配完成",
		"group_message": groupMessage, "auto_memory_item": autoMem,
		"messages": messages, "agents": agents,
		"memory_items": memoryItems, "tasks": tasks,
		"research_pipeline":                       pipelineOut,
		"business_task":                           businessTask,
		"fixed_task_owner":                        fixedOwner,
		"requested_selection_ignored_for_routing": true,
		"requested_by":                            requester,
		"response_recipient":                      requester,
		"memory_interchange": map[string]any{
			"mode":                   "star-mediated-candidate-writeback",
			"direct_database_access": false,
			"candidate_count":        len(candidates),
			"candidates":             candidates,
		},
		"external_coordinator": "chatgpt",
		"final_coordinator":    "chatgpt",
		"final_response":       finalResponse,
		"workflow_status":      workflow,
		"channel_coordinator":  "xingcheng",
		"fixed_workflow": []any{
			"star-request", "fixed-owner",
			"same-provider-shared-foreground-browser-tab",
			"chatgpt-final-coordination", "star-response",
		},
	}, nil
}

// sendFixedTasks mirrors the tasks-list path (xingcheng only, cap 6).
func (s *Service) sendFixedTasks(ctx context.Context, p map[string]any, tasks []map[string]any, requester string) (map[string]any, error) {
	if len(tasks) > maxParallelAI {
		tasks = tasks[:maxParallelAI]
	}
	results := make([]map[string]any, len(tasks))
	var wg sync.WaitGroup
	for i, item := range tasks {
		wg.Add(1)
		s.taskSlots <- struct{}{}
		go func(idx int, t map[string]any) {
			defer wg.Done()
			defer func() { <-s.taskSlots }()
			sub := cloneMap(p)
			if c := str(t, "content"); c != "" {
				sub["content"] = c
			}
			if bt := str(t, "task_type"); bt != "" {
				sub["business_task"] = bt
			} else if bt := str(t, "business_task"); bt != "" {
				sub["business_task"] = bt
			} else {
				sub["business_task"] = "general"
			}
			if bs := str(t, "business_scope"); bs != "" {
				sub["business_scope"] = bs
			}
			if rp := str(t, "research_pipeline"); rp != "" {
				sub["research_pipeline"] = rp
			}
			delete(sub, "tasks")
			res, err := s.sendFixedMessage(ctx, sub, requester)
			if err != nil {
				code := "REQUEST_REJECTED"
				if err == errPermission {
					code = "PERMISSION_DENIED"
				}
				res = map[string]any{"ok": false, "error_code": code, "message": err.Error()}
			}
			results[idx] = res
		}(i, item)
	}
	wg.Wait()
	allOK := true
	var out []any
	for _, r := range results {
		out = append(out, r)
		if ok, _ := r["ok"].(bool); !ok {
			allOK = false
		}
	}
	return map[string]any{
		"ok":                     allOK,
		"message":                fmt.Sprintf("責任制已送出 %d 個並行任務", len(tasks)),
		"execution_mode":         "parallel-fixed-responsibility",
		"maximum_parallel_tasks": maxParallelAI,
		"task_count":             len(tasks),
		"results":                out,
		"channel_coordinator":    "xingcheng",
		"response_recipient":     requester,
	}, nil
}

// ---------------- auto memory ----------------

var whitespaceRe = regexp.MustCompile(`\s+`)

func shorten(text string, limit int) string {
	collapsed := strings.TrimSpace(whitespaceRe.ReplaceAllString(text, " "))
	if len(collapsed) <= limit {
		return collapsed
	}
	return strings.TrimRight(collapsed[:limit-1], " ") + "…"
}

// autoMemoryFromGroupMessage mirrors _auto_memory_from_group_message.
func (s *Service) autoMemoryFromGroupMessage(ctx context.Context, message map[string]any) map[string]any {
	if message == nil {
		return nil
	}
	var responseLines []string
	var agentIDs []string
	for _, r := range toMapList(message["responses"]) {
		body := str(r, "content")
		if body == "" {
			body = str(r, "error")
		}
		responseLines = append(responseLines,
			fmt.Sprintf("[%s] %s\n%s", str(r, "agent_id"), str(r, "status"), shorten(body, 900)))
		agentIDs = append(agentIDs, str(r, "agent_id"))
	}
	sort.Strings(agentIDs)
	agentIDs = dedupeStrings(agentIDs)
	responses := strings.Join(responseLines, "\n")
	if responses == "" {
		responses = "無回覆內容"
	}
	content := strings.Join([]string{
		"AI 協作記憶：群組訊息",
		"需求：" + str(message, "content"),
		"回覆：",
		responses,
	}, "\n\n")
	hash := sha256.Sum256([]byte(content))
	item, err := s.Repo.AddMemoryItem(ctx, "auto",
		"AI 協作："+shorten(str(message, "content"), 28),
		content, str(message, "business_scope"),
		strings.Join(agentIDs, ","), str(message, "message_id"),
		hex.EncodeToString(hash[:]))
	if err != nil {
		return nil
	}
	return item
}

func dedupeStrings(in []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, s := range in {
		if !seen[s] {
			seen[s] = true
			out = append(out, s)
		}
	}
	return out
}

func (s *Service) browserCloseBackground() error {
	// close_background_context is a no-op in the browser-view model.
	return nil
}
