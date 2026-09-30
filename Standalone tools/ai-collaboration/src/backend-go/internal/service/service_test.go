package service

import (
	"context"
	"strings"
	"testing"
)

// fakeOps is a BrowserOps stub that completes every provider response
// immediately with canned content.
type fakeOps struct {
	failCreate bool
}

func (f *fakeOps) CreateSession(owner, url, sessionID string) (map[string]any, error) {
	if f.failCreate {
		return map[string]any{"ok": false, "message": "EMBEDDED_BROWSER_SESSION_FAILED"}, nil
	}
	return map[string]any{"ok": true, "id": sessionID}, nil
}
func (f *fakeOps) Navigate(sessionID, url string) (map[string]any, error) {
	return map[string]any{"ok": true}, nil
}
func (f *fakeOps) ExecuteScript(sessionID, script string) (map[string]any, error) {
	switch {
	case strings.Contains(script, "ready"):
		return map[string]any{"ok": true, "result": map[string]any{"ready": true}}, nil
	case strings.Contains(script, "flagged"):
		return map[string]any{"ok": true, "result": map[string]any{"flagged": false}}, nil
	case strings.Contains(script, "found"):
		return map[string]any{"ok": true, "result": map[string]any{"found": true}}, nil
	case strings.Contains(script, "sent"):
		return map[string]any{"ok": true, "result": map[string]any{"sent": true}}, nil
	default:
		// probe: stable completed content
		return map[string]any{"ok": true, "result": map[string]any{
			"content":    "這是來自測試的固定回覆內容，長度超過十字元。",
			"generating": false, "verification": false, "marker": "",
		}}, nil
	}
}
func (f *fakeOps) GetURL(sessionID string) (map[string]any, error) {
	return map[string]any{"ok": true, "url": "https://chatgpt.com/"}, nil
}
func (f *fakeOps) Close(sessionID string) (map[string]any, error) {
	return map[string]any{"ok": true}, nil
}

func newTestService(t *testing.T) (*Service, *memStore) {
	t.Helper()
	store := newMemStore()
	browser := NewBrowserSession(&fakeOps{})
	browser.PollInterval = 0 // tests: no real-time polling delay
	svc := New("9.9.9-test", t.TempDir(), store, browser)
	return svc, store
}

func actorP(actor string) map[string]any {
	return map[string]any{"_authorized_requester_actor": actor}
}

func TestUnknownCommand(t *testing.T) {
	svc, _ := newTestService(t)
	event, res := svc.Handle(context.Background(), "bogus_command", map[string]any{})
	if event != "bogus_command_result" {
		t.Fatalf("event=%s", event)
	}
	if res["ok"] != false || res["error_code"] != "UNSUPPORTED_COMMAND" {
		t.Fatalf("res=%v", res)
	}
}

func TestGetState(t *testing.T) {
	svc, _ := newTestService(t)
	_, res := svc.Handle(context.Background(), "ai_nexus_get_state", map[string]any{})
	if res["ok"] != true {
		t.Fatalf("res=%v", res)
	}
	if res["version"] != "9.9.9-test" {
		t.Fatalf("version=%v", res["version"])
	}
	pe := res["provider_execution"].(map[string]any)
	if pe["maximum_parallel_ai"] != float64(6) && pe["maximum_parallel_ai"] != 6 {
		t.Fatalf("max_parallel=%v", pe["maximum_parallel_ai"])
	}
	agents := res["agents"].([]map[string]any)
	if len(agents) != 6 {
		t.Fatalf("agents=%d", len(agents))
	}
	if res["database_path"] != "postgresql:gptbridge_collab" {
		t.Fatalf("dbpath=%v", res["database_path"])
	}
}

func TestRequesterGate(t *testing.T) {
	svc, _ := newTestService(t)
	// ai-assistant is always denied.
	_, res := svc.Handle(context.Background(), "ai_nexus_collab_start",
		map[string]any{"_authorized_requester_actor": "governance/tool/ai-assistant",
			"content": "x", "provider_ids": []any{"chatgpt"}})
	if res["ok"] != false || res["error_code"] != "PERMISSION_DENIED" {
		t.Fatalf("res=%v", res)
	}
	// Unknown actor denied.
	_, res = svc.Handle(context.Background(), "ai_nexus_collab_start",
		map[string]any{"_authorized_requester_actor": "governance/tool/other",
			"content": "x", "provider_ids": []any{"chatgpt"}})
	if res["error_code"] != "PERMISSION_DENIED" {
		t.Fatalf("res=%v", res)
	}
	// manual_result: xingcheng denied, empty actor allowed.
	_, res = svc.Handle(context.Background(), "ai_nexus_collab_manual_result",
		map[string]any{"_authorized_requester_actor": "governance/tool/xingcheng",
			"task_id": "t", "provider_id": "chatgpt", "content": "x"})
	if res["error_code"] != "PERMISSION_DENIED" {
		t.Fatalf("xingcheng manual result must be denied: %v", res)
	}
}

func TestSelectionAndAddAgent(t *testing.T) {
	svc, store := newTestService(t)
	_, res := svc.Handle(context.Background(), "ai_nexus_set_agent_selection",
		map[string]any{"agent_ids": []any{"chatgpt", "gemini", "bogus"}})
	if res["ok"] != true || res["selected_count"] != float64(2) && res["selected_count"] != 2 {
		t.Fatalf("res=%v", res)
	}
	agents, _ := store.ListAgents(context.Background())
	sel := 0
	for _, a := range agents {
		if a["selected"].(bool) {
			sel++
		}
	}
	if sel != 2 {
		t.Fatalf("store selected=%d", sel)
	}
	// add agent — valid https URL.
	_, res = svc.Handle(context.Background(), "ai_nexus_add_agent",
		map[string]any{"name": "Local AI", "home_url": "https://ai.example.com/"})
	if res["ok"] != true {
		t.Fatalf("add res=%v", res)
	}
	// duplicate name rejected.
	_, res = svc.Handle(context.Background(), "ai_nexus_add_agent",
		map[string]any{"name": "Local AI", "home_url": "https://ai.example.com/"})
	if res["ok"] != false {
		t.Fatalf("dup res=%v", res)
	}
	// private URL rejected.
	_, res = svc.Handle(context.Background(), "ai_nexus_add_agent",
		map[string]any{"name": "X", "home_url": "https://192.168.0.1/"})
	if res["ok"] == true {
		t.Fatalf("private URL must be rejected")
	}
	_, res = svc.Handle(context.Background(), "ai_nexus_add_agent",
		map[string]any{"name": "Y", "home_url": "http://example.com/"})
	if res["ok"] == true {
		t.Fatalf("http URL must be rejected")
	}
}

func TestCollabStartCompare(t *testing.T) {
	svc, store := newTestService(t)
	_, res := svc.Handle(context.Background(), "ai_nexus_collab_start",
		map[string]any{
			"mode":                        "compare",
			"content":                     "什麼是 2+2？",
			"provider_ids":                []any{"chatgpt", "gemini"},
			"request_id":                  "req-1",
			"_authorized_requester_actor": "governance/tool/ai-collaboration",
		})
	if res["ok"] != true {
		t.Fatalf("res=%v", res)
	}
	if res["overall_status"] != "completed" {
		t.Fatalf("status=%v message=%v", res["overall_status"], res["message"])
	}
	if res["request_id"] != "req-1" {
		t.Fatalf("request_id echo missing: %v", res["request_id"])
	}
	results := toMapList(res["provider_results"])
	if len(results) != 2 {
		t.Fatalf("results=%v", results)
	}
	task, _ := store.GetCollabTask(context.Background(), res["task_id"].(string))
	if task == nil || task["overall_status"] != "completed" {
		t.Fatalf("task=%v", task)
	}
	if task["comparison"].(map[string]any)["task_id"] == nil {
		t.Fatalf("comparison missing: %v", task["comparison"])
	}
	if task["summary_reference"] == "" {
		t.Fatalf("summary_reference empty")
	}
}

func TestCollabProviderValidation(t *testing.T) {
	svc, _ := newTestService(t)
	_, res := svc.Handle(context.Background(), "ai_nexus_collab_start",
		map[string]any{"mode": "compare", "content": "x",
			"provider_ids": []any{"chatgpt", "unknown"}})
	if res["error_code"] != "UNSUPPORTED_BROWSER_PROVIDER" {
		t.Fatalf("res=%v", res)
	}
	// compare needs ≥2 providers
	_, res = svc.Handle(context.Background(), "ai_nexus_collab_start",
		map[string]any{"mode": "compare", "content": "x",
			"provider_ids": []any{"chatgpt"}})
	if res["ok"] == true {
		t.Fatalf("single provider must fail for compare")
	}
}

func TestCollabCancel(t *testing.T) {
	svc, store := newTestService(t)
	// create a task directly so cancel has a target
	task, _ := store.CreateCollabTask(context.Background(), "req-x", "compare",
		[]string{"chatgpt", "gemini"}, "test", "gen", "att")
	tid := task["task_id"].(string)
	_, res := svc.Handle(context.Background(), "ai_nexus_collab_cancel",
		map[string]any{"task_id": tid})
	if res["ok"] != true {
		t.Fatalf("res=%v", res)
	}
	updated, _ := store.GetCollabTask(context.Background(), tid)
	if updated["overall_status"] != "cancelled" || updated["fault_reference"] != "REQUEST_CANCELLED" {
		t.Fatalf("task=%v", updated)
	}
	// unknown task → SESSION_NOT_FOUND
	_, res = svc.Handle(context.Background(), "ai_nexus_collab_cancel",
		map[string]any{"task_id": "missing"})
	if res["error_code"] != "SESSION_NOT_FOUND" {
		t.Fatalf("res=%v", res)
	}
}

func TestCollabManualResult(t *testing.T) {
	svc, store := newTestService(t)
	task, _ := store.CreateCollabTask(context.Background(), "req-m", "compare",
		[]string{"chatgpt", "gemini"}, "test", "gen", "att")
	tid := task["task_id"].(string)
	_, res := svc.Handle(context.Background(), "ai_nexus_collab_manual_result",
		map[string]any{"task_id": tid, "provider_id": "chatgpt", "content": "手動答案"})
	if res["ok"] != true {
		t.Fatalf("res=%v", res)
	}
	results, _ := store.ListCollabResults(context.Background(), tid)
	if len(results) != 1 || results[0]["capture_method"] != "MANUAL" || results[0]["response_status"] != "completed" {
		t.Fatalf("results=%v", results)
	}
	// missing fields rejected
	_, res = svc.Handle(context.Background(), "ai_nexus_collab_manual_result",
		map[string]any{"task_id": tid})
	if res["ok"] == true {
		t.Fatalf("missing fields must fail")
	}
}

func TestSendMessageGeneral(t *testing.T) {
	svc, _ := newTestService(t)
	_, res := svc.Handle(context.Background(), "ai_nexus_send_message",
		map[string]any{
			"content": "測試訊息", "agent_ids": []any{"chatgpt"},
			"_authorized_requester_actor": "governance/tool/ai-collaboration",
		})
	if res["ok"] != true {
		t.Fatalf("res=%v", res)
	}
	msg := res["group_message"].(map[string]any)
	responses := toMapList(msg["responses"])
	if len(responses) != 1 {
		t.Fatalf("responses=%v", responses)
	}
	if res["channel_coordinator"] != "ai-collaboration" {
		t.Fatalf("coordinator=%v", res["channel_coordinator"])
	}
	// dedupe: same idempotency key returns the recorded result.
	key := map[string]any{
		"content": "另一測試", "agent_ids": []any{"chatgpt"},
		"idempotency_key":             "dup-1",
		"_authorized_requester_actor": "governance/tool/ai-collaboration",
	}
	_, r1 := svc.Handle(context.Background(), "ai_nexus_send_message", key)
	_, r2 := svc.Handle(context.Background(), "ai_nexus_send_message", map[string]any{
		"content": "另一測試", "agent_ids": []any{"chatgpt"},
		"idempotency_key":             "dup-2",
		"_authorized_requester_actor": "governance/tool/ai-collaboration",
	})
	if r1["ok"] != true || r2["ok"] != true {
		t.Fatalf("r1=%v r2=%v", r1, r2)
	}
}

func TestSendMessageFixedPath(t *testing.T) {
	svc, _ := newTestService(t)
	_, res := svc.Handle(context.Background(), "ai_nexus_send_message",
		map[string]any{
			"content": "分析趨勢", "business_task": "trends",
			"_authorized_requester_actor": "governance/tool/xingcheng",
		})
	if res["ok"] != true {
		t.Fatalf("res=%v", res)
	}
	if res["fixed_task_owner"] != "grok" {
		t.Fatalf("owner=%v", res["fixed_task_owner"])
	}
	if res["final_coordinator"] != "chatgpt" {
		t.Fatalf("coordinator=%v", res["final_coordinator"])
	}
	if res["channel_coordinator"] != "xingcheng" {
		t.Fatalf("channel=%v", res["channel_coordinator"])
	}
	// ai-collaboration requester + tasks → denied.
	_, res = svc.Handle(context.Background(), "ai_nexus_send_message",
		map[string]any{
			"content": "x", "tasks": []any{map[string]any{"task_type": "general"}},
			"_authorized_requester_actor": "governance/tool/ai-collaboration",
		})
	if res["error_code"] != "PERMISSION_DENIED" {
		t.Fatalf("res=%v", res)
	}
}

func TestMemoryAndTaskAndReport(t *testing.T) {
	svc, _ := newTestService(t)
	_, res := svc.Handle(context.Background(), "ai_nexus_add_memory",
		map[string]any{"content": "記住這件事"})
	if res["ok"] != true {
		t.Fatalf("res=%v", res)
	}
	if len(res["memory_items"].([]map[string]any)) != 1 {
		t.Fatalf("memory=%v", res["memory_items"])
	}
	_, res = svc.Handle(context.Background(), "ai_nexus_create_task",
		map[string]any{"title": "整理結論", "participant_agents": []any{"chatgpt"}})
	if res["ok"] != true {
		t.Fatalf("res=%v", res)
	}
	_, res = svc.Handle(context.Background(), "ai_nexus_export_report", map[string]any{})
	if res["ok"] != true || !strings.Contains(res["report_path"].(string), "ai-collaboration-diagnostic-") {
		t.Fatalf("res=%v", res)
	}
}

func TestErrorNormalization(t *testing.T) {
	svc, _ := newTestService(t)
	// open_agent with unknown id → REQUEST_REJECTED normalization.
	_, res := svc.Handle(context.Background(), "ai_nexus_open_agent",
		map[string]any{"agent_id": "nope"})
	if res["ok"] != false || res["error_code"] != "REQUEST_REJECTED" {
		t.Fatalf("res=%v", res)
	}
}
