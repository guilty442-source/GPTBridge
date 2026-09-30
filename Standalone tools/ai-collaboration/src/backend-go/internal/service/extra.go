// extra.go — ai_nexus_add_memory / create_task / export_report and the
// diagnostics block; parity with collab_repo_memory_tasks and
// collab_svc_diagnostics.
package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func (s *Service) addMemory(ctx context.Context, p map[string]any) (map[string]any, error) {
	content := str(p, "content")
	if strings.TrimSpace(content) == "" {
		return map[string]any{"ok": false, "message": "內容為必填欄位"}, nil
	}
	kind := str(p, "kind")
	if kind == "" {
		kind = "note"
	}
	title := str(p, "title")
	if title == "" {
		title = clipRunes(content, 40)
	}
	scope := str(p, "business_scope")
	if scope == "" {
		scope = "general"
	}
	hash := sha256.Sum256([]byte(content))
	item, err := s.Repo.AddMemoryItem(ctx, kind, title, content, scope,
		str(p, "source_agent_id"), str(p, "source_message_id"),
		hex.EncodeToString(hash[:]))
	if err != nil {
		return nil, err
	}
	items, err := s.Repo.ListMemoryItems(ctx)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"ok": true, "memory_item": item, "memory_items": items,
		"message": "AI 協作記憶已更新",
	}, nil
}

func clipRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

func (s *Service) createTask(ctx context.Context, p map[string]any) (map[string]any, error) {
	title := str(p, "title")
	if strings.TrimSpace(title) == "" {
		return map[string]any{"ok": false, "message": "任務標題為必填欄位"}, nil
	}
	participants := strList(p["participant_agents"])
	task, err := s.Repo.CreateTask(ctx, title, str(p, "source_message_id"), participants)
	if err != nil {
		return nil, err
	}
	tasks, err := s.Repo.ListTasks(ctx)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"ok": true, "task": task, "tasks": tasks,
		"message": "任務已建立",
	}, nil
}

func (s *Service) exportReport(ctx context.Context) (map[string]any, error) {
	state, err := s.getState(ctx)
	if err != nil {
		return nil, err
	}
	dir := filepath.Join(s.ToolRoot, "runtime", "exports")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	name := fmt.Sprintf("ai-collaboration-diagnostic-%s.json",
		time.Now().Format("20060102_150405"))
	path := filepath.Join(dir, name)
	raw, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return nil, err
	}
	raw = append(raw, '\n')
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		return nil, err
	}
	abs, _ := filepath.Abs(path)
	return map[string]any{
		"ok": true, "report_path": abs,
		"message": "AI 協作診斷報告已匯出",
	}, nil
}

// diagnostics mirrors _diagnostics.
func (s *Service) diagnostics(agents, messages, memoryItems, tasks []map[string]any) map[string]any {
	statusCounts := map[string]int{}
	selected, enabled := 0, 0
	for _, a := range agents {
		statusCounts[str(a, "status")]++
		if v, _ := a["selected"].(bool); v {
			selected++
		}
		if v, _ := a["enabled"].(bool); v {
			enabled++
		}
	}
	completed, failed, waiting := 0, 0, 0
	latestID := ""
	if len(messages) > 0 {
		latestID = str(messages[len(messages)-1], "message_id")
	}
	for _, m := range messages {
		for _, r := range toMapList(m["responses"]) {
			switch str(r, "status") {
			case "completed":
				completed++
			case "failed":
				failed++
			case "waiting_verification":
				waiting++
			}
		}
	}
	state := "empty"
	stateMsg := "尚無 AI 協作資料"
	switch {
	case failed > 0 || waiting > 0:
		state = "attention"
		stateMsg = "部分 AI 回覆需要注意（失敗或等待驗證）"
	case statusCounts["running"] > 0:
		state = "running"
		stateMsg = "AI 協作執行中"
	case selected == 0:
		state = "setup"
		stateMsg = "請先選取要協作的 AI"
	case enabled > 0:
		state = "ready"
		stateMsg = "AI 協作已就緒"
	}
	return map[string]any{
		"state": state, "message": stateMsg,
		"agents": map[string]any{
			"total": len(agents), "selected": selected,
			"enabled": enabled, "status_counts": statusCounts,
		},
		"collaboration": map[string]any{
			"message_count": len(messages), "memory_count": len(memoryItems),
			"task_count": len(tasks), "completed_responses": completed,
			"failed_responses": failed, "waiting_verification": waiting,
			"latest_message_id": latestID,
		},
		"browser": map[string]any{
			"product": "embedded-browser-view", "available": true,
			"ready_without_restart": true, "mode": "embedded-browser-view",
			"automation": true, "foreground": true,
			"shared_browser_context": true,
		},
		"generated_at": time.Now().UTC().Format("2006-01-02T15:04:05+00:00"),
	}
}
