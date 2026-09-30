// collab.go — collab orchestrator parity with collab_orchestrator /
// collab_svc_coordination: collab_start / cancel / manual_result /
// resume / complete_browser_response, provider steps, terminal
// aggregation (comparison + synthesis) and the task result shape.
package service

import (
	"context"
	"fmt"
	"strings"
	"sync"

	"gptbridge.local/ai-collaboration-backend/internal/domain"
	"gptbridge.local/ai-collaboration-backend/internal/repo"
)

var collabModes = map[string]bool{
	"single": true, "compare": true, "sequential_review": true,
}

func (s *Service) collabStart(ctx context.Context, p map[string]any) (map[string]any, error) {
	if _, err := s.requesterToolID(p); err != nil {
		return nil, err
	}
	return s.withDedupe(p, func() (map[string]any, error) {
		content := strings.TrimSpace(str(p, "content"))
		if content == "" {
			return map[string]any{"ok": false, "message": "請輸入要交給 AI 協作的內容"}, nil
		}
		mode := str(p, "mode")
		if mode == "" {
			mode = "single"
		}
		if !collabModes[mode] {
			return map[string]any{
				"ok": false, "error_code": "REQUEST_REJECTED",
				"message": fmt.Sprintf("不支援的協作模式：%s", mode),
			}, nil
		}
		ids := strList(p["provider_ids"])
		if len(ids) == 0 {
			ids = strList(p["agent_ids"])
		}
		// casefold + dedupe preserving order
		seen := map[string]bool{}
		var providers []string
		for _, id := range ids {
			key := strings.ToLower(id)
			if !seen[key] {
				seen[key] = true
				providers = append(providers, key)
			}
		}
		label := "一個"
		if mode != "single" {
			label = "兩個"
		}
		min := 1
		if mode != "single" {
			min = 2
		}
		if len(providers) < min {
			return map[string]any{
				"ok":      false,
				"message": fmt.Sprintf("此協作模式至少需要 %s AI", label),
			}, nil
		}
		if mode == "single" {
			providers = providers[:1]
		}
		if len(providers) > maxParallelAI {
			providers = providers[:maxParallelAI]
		}
		// Every provider id must match an enabled agent's provider.
		agents, err := s.Repo.ListAgents(ctx)
		if err != nil {
			return nil, err
		}
		enabled := map[string]bool{}
		for _, a := range agents {
			if en, _ := a["enabled"].(bool); en {
				enabled[str(a, "provider")] = true
			}
		}
		var bad []string
		var agentByProvider = map[string]map[string]any{}
		for _, a := range agents {
			if en, _ := a["enabled"].(bool); en {
				agentByProvider[str(a, "provider")] = a
			}
		}
		for _, pid := range providers {
			if !enabled[pid] {
				bad = append(bad, pid)
			}
		}
		if len(bad) > 0 {
			return map[string]any{
				"ok": false, "error_code": "UNSUPPORTED_BROWSER_PROVIDER",
				"message": fmt.Sprintf("此業務範圍不支援所選 Provider（%s）", strings.Join(bad, ", ")),
			}, nil
		}
		requestID := str(p, "request_id")
		if requestID == "" {
			requestID = repo.NewID(12)
		}
		attemptID := repo.NewID(8)
		task, err := s.Repo.CreateCollabTask(ctx, requestID, mode, providers, content, s.RuntimeGen, attemptID)
		if err != nil {
			return nil, err
		}
		taskID := str(task, "task_id")
		s.trackRequest(requestID, taskID)
		s.ctMu.Lock()
		s.collabTasks[taskID] = &collabTrack{RequestID: requestID}
		s.ctMu.Unlock()

		var stepAgents []map[string]any
		for _, pid := range providers {
			stepAgents = append(stepAgents, agentByProvider[pid])
		}
		s.runCollabTask(ctx, taskID, mode, stepAgents, content, requestID, attemptID)
		return s.collabTaskResult(ctx, taskID)
	})
}

// runCollabTask mirrors _run_collab_task — mode dispatch then
// terminal aggregation.
func (s *Service) runCollabTask(ctx context.Context, taskID, mode string, agents []map[string]any, content, requestID, attemptID string) {
	if err := s.Repo.UpdateCollabTask(ctx, taskID, repo.CollabTaskUpdate{
		Status: "running", Started: true,
	}); err != nil {
		return
	}
	switch mode {
	case "single":
		if len(agents) > 0 {
			s.runProviderStep(ctx, taskID, agents[0], content, requestID, attemptID)
		}
	case "compare":
		var wg sync.WaitGroup
		for _, agent := range agents {
			wg.Add(1)
			go func(a map[string]any) {
				defer wg.Done()
				s.runProviderStep(ctx, taskID, a, content, requestID, attemptID)
			}(agent)
		}
		wg.Wait()
	case "sequential_review":
		var prior []map[string]any
		for i, agent := range agents {
			prompt := content
			if i > 0 {
				prompt = reviewPrompt(content, prior)
			}
			res := s.runProviderStep(ctx, taskID, agent, prompt, requestID, attemptID)
			s.ctMu.Lock()
			cancelled := s.collabTasks[taskID] != nil && s.collabTasks[taskID].Cancelled
			s.ctMu.Unlock()
			if cancelled {
				break
			}
			if str(res, "response_status") == "completed" {
				prior = append(prior, res)
			}
		}
	}
	s.aggregateIfTerminal(ctx, taskID)
}

func reviewPrompt(original string, prior []map[string]any) string {
	lines := []string{
		"你是一位審查者。以下是原始需求與先前各 AI 的回覆，請綜合審查、指正錯誤並給出最終答案。",
		"原始需求：" + original,
		"",
	}
	for _, r := range prior {
		text := str(r, "response_text")
		if len(text) > 4000 {
			text = text[:4000]
		}
		lines = append(lines, fmt.Sprintf("【%s 的回覆】\n%s\n", str(r, "provider_id"), text))
	}
	return strings.Join(lines, "\n")
}

// runProviderStep mirrors _run_provider_step incl. sealed upsert.
func (s *Service) runProviderStep(ctx context.Context, taskID string, agent map[string]any, prompt, requestID, attemptID string) map[string]any {
	provider := str(agent, "provider")
	s.ctMu.Lock()
	cancelled := s.collabTasks[taskID] != nil && s.collabTasks[taskID].Cancelled
	s.ctMu.Unlock()
	if cancelled {
		rec := domain.SealProviderResponse(provider, requestID, repo.NewID(12), "", "NONE", "cancelled", domain.AdapterVersion, repo.UtcNow(), "cancelled")
		_ = s.Repo.UpsertCollabResult(ctx, collabResultRecord(taskID, rec, attemptID))
		return rec
	}
	rt := s.runtimePool().RuntimeFor(provider, str(agent, "home_url"))
	var result map[string]any
	if rt == nil {
		result = failedResult(provider, "UNSUPPORTED_BROWSER_PROVIDER", "")
	} else {
		_ = s.Repo.UpdateAgentStatus(ctx, str(agent, "agent_id"), "running", "")
		func() {
			defer func() {
				if r := recover(); r != nil {
					result = failedResult(provider, "EMBEDDED_BROWSER_SESSION_FAILED", fmt.Sprint(r))
				}
			}()
			result = rt.Submit(agent, prompt, taskID+":"+provider)
		}()
	}
	status := str(result, "status")
	captureMethod := "NONE"
	sealed := ""
	if status == "completed" && str(result, "content") != "" {
		captureMethod = "AUTO"
		sealed = domain.SealExternalText(str(result, "content"))
	}
	recordStatus := status
	switch status {
	case "awaiting-user", "waiting_verification":
		recordStatus = "awaiting-user"
	case "completed", "cancelled", "failed":
		// unchanged
	default:
		recordStatus = "failed"
	}
	evidence := str(result, "response_state")
	if evidence == "" {
		if h, ok := result["browser_handoff"].(map[string]any); ok {
			evidence = str(h, "response_state")
		}
	}
	if evidence == "" {
		evidence = recordStatus
	}
	rec := domain.SealProviderResponse(provider, requestID, repo.NewID(12),
		sealed, captureMethod, evidence, str(result, "adapter_version"), repo.UtcNow(), recordStatus)
	if sealed == "" {
		rec["response_text"] = ""
	}
	out := collabResultRecord(taskID, rec, attemptID)
	out["error_code"] = str(result, "error_code")
	_ = s.Repo.UpsertCollabResult(ctx, out)
	s.updateAgentAfterStep(ctx, str(agent, "agent_id"), recordStatus)
	return out
}

func collabResultRecord(taskID string, rec map[string]any, attemptID string) map[string]any {
	out := cloneMap(rec)
	out["task_id"] = taskID
	out["attempt_id"] = attemptID
	return out
}

func (s *Service) updateAgentAfterStep(ctx context.Context, agentID, status string) {
	switch status {
	case "completed":
		_ = s.Repo.UpdateAgentStatus(ctx, agentID, "completed", "")
	case "awaiting-user":
		_ = s.Repo.UpdateAgentStatus(ctx, agentID, "awaiting-user", "")
	case "cancelled":
		_ = s.Repo.UpdateAgentStatus(ctx, agentID, "idle", "")
	default:
		_ = s.Repo.UpdateAgentStatus(ctx, agentID, "failed", "")
	}
}

// aggregateIfTerminal mirrors _aggregate_if_terminal.
func (s *Service) aggregateIfTerminal(ctx context.Context, taskID string) {
	task, err := s.Repo.GetCollabTask(ctx, taskID)
	if err != nil || task == nil {
		return
	}
	if str(task, "overall_status") == "cancelled" {
		return
	}
	results := toMapList(task["provider_results"])
	statusSet := map[string]int{}
	for _, r := range results {
		statusSet[str(r, "response_status")]++
	}
	if statusSet["awaiting-user"] > 0 {
		_ = s.Repo.UpdateCollabTask(ctx, taskID, repo.CollabTaskUpdate{Status: "waiting_user"})
		return
	}
	_ = s.Repo.UpdateCollabTask(ctx, taskID, repo.CollabTaskUpdate{Status: "aggregating"})
	completed := statusSet["completed"]
	selected := len(toStrings(task["selected_providers"]))
	final := "completed"
	if completed == 0 {
		final = "failed"
	} else if completed < selected {
		final = "partial"
	}
	comparison := domain.Compare(taskID, results)
	synthesis := domain.Synthesize(taskID, str(task, "original_request"), results, comparison)
	summaryRef := "collab_task:" + taskID + ":synthesis"
	_ = s.Repo.UpdateCollabTask(ctx, taskID, repo.CollabTaskUpdate{
		Status: final, Comparison: comparison, Synthesis: synthesis,
		SummaryReference: &summaryRef, Completed: true,
	})
}

// collabTaskResult mirrors _collab_task_result.
func (s *Service) collabTaskResult(ctx context.Context, taskID string) (map[string]any, error) {
	task, err := s.Repo.GetCollabTask(ctx, taskID)
	if err != nil {
		return nil, err
	}
	if task == nil {
		return map[string]any{
			"ok": false, "error_code": "SESSION_NOT_FOUND",
			"message": "找不到指定的協作任務",
		}, nil
	}
	status := str(task, "overall_status")
	okStatus := status == "completed" || status == "partial" || status == "waiting_user"
	messages, err := s.Repo.ListMessages(ctx, 50, "")
	if err != nil {
		return nil, err
	}
	agents, err := s.Repo.ListAgents(ctx)
	if err != nil {
		return nil, err
	}
	collabTasks, err := s.Repo.ListCollabTasks(ctx, 50)
	if err != nil {
		return nil, err
	}
	messagesByStatus := map[string]string{
		"completed":    "協作任務已完成。",
		"partial":      "部分 AI 回覆失敗，已提供可用結果。",
		"waiting_user": "部分 AI 需要使用者確認瀏覽器回覆後才能繼續。",
		"failed":       "所有 Provider 皆失敗。",
		"cancelled":    "協作任務已取消。",
	}
	message, ok := messagesByStatus[status]
	if !ok {
		message = "協作任務已建立。"
	}
	return map[string]any{
		"ok": okStatus, "task_id": taskID, "task": task,
		"overall_status":   status,
		"mode":             str(task, "mode"),
		"provider_results": task["provider_results"],
		"comparison":       task["comparison"],
		"synthesis":        task["synthesis"],
		"collab_tasks":     collabTasks,
		"messages":         messages,
		"agents":           agents,
		"message":          message,
	}, nil
}

func (s *Service) collabCancel(ctx context.Context, p map[string]any) (map[string]any, error) {
	taskID := str(p, "task_id")
	var task map[string]any
	var err error
	if taskID != "" {
		task, err = s.Repo.GetCollabTask(ctx, taskID)
	} else if rid := str(p, "request_id"); rid != "" {
		tasks, terr := s.Repo.ListCollabTasks(ctx, 50)
		err = terr
		for _, t := range tasks {
			if str(t, "request_id") == rid {
				task = t
				taskID = str(t, "task_id")
				break
			}
		}
	}
	if err != nil {
		return nil, err
	}
	if task == nil {
		return map[string]any{
			"ok": false, "error_code": "SESSION_NOT_FOUND",
			"message": "找不到指定的協作任務",
		}, nil
	}
	s.ctMu.Lock()
	if t, ok := s.collabTasks[taskID]; ok {
		t.Cancelled = true
	}
	s.ctMu.Unlock()
	var cancelledProviders []any
	for _, pid := range toStrings(task["selected_providers"]) {
		if rt := s.runtimePool().RuntimeFor(pid, ""); rt != nil {
			res := rt.Cancel(str(task, "request_id"))
			if ok, _ := res["ok"].(bool); ok {
				cancelledProviders = append(cancelledProviders, pid)
			}
		}
	}
	if cancelledProviders == nil {
		cancelledProviders = []any{}
	}
	if err := s.Repo.CancelCollabResults(ctx, taskID); err != nil {
		return nil, err
	}
	fault := "REQUEST_CANCELLED"
	if err := s.Repo.UpdateCollabTask(ctx, taskID, repo.CollabTaskUpdate{
		Status: "cancelled", FaultReference: &fault, Completed: true,
	}); err != nil {
		return nil, err
	}
	updated, err := s.Repo.GetCollabTask(ctx, taskID)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"ok": true, "task_id": taskID,
		"cancelled_providers": cancelledProviders,
		"cancel_remote_state": "unconfirmed",
		"message":             "協作任務已取消（遠端瀏覽器狀態已標記取消，實際狀態待確認）。",
		"task":                updated,
	}, nil
}

// manualResultGate: actor, if present, must be ai-collaboration or
// main-system — xingcheng is denied; empty actor allowed.
func (s *Service) manualResultGate(p map[string]any) error {
	actor := str(p, "_authorized_requester_actor")
	if actor == "" || actor == "governance/tool/ai-collaboration" || actor == "governance/main-system" {
		return nil
	}
	return errPermission
}

func (s *Service) collabManualResult(ctx context.Context, p map[string]any) (map[string]any, error) {
	if err := s.manualResultGate(p); err != nil {
		return nil, err
	}
	taskID := str(p, "task_id")
	providerID := strings.ToLower(str(p, "provider_id"))
	content := str(p, "content")
	if taskID == "" || providerID == "" || strings.TrimSpace(content) == "" {
		return map[string]any{"ok": false, "message": "task_id / provider_id / content 為必填"}, nil
	}
	task, err := s.Repo.GetCollabTask(ctx, taskID)
	if err != nil {
		return nil, err
	}
	if task == nil {
		return map[string]any{
			"ok": false, "error_code": "SESSION_NOT_FOUND",
			"message": "找不到指定的協作任務",
		}, nil
	}
	rec := domain.SealProviderResponse(providerID, str(task, "request_id"),
		repo.NewID(12), content, "MANUAL", "user-confirmed-import",
		"", repo.UtcNow(), "completed")
	out := collabResultRecord(taskID, rec, str(task, "attempt_id"))
	if err := s.Repo.UpsertCollabResult(ctx, out); err != nil {
		return nil, err
	}
	s.aggregateIfTerminal(ctx, taskID)
	updated, err := s.Repo.GetCollabTask(ctx, taskID)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"ok": true, "message": "已手動匯入回覆（MANUAL）", "task": updated,
	}, nil
}

func (s *Service) collabResume(ctx context.Context, p map[string]any) (map[string]any, error) {
	taskID := str(p, "task_id")
	if taskID == "" {
		return nil, fmt.Errorf("找不到指定的協作任務")
	}
	task, err := s.Repo.GetCollabTask(ctx, taskID)
	if err != nil {
		return nil, err
	}
	if task == nil {
		return map[string]any{
			"ok": false, "error_code": "SESSION_NOT_FOUND",
			"message": "找不到指定的協作任務",
		}, nil
	}
	if str(task, "overall_status") == "completed" {
		return map[string]any{
			"ok": true, "task": task,
			"message": "任務已完成，無需恢復。",
		}, nil
	}
	attemptID := repo.NewID(8)
	done, err := s.Repo.CompletedResultProviders(ctx, taskID, "")
	if err != nil {
		return nil, err
	}
	agents, err := s.Repo.ListAgents(ctx)
	if err != nil {
		return nil, err
	}
	byProvider := map[string]map[string]any{}
	for _, a := range agents {
		if en, _ := a["enabled"].(bool); en {
			byProvider[str(a, "provider")] = a
		}
	}
	var pending []map[string]any
	for _, pid := range toStrings(task["selected_providers"]) {
		if !done[pid] {
			if a, ok := byProvider[pid]; ok {
				pending = append(pending, a)
			}
		}
	}
	if err := s.Repo.UpdateCollabTask(ctx, taskID, repo.CollabTaskUpdate{
		Status: "running", AttemptID: attemptID,
		FaultReference: strPtr(""), Started: true,
	}); err != nil {
		return nil, err
	}
	requestID := str(task, "request_id")
	s.runCollabTask(ctx, taskID, str(task, "mode"), pending,
		str(task, "original_request"), requestID, attemptID)
	return s.collabTaskResult(ctx, taskID)
}

func strPtr(s string) *string { return &s }

func (s *Service) completeBrowserResponse(ctx context.Context, p map[string]any) (map[string]any, error) {
	if err := s.manualResultGate(p); err != nil {
		return nil, err
	}
	messageID := str(p, "message_id")
	agentID := str(p, "agent_id")
	content := str(p, "content")
	if messageID == "" || agentID == "" || strings.TrimSpace(content) == "" {
		return map[string]any{"ok": false, "message": "瀏覽器回覆內容為必填"}, nil
	}
	key := messageID + "|" + agentID
	s.bwMu.Lock()
	if ch, ok := s.browserWait[key]; ok {
		delete(s.browserWait, key)
		s.bwMu.Unlock()
		if len(content) > 64000 {
			content = content[:64000]
		}
		select {
		case ch <- content:
		default:
		}
		return s.browserCompletedResult(ctx, messageID, agentID,
			"瀏覽器回覆已匯入，協作任務繼續處理中。")
	}
	s.bwMu.Unlock()
	return s.storeBrowserResult(ctx, messageID, agentID, content)
}

// storeBrowserResult mirrors _store_browser_result: find the
// awaiting-user/waiting_verification response for the agent, mark
// completed, then auto-memory when all responses are done.
func (s *Service) storeBrowserResult(ctx context.Context, messageID, agentID, content string) (map[string]any, error) {
	message, err := s.Repo.GetMessage(ctx, messageID)
	if err != nil {
		return nil, err
	}
	if message == nil {
		return map[string]any{"ok": false, "message": "找不到待完成的瀏覽器回覆"}, nil
	}
	var target map[string]any
	for _, r := range toMapList(message["responses"]) {
		if str(r, "agent_id") != agentID {
			continue
		}
		st := str(r, "status")
		if st == "awaiting-user" || st == "waiting_verification" {
			target = r
			break
		}
	}
	if target == nil {
		return map[string]any{"ok": false, "message": "找不到待完成的瀏覽器回覆"}, nil
	}
	if len(content) > 64000 {
		content = content[:64000]
	}
	if err := s.Repo.UpdateResponse(ctx, messageID, agentID, repo.ResponseUpdate{
		Status: "completed", Content: content,
		Transport: "embedded-browser-view",
		Fallback: map[string]any{
			"used": false, "browser_only": true,
			"cross_provider_substitution": false,
		},
		ExecutionProvider: agentID,
		ResponseState:     "response_completed",
	}); err != nil {
		return nil, err
	}
	_ = s.Repo.UpdateAgentStatus(ctx, agentID, "completed", "")
	message, err = s.Repo.GetMessage(ctx, messageID)
	if err != nil {
		return nil, err
	}
	allDone := true
	for _, r := range toMapList(message["responses"]) {
		if str(r, "status") != "completed" {
			allDone = false
			break
		}
	}
	if allDone {
		_ = s.autoMemoryFromGroupMessage(ctx, message)
	}
	msg := "瀏覽器回覆已匯入，仍有其他 AI 等待回覆。"
	if allDone {
		msg = "全部瀏覽器回覆已匯入。"
	}
	return s.browserCompletedResult(ctx, messageID, agentID, msg)
}

func (s *Service) browserCompletedResult(ctx context.Context, messageID, agentID, message string) (map[string]any, error) {
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
	return map[string]any{
		"ok": true, "message": message,
		"message_id": messageID, "agent_id": agentID,
		"messages": messages, "agents": agents, "memory_items": memoryItems,
	}, nil
}

func toMapList(v any) []map[string]any {
	var out []map[string]any
	if list, ok := v.([]any); ok {
		for _, item := range list {
			if m, ok := item.(map[string]any); ok {
				out = append(out, m)
			}
		}
	}
	return out
}
