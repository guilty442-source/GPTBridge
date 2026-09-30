package repo

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5"
)

func readRandom(b []byte) {
	_, _ = rand.Read(b)
}

var terminalStatuses = map[string]bool{
	"completed": true, "failed": true, "cancelled": true,
}

// Collab task statuses — update rejects anything else.
var TaskStatuses = map[string]bool{
	"created": true, "validating": true, "running": true,
	"waiting_user": true, "aggregating": true, "completed": true,
	"partial": true, "failed": true, "cancelled": true,
}

func newID(n int) string {
	const hexd = "0123456789abcdef"
	var b [32]byte
	buf := make([]byte, n)
	readRandom(b[:])
	for i := range buf {
		buf[i] = hexd[b[i]&0x0f]
	}
	return string(buf)
}

// NewID exposes the uuid4-hex[:n] helper used across the service.
func NewID(n int) string { return newID(n) }

// ---------------- group messages ----------------

// CreateGroupMessage inserts a user message + one pending response per
// agent; returns the full message row.
func (r *PG) CreateGroupMessage(ctx context.Context, content string, selectedAgents []string, scope, requestID, generation string) (map[string]any, error) {
	messageID := NewID(16)
	agentsJSON, _ := json.Marshal(selectedAgents)
	now := UtcNow()
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `
		INSERT INTO ai_nexus_group_messages
			(message_id, role, content, selected_agents_json, business_scope, request_id, runtime_generation, created_at)
		VALUES ($1,'user',$2,$3,$4,$5,$6,$7)`,
		messageID, content, string(agentsJSON), scope, requestID, generation, now); err != nil {
		return nil, err
	}
	for _, agentID := range selectedAgents {
		if _, err := tx.Exec(ctx, `
			INSERT INTO ai_nexus_agent_responses
				(response_id, message_id, agent_id, status, request_id, runtime_generation, created_at, updated_at)
			VALUES ($1,$2,$3,'pending',$4,$5,$6,$6)`,
			NewID(16), messageID, agentID, requestID, generation, now); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return r.GetMessage(ctx, messageID)
}

// ResponseUpdate carries the optional fields of UpdateResponse; empty
// strings keep existing values (CASE-WHEN parity).
type ResponseUpdate struct {
	Status            string
	Content           string
	Error             string
	ErrorCode         string
	ExecutionProvider string
	Transport         string
	Fallback          any
	MemoryCandidates  any
	ResponseState     string
	ResultReference   string
}

// UpdateResponse updates a response row; response_state/result_reference
// are only overwritten when non-empty; completed_at is stamped iff the
// new status is terminal.
func (r *PG) UpdateResponse(ctx context.Context, messageID, agentID string, u ResponseUpdate) error {
	fallback := "{}"
	if u.Fallback != nil {
		if raw, err := json.Marshal(u.Fallback); err == nil {
			fallback = string(raw)
		}
	}
	candidates := "[]"
	if u.MemoryCandidates != nil {
		if raw, err := json.Marshal(u.MemoryCandidates); err == nil {
			candidates = string(raw)
		}
	}
	now := UtcNow()
	_, err := r.pool.Exec(ctx, `
		UPDATE ai_nexus_agent_responses SET
			status=$3, content=$4, error=$5, error_code=$6,
			execution_provider=$7, transport=$8, fallback_json=$9,
			memory_candidates_json=$10,
			response_state = CASE WHEN $11 != '' THEN $11 ELSE response_state END,
			result_reference = CASE WHEN $12 != '' THEN $12 ELSE result_reference END,
			completed_at = CASE WHEN $13 THEN $14 ELSE completed_at END,
			updated_at=$14
		WHERE message_id=$1 AND agent_id=$2`,
		messageID, agentID, u.Status, u.Content, u.Error, u.ErrorCode,
		u.ExecutionProvider, u.Transport, fallback, candidates,
		u.ResponseState, u.ResultReference, terminalStatuses[u.Status], now)
	return err
}

// CancelPendingResponses cancels non-terminal responses of a message.
func (r *PG) CancelPendingResponses(ctx context.Context, messageID string) (int, error) {
	tag, err := r.pool.Exec(ctx, `
		UPDATE ai_nexus_agent_responses
		SET status='cancelled', error='REQUEST_CANCELLED', error_code='REQUEST_CANCELLED',
			completed_at=$2, updated_at=$2
		WHERE message_id=$1 AND status NOT IN ('completed','failed','cancelled')`,
		messageID, UtcNow())
	if err != nil {
		return 0, err
	}
	return int(tag.RowsAffected()), nil
}

const messageColumns = `message_id, role, content, selected_agents_json,
	business_scope, request_id, runtime_generation, created_at`

// ListMessages returns messages in ascending created_at order (the SQL
// returns DESC and the list is reversed), each with its responses.
func (r *PG) ListMessages(ctx context.Context, limit int, messageID string) ([]map[string]any, error) {
	if limit < 1 {
		limit = 1
	}
	if limit > 200 {
		limit = 200
	}
	var rows pgx.Rows
	var err error
	if messageID != "" {
		rows, err = r.pool.Query(ctx,
			"SELECT "+messageColumns+" FROM ai_nexus_group_messages WHERE message_id=$1 ORDER BY created_at DESC LIMIT $2",
			messageID, limit)
	} else {
		rows, err = r.pool.Query(ctx,
			"SELECT "+messageColumns+" FROM ai_nexus_group_messages ORDER BY created_at DESC LIMIT $1",
			limit)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var ids []string
	var messages []map[string]any
	for rows.Next() {
		var mid, role, content, agentsRaw, scope, requestID, generation, created string
		if err := rows.Scan(&mid, &role, &content, &agentsRaw, &scope, &requestID, &generation, &created); err != nil {
			return nil, err
		}
		var agents []any
		if err := json.Unmarshal([]byte(agentsRaw), &agents); err != nil || agents == nil {
			agents = []any{}
		}
		messages = append(messages, map[string]any{
			"message_id": mid, "role": role, "content": content,
			"selected_agents": agents, "business_scope": scope,
			"request_id": requestID, "runtime_generation": generation,
			"created_at": created, "responses": []any{},
		})
		ids = append(ids, mid)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(ids) > 0 {
		respRows, err := r.pool.Query(ctx, `
			SELECT response_id, message_id, agent_id, status, content, error,
				error_code, execution_provider, transport, fallback_json,
				memory_candidates_json, request_id, runtime_generation,
				response_state, result_reference, created_at, completed_at, updated_at
			FROM ai_nexus_agent_responses
			WHERE message_id = ANY($1)
			ORDER BY created_at ASC`, ids)
		if err != nil {
			return nil, err
		}
		defer respRows.Close()
		byMessage := map[string][]any{}
		for respRows.Next() {
			m, err := scanResponse(respRows)
			if err != nil {
				return nil, err
			}
			mid := m["message_id"].(string)
			byMessage[mid] = append(byMessage[mid], m)
		}
		for _, msg := range messages {
			if rs, ok := byMessage[msg["message_id"].(string)]; ok {
				msg["responses"] = rs
			}
		}
	}
	// reverse → ascending
	for i, j := 0, len(messages)-1; i < j; i, j = i+1, j-1 {
		messages[i], messages[j] = messages[j], messages[i]
	}
	return messages, nil
}

func scanResponse(row pgx.Row) (map[string]any, error) {
	var rid, mid, agentID, status, content, errText, errCode string
	var execProvider, transport, fbRaw, mcRaw, reqID, gen, respState, resultRef string
	var created, completed, updated string
	if err := row.Scan(&rid, &mid, &agentID, &status, &content, &errText,
		&errCode, &execProvider, &transport, &fbRaw, &mcRaw, &reqID, &gen,
		&respState, &resultRef, &created, &completed, &updated); err != nil {
		return nil, err
	}
	var fallback any
	if err := json.Unmarshal([]byte(fbRaw), &fallback); err != nil || fallback == nil {
		fallback = map[string]any{}
	}
	var candidates any
	if err := json.Unmarshal([]byte(mcRaw), &candidates); err != nil || candidates == nil {
		candidates = []any{}
	}
	return map[string]any{
		"response_id": rid, "message_id": mid, "agent_id": agentID,
		"status": status, "content": content, "error": errText,
		"error_code": errCode, "execution_provider": execProvider,
		"transport": transport, "fallback": fallback,
		"memory_candidates": candidates, "request_id": reqID,
		"runtime_generation": gen, "response_state": respState,
		"result_reference": resultRef, "created_at": created,
		"completed_at": completed, "updated_at": updated,
	}, nil
}

// GetMessage returns one message or nil.
func (r *PG) GetMessage(ctx context.Context, messageID string) (map[string]any, error) {
	list, err := r.ListMessages(ctx, 1, messageID)
	if err != nil || len(list) == 0 {
		return nil, err
	}
	return list[0], nil
}

// ---------------- memory / tasks ----------------

// ListMemoryItems — updated_at DESC, cap 100.
func (r *PG) ListMemoryItems(ctx context.Context) ([]map[string]any, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT memory_id, kind, title, content, business_scope, source_agent_id,
			owner_model_id, status, content_hash, source_message_id, created_at, updated_at
		FROM ai_nexus_memory_items ORDER BY updated_at DESC LIMIT 100`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id, kind, title, content, scope, srcAgent, owner, status, hash, srcMsg, created, updated string
		if err := rows.Scan(&id, &kind, &title, &content, &scope, &srcAgent, &owner, &status, &hash, &srcMsg, &created, &updated); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{
			"memory_id": id, "kind": kind, "title": title, "content": content,
			"business_scope": scope, "source_agent_id": srcAgent,
			"owner_model_id": owner, "status": status, "content_hash": hash,
			"source_message_id": srcMsg, "created_at": created, "updated_at": updated,
		})
	}
	return out, rows.Err()
}

// AddMemoryItem inserts one memory item and returns it.
func (r *PG) AddMemoryItem(ctx context.Context, kind, title, content, scope, sourceAgentID, sourceMessageID, contentHash string) (map[string]any, error) {
	id := NewID(16)
	now := UtcNow()
	_, err := r.pool.Exec(ctx, `
		INSERT INTO ai_nexus_memory_items
			(memory_id, kind, title, content, business_scope, source_agent_id,
			 owner_model_id, status, content_hash, source_message_id, created_at, updated_at)
		VALUES ($1,$2,$3,$4,$5,$6,'ai-collaboration','accepted',$7,$8,$9,$9)`,
		id, kind, title, content, scope, sourceAgentID, contentHash, sourceMessageID, now)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"memory_id": id, "kind": kind, "title": title, "content": content,
		"business_scope": scope, "source_agent_id": sourceAgentID,
		"content_hash": contentHash, "source_message_id": sourceMessageID,
		"created_at": now, "updated_at": now,
	}, nil
}

// ListTasks — updated_at DESC, cap 100.
func (r *PG) ListTasks(ctx context.Context) ([]map[string]any, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT task_id, title, status, source_message_id, participant_agents_json,
			conclusion, files_json, created_at, updated_at
		FROM ai_nexus_tasks ORDER BY updated_at DESC LIMIT 100`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id, title, status, srcMsg, paRaw, conclusion, filesRaw, created, updated string
		if err := rows.Scan(&id, &title, &status, &srcMsg, &paRaw, &conclusion, &filesRaw, &created, &updated); err != nil {
			return nil, err
		}
		var participants, files any
		if json.Unmarshal([]byte(paRaw), &participants) != nil || participants == nil {
			participants = []any{}
		}
		if json.Unmarshal([]byte(filesRaw), &files) != nil || files == nil {
			files = []any{}
		}
		out = append(out, map[string]any{
			"task_id": id, "title": title, "status": status,
			"source_message_id": srcMsg, "participant_agents": participants,
			"files": files, "conclusion": conclusion,
			"created_at": created, "updated_at": updated,
		})
	}
	return out, rows.Err()
}

// CreateTask inserts a pending task and returns it.
func (r *PG) CreateTask(ctx context.Context, title, sourceMessageID string, participants []string) (map[string]any, error) {
	id := NewID(16)
	now := UtcNow()
	paJSON, _ := json.Marshal(participants)
	_, err := r.pool.Exec(ctx, `
		INSERT INTO ai_nexus_tasks
			(task_id, title, status, source_message_id, participant_agents_json, files_json, created_at, updated_at)
		VALUES ($1,$2,'pending',$3,$4,'[]',$5,$5)`,
		id, title, sourceMessageID, string(paJSON), now)
	if err != nil {
		return nil, err
	}
	var pa any = participants
	if participants == nil {
		pa = []any{}
	}
	return map[string]any{
		"task_id": id, "title": title, "status": "pending",
		"source_message_id": sourceMessageID, "participant_agents": pa,
		"files": []any{}, "conclusion": "",
		"created_at": now, "updated_at": now,
	}, nil
}

// ---------------- collab tasks ----------------

// CreateCollabTask inserts a 'created' collab task; returns the row.
func (r *PG) CreateCollabTask(ctx context.Context, requestID, mode string, providers []string, originalRequest, generation, attemptID string) (map[string]any, error) {
	id := NewID(16)
	providersJSON, _ := json.Marshal(providers)
	if len(originalRequest) > 64000 {
		originalRequest = originalRequest[:64000]
	}
	now := UtcNow()
	_, err := r.pool.Exec(ctx, `
		INSERT INTO ai_nexus_collab_tasks
			(task_id, request_id, mode, selected_providers_json, original_request,
			 task_generation, attempt_id, overall_status, created_at, updated_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,'created',$8,$8)`,
		id, requestID, mode, string(providersJSON), originalRequest, generation, attemptID, now)
	if err != nil {
		return nil, err
	}
	return r.GetCollabTask(ctx, id)
}

// CollabTaskUpdate carries the optional update fields.
type CollabTaskUpdate struct {
	Status           string
	RequestID        string
	AttemptID        string
	Comparison       any
	Synthesis        any
	SummaryReference *string
	FaultReference   *string
	Started          bool
	Completed        bool
}

// UpdateCollabTask applies a dynamic update; status must be in
// TaskStatuses. started stamps started_at once; completed stamps
// completed_at always.
func (r *PG) UpdateCollabTask(ctx context.Context, taskID string, u CollabTaskUpdate) error {
	if u.Status != "" && !TaskStatuses[u.Status] {
		return fmt.Errorf("unknown collaboration task status: %s", u.Status)
	}
	sets := []string{"updated_at=$2"}
	args := []any{taskID, UtcNow()}
	idx := 3
	add := func(col string, v any) {
		sets = append(sets, fmt.Sprintf("%s=$%d", col, idx))
		args = append(args, v)
		idx++
	}
	if u.Status != "" {
		add("overall_status", u.Status)
	}
	if u.RequestID != "" {
		add("request_id", u.RequestID)
	}
	if u.AttemptID != "" {
		add("attempt_id", u.AttemptID)
	}
	if u.Comparison != nil {
		raw, _ := json.Marshal(u.Comparison)
		add("comparison_json", string(raw))
	}
	if u.Synthesis != nil {
		raw, _ := json.Marshal(u.Synthesis)
		add("synthesis_json", string(raw))
	}
	if u.SummaryReference != nil {
		add("summary_reference", *u.SummaryReference)
	}
	if u.FaultReference != nil {
		add("fault_reference", *u.FaultReference)
	}
	if u.Started {
		sets = append(sets, "started_at = CASE WHEN started_at='' THEN $"+itoa(idx)+" ELSE started_at END")
		args = append(args, UtcNow())
		idx++
	}
	if u.Completed {
		add("completed_at", UtcNow())
	}
	_, err := r.pool.Exec(ctx,
		"UPDATE ai_nexus_collab_tasks SET "+strings.Join(sets, ",")+" WHERE task_id=$1", args...)
	return err
}

func itoa(i int) string {
	return fmt.Sprintf("%d", i)
}

const collabTaskColumns = `task_id, request_id, mode, selected_providers_json,
	original_request, task_generation, attempt_id, overall_status,
	comparison_json, synthesis_json, summary_reference, fault_reference,
	created_at, started_at, completed_at, updated_at`

func (r *PG) scanCollabTask(ctx context.Context, row pgx.Row) (map[string]any, error) {
	var id, requestID, mode, providersRaw, original, generation, attemptID string
	var status, compRaw, synthRaw, summaryRef, faultRef, created, started, completed, updated string
	if err := row.Scan(&id, &requestID, &mode, &providersRaw, &original, &generation,
		&attemptID, &status, &compRaw, &synthRaw, &summaryRef, &faultRef,
		&created, &started, &completed, &updated); err != nil {
		return nil, err
	}
	var providers, comparison, synthesis any
	if json.Unmarshal([]byte(providersRaw), &providers) != nil || providers == nil {
		providers = []any{}
	}
	if json.Unmarshal([]byte(compRaw), &comparison) != nil || comparison == nil {
		comparison = map[string]any{}
	}
	if json.Unmarshal([]byte(synthRaw), &synthesis) != nil || synthesis == nil {
		synthesis = map[string]any{}
	}
	results, err := r.ListCollabResults(ctx, id)
	if err != nil {
		return nil, err
	}
	if results == nil {
		results = []map[string]any{}
	}
	return map[string]any{
		"task_id": id, "request_id": requestID, "mode": mode,
		"selected_providers": providers, "original_request": original,
		"task_generation": generation, "attempt_id": attemptID,
		"overall_status": status, "comparison": comparison,
		"synthesis": synthesis, "summary_reference": summaryRef,
		"fault_reference": faultRef, "created_at": created,
		"started_at": started, "completed_at": completed,
		"updated_at": updated, "provider_results": results,
	}, nil
}

// GetCollabTask returns one task with provider_results or nil.
func (r *PG) GetCollabTask(ctx context.Context, taskID string) (map[string]any, error) {
	row := r.pool.QueryRow(ctx,
		"SELECT "+collabTaskColumns+" FROM ai_nexus_collab_tasks WHERE task_id=$1", taskID)
	t, err := r.scanCollabTask(ctx, row)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return t, err
}

// ListCollabTasks — created_at DESC, clamped 1..200.
func (r *PG) ListCollabTasks(ctx context.Context, limit int) ([]map[string]any, error) {
	if limit < 1 {
		limit = 1
	}
	if limit > 200 {
		limit = 200
	}
	rows, err := r.pool.Query(ctx,
		"SELECT task_id FROM ai_nexus_collab_tasks ORDER BY created_at DESC LIMIT $1", limit)
	if err != nil {
		return nil, err
	}
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	rows.Close()
	var out []map[string]any
	for _, id := range ids {
		t, err := r.GetCollabTask(ctx, id)
		if err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, nil
}

// InterruptedCollabTasks tombstones tasks from dead generations:
// running/validating/aggregating with a different generation become
// 'partial' when they hold completed results, else 'failed'.
func (r *PG) InterruptedCollabTasks(ctx context.Context, generation string) ([]string, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT task_id FROM ai_nexus_collab_tasks
		WHERE overall_status IN ('validating','running','aggregating')
		  AND task_generation != $1`, generation)
	if err != nil {
		return nil, err
	}
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	rows.Close()
	now := UtcNow()
	for _, id := range ids {
		var completed int
		if err := r.pool.QueryRow(ctx,
			"SELECT COUNT(*) FROM ai_nexus_collab_results WHERE task_id=$1 AND response_status='completed'",
			id).Scan(&completed); err != nil {
			return ids, err
		}
		status := "failed"
		if completed > 0 {
			status = "partial"
		}
		if _, err := r.pool.Exec(ctx, `
			UPDATE ai_nexus_collab_tasks
			SET overall_status=$2, fault_reference='RUNTIME_RESTART', completed_at=$3, updated_at=$3
			WHERE task_id=$1`, id, status, now); err != nil {
			return ids, err
		}
	}
	return ids, nil
}

// UpsertCollabResult inserts or updates a provider result row keyed by
// (task_id, provider_id, attempt_id); updates touch only the mutable
// columns (parity with ON CONFLICT DO UPDATE column list).
func (r *PG) UpsertCollabResult(ctx context.Context, rec map[string]any) error {
	taskID, _ := rec["task_id"].(string)
	providerID, _ := rec["provider_id"].(string)
	attemptID, _ := rec["attempt_id"].(string)
	pk := taskID + ":" + providerID + ":" + attemptID
	actions, _ := json.Marshal(rec["suggested_actions"])
	if rec["suggested_actions"] == nil {
		actions = []byte("[]")
	}
	_, err := r.pool.Exec(ctx, `
		INSERT INTO ai_nexus_collab_results
			(result_pk, task_id, request_id, provider_id, response_id, attempt_id,
			 response_text, response_status, capture_method, captured_at,
			 completion_evidence, adapter_version, content_class, suggested_actions_json, created_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
		ON CONFLICT (task_id, provider_id, attempt_id) DO UPDATE SET
			response_text=EXCLUDED.response_text,
			response_status=EXCLUDED.response_status,
			capture_method=EXCLUDED.capture_method,
			captured_at=EXCLUDED.captured_at,
			completion_evidence=EXCLUDED.completion_evidence,
			suggested_actions_json=EXCLUDED.suggested_actions_json`,
		pk, taskID, str(rec, "request_id"), providerID, str(rec, "response_id"), attemptID,
		str(rec, "response_text"), str(rec, "response_status"), str(rec, "capture_method"),
		str(rec, "captured_at"), str(rec, "completion_evidence"), str(rec, "adapter_version"),
		strDefault(rec, "content_class", "UNTRUSTED_EXTERNAL_CONTENT"), string(actions), UtcNow())
	return err
}

// ListCollabResults — created_at ASC; result_pk stripped.
func (r *PG) ListCollabResults(ctx context.Context, taskID string) ([]map[string]any, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT task_id, request_id, provider_id, response_id, attempt_id,
			response_text, response_status, capture_method, captured_at,
			completion_evidence, adapter_version, content_class,
			suggested_actions_json, created_at
		FROM ai_nexus_collab_results WHERE task_id=$1 ORDER BY created_at ASC`, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var id, requestID, providerID, responseID, attemptID string
		var text, status, method, capturedAt, evidence, adapterVersion, contentClass, actionsRaw, created string
		if err := rows.Scan(&id, &requestID, &providerID, &responseID, &attemptID,
			&text, &status, &method, &capturedAt, &evidence, &adapterVersion,
			&contentClass, &actionsRaw, &created); err != nil {
			return nil, err
		}
		var actions any
		if json.Unmarshal([]byte(actionsRaw), &actions) != nil || actions == nil {
			actions = []any{}
		}
		out = append(out, map[string]any{
			"task_id": id, "request_id": requestID, "provider_id": providerID,
			"response_id": responseID, "attempt_id": attemptID,
			"response_text": text, "response_status": status,
			"capture_method": method, "captured_at": capturedAt,
			"completion_evidence": evidence, "adapter_version": adapterVersion,
			"content_class": contentClass, "suggested_actions": actions,
			"created_at": created,
		})
	}
	return out, rows.Err()
}

// CancelCollabResults marks pending/running/awaiting-user results of a
// task cancelled; returns affected provider ids.
func (r *PG) CancelCollabResults(ctx context.Context, taskID string) error {
	_, err := r.pool.Exec(ctx, `
		UPDATE ai_nexus_collab_results
		SET response_status='cancelled', completion_evidence='cancel_remote_state=unconfirmed'
		WHERE task_id=$1 AND response_status IN ('pending','running','awaiting-user')`,
		taskID)
	return err
}

// CompletedResultProviders returns provider ids with a completed result
// for the given attempt (empty attemptID = all attempts).
func (r *PG) CompletedResultProviders(ctx context.Context, taskID, attemptID string) (map[string]bool, error) {
	var rows pgx.Rows
	var err error
	if attemptID != "" {
		rows, err = r.pool.Query(ctx,
			"SELECT provider_id FROM ai_nexus_collab_results WHERE task_id=$1 AND attempt_id=$2 AND response_status='completed'",
			taskID, attemptID)
	} else {
		rows, err = r.pool.Query(ctx,
			"SELECT provider_id FROM ai_nexus_collab_results WHERE task_id=$1 AND response_status='completed'",
			taskID)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			return nil, err
		}
		out[p] = true
	}
	return out, rows.Err()
}

func str(m map[string]any, key string) string {
	if v, ok := m[key].(string); ok {
		return v
	}
	return ""
}

func strDefault(m map[string]any, key, def string) string {
	if v := str(m, key); v != "" {
		return v
	}
	return def
}
