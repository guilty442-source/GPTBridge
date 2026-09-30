// Package domain ports the retired ai_collaboration domain layer —
// task_protocol, external_content, result_comparator and
// result_synthesizer — with identical keys, statuses and limits.
package domain

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"gptbridge.local/ai-collaboration-backend/internal/repo"
)

// ErrPermissionDenied is raised by every protocol normalizer failure.
var ErrPermissionDenied = errors.New("PERMISSION_DENIED")

// str safely reads a string field from a JSON-shaped row.
func str(m map[string]any, key string) string {
	if v, ok := m[key].(string); ok {
		return v
	}
	return ""
}

const (
	AITaskSchemaVersion = "1.0"

	ExternalContentClass  = "UNTRUSTED_EXTERNAL_CONTENT"
	MaxExternalTextChars  = 64000
	MaxTaskContentChars   = 64000
	MaxMemoryItems        = 20
	MaxMemoryContentChars = 2000
	MaxMemoryClipChars    = 4000
	AdapterVersion        = "1.1.0"
)

var (
	AITaskProviders  = setOf("chatgpt", "claude", "gemini", "grok", "deepseek", "perplexity")
	AITaskScopes     = setOf("general", "investment")
	AITaskTypes      = setOf("general", "orchestration", "search", "advanced_search", "calculation", "longform", "reasoning", "social_media", "trends", "breaking_news")
	AITaskRequesters = setOf("xingcheng", "ai-collaboration")

	// FixedTaskOwners — verbatim FIXED_TASK_OWNERS.
	FixedTaskOwners = map[string]string{
		"general":                      "chatgpt",
		"orchestration":                "chatgpt",
		"search":                       "gemini",
		"advanced_search":              "perplexity",
		"calculation":                  "perplexity",
		"longform":                     "claude",
		"reasoning":                    "deepseek",
		"social_media":                 "grok",
		"trends":                       "grok",
		"breaking_news":                "grok",
		"training-candidate-authoring": "chatgpt",
	}
)

func setOf(items ...string) map[string]bool {
	m := make(map[string]bool, len(items))
	for _, i := range items {
		m[i] = true
	}
	return m
}

// ---------------- task_protocol ----------------

func normalizedText(v any, maximum int, required bool) (string, error) {
	s, ok := v.(string)
	if !ok || strings.ContainsRune(s, '\x00') {
		return "", ErrPermissionDenied
	}
	s = strings.TrimSpace(s)
	if required && s == "" {
		return "", ErrPermissionDenied
	}
	if len(s) > maximum {
		return "", ErrPermissionDenied
	}
	return s, nil
}

// SanitizeMemoryContext ports sanitize_memory_context.
func SanitizeMemoryContext(items any) ([]map[string]any, error) {
	list, ok := items.([]any)
	if !ok && items != nil {
		return nil, ErrPermissionDenied
	}
	var out []map[string]any
	for i, raw := range list {
		if i >= MaxMemoryItems {
			break
		}
		item, ok := raw.(map[string]any)
		if !ok {
			return nil, ErrPermissionDenied
		}
		content, err := normalizedText(item["content"], MaxMemoryContentChars, true)
		if err != nil {
			return nil, err
		}
		memoryID, _ := normalizedText(item["memory_id"], 64, false)
		if memoryID == "" {
			memoryID = repo.NewID(16)
		}
		kind, _ := normalizedText(item["kind"], 64, false)
		if kind == "" {
			kind = "context"
		}
		title, _ := normalizedText(item["title"], 160, false)
		if title == "" {
			title = clip(content, 80)
		}
		origin, _ := normalizedText(item["origin_model_id"], 96, false)
		if origin == "" {
			origin = "star-main-native-model"
		}
		scope, _ := normalizedText(item["business_scope"], 32, false)
		if scope == "" {
			scope = "general"
		}
		out = append(out, map[string]any{
			"memory_id": memoryID, "kind": kind, "title": title,
			"content": content, "origin_model_id": origin,
			"business_scope": scope,
		})
	}
	return out, nil
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// BuildAITaskEnvelope ports build_ai_task_envelope; every normalized
// field must be in its closed set else permission denied.
func BuildAITaskEnvelope(provider, scope, taskType, content, requestedBy string, memoryContext []map[string]any, memoryWriteback bool) (map[string]any, error) {
	p, err := normalizedText(provider, 64, true)
	if err != nil || !AITaskProviders[p] {
		return nil, ErrPermissionDenied
	}
	sc, err := normalizedText(scope, 32, true)
	if err != nil || !AITaskScopes[sc] {
		return nil, ErrPermissionDenied
	}
	t, err := normalizedText(taskType, 64, true)
	if err != nil || !AITaskTypes[t] {
		return nil, ErrPermissionDenied
	}
	c, err := normalizedText(content, MaxTaskContentChars, true)
	if err != nil {
		return nil, ErrPermissionDenied
	}
	r, err := normalizedText(requestedBy, 64, true)
	if err != nil || !AITaskRequesters[r] {
		return nil, ErrPermissionDenied
	}
	if memoryContext == nil {
		memoryContext = []map[string]any{}
	}
	writeback := "disabled"
	if memoryWriteback {
		writeback = "candidate-only"
	}
	return map[string]any{
		"schema_version":     AITaskSchemaVersion,
		"task_id":            repo.NewID(32),
		"provider":           p,
		"business_scope":     sc,
		"task_type":          t,
		"content":            c,
		"requested_by":       r,
		"response_recipient": r,
		"memory_context":     memoryContext,
		"memory_policy": map[string]any{
			"broker":                   "star-main-native-model",
			"direct_database_access":   false,
			"writeback":                writeback,
			"model_database_isolation": "mandatory",
		},
		"created_at": repo.UtcNow(),
	}, nil
}

// BuildMemoryCandidate ports build_memory_candidate.
func BuildMemoryCandidate(task map[string]any, sourceAgentID, content string) (map[string]any, error) {
	text, err := normalizedText(content, MaxTaskContentChars, true)
	if err != nil {
		return nil, err
	}
	clipped := clip(text, MaxMemoryClipChars)
	canonical, _ := json.Marshal(map[string]any{
		"provider": str(task, "provider"), "scope": str(task, "business_scope"),
		"task": str(task, "task_type"), "content": clipped,
	})
	sum := sha256.Sum256(canonical)
	agent, _ := normalizedText(sourceAgentID, 64, false)
	scope := str(task, "business_scope")
	if scope == "" {
		scope = "general"
	}
	taskType := str(task, "task_type")
	if taskType == "" {
		taskType = "external-ai"
	}
	firstLine := clipped
	if idx := strings.IndexByte(clipped, '\n'); idx >= 0 {
		firstLine = clipped[:idx]
	}
	return map[string]any{
		"candidate_id":          hex.EncodeToString(sum[:])[:24],
		"kind":                  taskType,
		"title":                 clip(firstLine, 120),
		"content":               clipped,
		"business_scope":        scope,
		"source_agent_id":       agent,
		"source_provider":       str(task, "provider"),
		"status":                "candidate",
		"direct_database_write": false,
		"reviewer":              "star-main-native-model",
		"created_at":            repo.UtcNow(),
	}, nil
}

// ---------------- external_content ----------------

// SealExternalText clips text to the external-content bound.
func SealExternalText(text string) string {
	return clip(text, MaxExternalTextChars)
}

// actionHint mirrors the retired _ACTION_HINT regex semantics: a line
// whose content (after optional bullet/numbering) opens a fenced
// shell block or begins with a dangerous shell verb. Python `re` used
// (?m) + case-insensitive matching on the stripped line start.
var actionHint = regexp.MustCompile(`(?i)^\s*(?:[-*>\d.\)]+\s*)?(?:` + "```" + `(?:bash|sh|shell|powershell|cmd|ps1)|(?:rm|del|sudo|git\s+(?:push|reset|checkout|commit)|curl|wget|invoke-|remove-item|drop\s+table|delete\s+from|execute|run)\b)`)

// SuggestedActions extracts dangerous-action hints, cap 16 × 240 chars.
func SuggestedActions(text string) []string {
	var out []string
	for _, line := range strings.Split(text, "\n") {
		if len(out) >= 16 {
			break
		}
		loc := actionHint.FindStringIndex(line)
		if loc == nil {
			continue
		}
		out = append(out, clip(strings.TrimSpace(line[loc[0]:loc[1]]), 240))
	}
	return out
}

// SealProviderResponse ports seal_provider_response.
func SealProviderResponse(providerID, requestID, responseID, text, captureMethod, completionEvidence, adapterVersion, capturedAt, status string) map[string]any {
	if status == "" {
		status = "completed"
	}
	return map[string]any{
		"request_id":          requestID,
		"provider_id":         providerID,
		"response_id":         responseID,
		"response_text":       SealExternalText(text),
		"response_status":     status,
		"capture_method":      captureMethod,
		"captured_at":         capturedAt,
		"completion_evidence": completionEvidence,
		"adapter_version":     adapterVersion,
		"content_class":       ExternalContentClass,
		"suggested_actions":   SuggestedActions(text),
	}
}

// ---------------- result_comparator ----------------

var numberRe = regexp.MustCompile(`\d+(?:\.\d+)?`)

var negationMarkers = []string{
	"不是", "沒有", "不會", "不能", "不可", "無法", "並非",
	"never", "not", "no ", "cannot", "can't", "don't", "doesn't", "isn't", "aren't",
}

func normalizeStatement(s string) string {
	return strings.Join(strings.Fields(strings.ToLower(s)), " ")
}

// splitSentences mirrors the retired _SENTENCE_SPLIT: split after
// full/half-width sentence terminators followed by whitespace, and on
// runs of newlines.
func splitSentences(text string) []string {
	var out []string
	var cur strings.Builder
	runes := []rune(text)
	isTerm := func(r rune) bool {
		return r == '。' || r == '！' || r == '？' || r == '!' || r == '?'
	}
	for i := 0; i < len(runes); i++ {
		r := runes[i]
		if r == '\n' {
			out = append(out, cur.String())
			cur.Reset()
			for i+1 < len(runes) && runes[i+1] == '\n' {
				i++
			}
			continue
		}
		cur.WriteRune(r)
		if isTerm(r) {
			j := i + 1
			for j < len(runes) && (runes[j] == ' ' || runes[j] == '\t') {
				j++
			}
			if j > i+1 {
				out = append(out, cur.String())
				cur.Reset()
				i = j - 1
			}
		}
	}
	if cur.Len() > 0 {
		out = append(out, cur.String())
	}
	return out
}

// statements mirrors _statements: strip bullet/dash prefixes, keep ≥4.
func statements(text string) []string {
	var out []string
	for _, s := range splitSentences(text) {
		item := strings.TrimSpace(s)
		item = strings.Trim(item, "-*•·\t")
		item = strings.TrimSpace(item)
		if len(item) >= 4 {
			out = append(out, item)
		}
	}
	return out
}

func hasNegation(s string) bool {
	lower := strings.ToLower(s)
	for _, m := range negationMarkers {
		if strings.Contains(lower, m) {
			return true
		}
	}
	return false
}

func numbers(s string) []string {
	return numberRe.FindAllString(s, -1)
}

func sameNumbers(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	as, bs := append([]string(nil), a...), append([]string(nil), b...)
	sort.Strings(as)
	sort.Strings(bs)
	for i := range as {
		if as[i] != bs[i] {
			return false
		}
	}
	return len(as) > 0
}

// completedResults filters rows to completed with non-empty text.
func completedResults(responses []map[string]any) []map[string]any {
	var out []map[string]any
	for _, r := range responses {
		if str(r, "response_status") == "completed" && strings.TrimSpace(str(r, "response_text")) != "" {
			out = append(out, r)
		}
	}
	return out
}

// Compare ports ResultComparator.compare.
func Compare(taskID string, responses []map[string]any) map[string]any {
	completed := completedResults(responses)
	byProvider := map[string][]string{}
	var providers []string
	for _, r := range completed {
		p := str(r, "provider_id")
		if _, ok := byProvider[p]; !ok {
			providers = append(providers, p)
		}
		byProvider[p] = statements(str(r, "response_text"))
	}
	sort.Strings(providers)

	index := map[string]map[string]bool{}
	originals := map[string]string{}
	for _, p := range providers {
		for _, st := range byProvider[p] {
			key := normalizeStatement(st)
			if key == "" {
				continue
			}
			if index[key] == nil {
				index[key] = map[string]bool{}
				originals[key] = st
			}
			index[key][p] = true
		}
	}
	var common, differences []any
	for key, sources := range index {
		if len(sources) > 1 {
			var srcs []string
			for s := range sources {
				srcs = append(srcs, s)
			}
			sort.Strings(srcs)
			var arr []any
			for _, s := range srcs {
				arr = append(arr, s)
			}
			common = append(common, map[string]any{"text": originals[key], "sources": arr})
		} else {
			for s := range sources {
				differences = append(differences, map[string]any{
					"text": originals[key], "source_provider": s,
				})
			}
		}
	}
	sortByText(common)
	sortByText(differences)

	// Contradictions: pairwise provider statements with identical number
	// sets and different negation polarity.
	var contradictions []any
	for i := 0; i < len(providers) && len(contradictions) < 8; i++ {
		for j := i + 1; j < len(providers) && len(contradictions) < 8; j++ {
			left, right := providers[i], providers[j]
			for _, ls := range byProvider[left] {
				ln := numbers(ls)
				if len(ln) == 0 {
					continue
				}
				lneg := hasNegation(ls)
				for _, rs := range byProvider[right] {
					if !sameNumbers(ln, numbers(rs)) {
						continue
					}
					if hasNegation(rs) == lneg {
						continue
					}
					topic := append([]string(nil), ln...)
					sort.Strings(topic)
					if len(topic) > 3 {
						topic = topic[:3]
					}
					var topicArr []any
					for _, t := range topic {
						topicArr = append(topicArr, t)
					}
					contradictions = append(contradictions, map[string]any{
						"topic": topicArr,
						"statements": []any{
							map[string]any{"provider_id": left, "text": ls},
							map[string]any{"provider_id": right, "text": rs},
						},
					})
					goto nextLeft
				}
			}
		nextLeft:
		}
	}

	// Unanswered questions: statements ending with ?/？; answered iff
	// another provider's statement shares any number token.
	type question struct {
		key, raisedBy string
	}
	var questions []question
	for _, p := range providers {
		for _, st := range byProvider[p] {
			trimmed := strings.TrimSpace(st)
			if strings.HasSuffix(trimmed, "?") || strings.HasSuffix(trimmed, "？") {
				questions = append(questions, question{normalizeStatement(st), p})
			}
		}
	}
	var unanswered []any
	for _, q := range questions {
		if len(unanswered) >= 8 {
			break
		}
		qnums := map[string]bool{}
		for _, n := range numbers(q.key) {
			qnums[n] = true
		}
		answered := false
		if len(qnums) > 0 {
			for _, p := range providers {
				if p == q.raisedBy {
					continue
				}
				for _, st := range byProvider[p] {
					for _, n := range numbers(st) {
						if qnums[n] {
							answered = true
							break
						}
					}
					if answered {
						break
					}
				}
				if answered {
					break
				}
			}
		}
		if !answered {
			unanswered = append(unanswered, map[string]any{
				"question": q.key, "raised_by": q.raisedBy,
			})
		}
	}

	var individual []any
	var providersCompared []any
	for _, p := range providers {
		providersCompared = append(providersCompared, p)
	}
	for _, r := range completed {
		individual = append(individual, map[string]any{
			"provider_id":   str(r, "provider_id"),
			"response_id":   str(r, "response_id"),
			"response_text": str(r, "response_text"),
		})
	}
	if common == nil {
		common = []any{}
	}
	if differences == nil {
		differences = []any{}
	}
	if contradictions == nil {
		contradictions = []any{}
	}
	if unanswered == nil {
		unanswered = []any{}
	}
	if individual == nil {
		individual = []any{}
	}
	if providersCompared == nil {
		providersCompared = []any{}
	}
	return map[string]any{
		"task_id":              taskID,
		"individual_responses": individual,
		"common_points":        common,
		"differences":          differences,
		"contradictions":       contradictions,
		"unanswered_questions": unanswered,
		"providers_compared":   providersCompared,
		"note":                 "Comparison describes output similarity only; agreement between models is not evidence of correctness.",
	}
}

func sortByText(items []any) {
	sort.SliceStable(items, func(i, j int) bool {
		ti, _ := items[i].(map[string]any)["text"].(string)
		tj, _ := items[j].(map[string]any)["text"].(string)
		return ti < tj
	})
}

// ---------------- result_synthesizer ----------------

// Synthesize ports ResultSynthesizer.synthesize — deterministic summary
// (the governed-model path was never injected in the retired service).
func Synthesize(taskID, originalRequest string, responses []map[string]any, comparison map[string]any) map[string]any {
	completed := completedResults(responses)
	var lines []string
	lines = append(lines, fmt.Sprintf("協作整合（%d 個 AI 回覆，去重後摘錄重點；共識不代表事實正確）", len(completed)))
	lines = append(lines, "原始需求："+clip(originalRequest, 200))

	seen := map[string]bool{}
	for _, r := range completed {
		p := str(r, "provider_id")
		lines = append(lines, "\n【"+p+"】")
		count := 0
		for _, st := range statements(str(r, "response_text")) {
			if count >= 12 {
				break
			}
			key := normalizeStatement(st)
			if seen[key] {
				continue
			}
			seen[key] = true
			lines = append(lines, "- "+st)
			count++
		}
	}
	if common := getList(comparison, "common_points"); len(common) > 0 {
		lines = append(lines, "\n■ 共同觀點")
		for i, c := range common {
			if i >= 8 {
				break
			}
			if m, ok := c.(map[string]any); ok {
				lines = append(lines, "- "+str(m, "text"))
			}
		}
	}
	if diffs := getList(comparison, "differences"); len(diffs) > 0 {
		lines = append(lines, "\n■ 各 AI 差異")
		for i, d := range diffs {
			if i >= 12 {
				break
			}
			if m, ok := d.(map[string]any); ok {
				lines = append(lines, "- ["+str(m, "source_provider")+"] "+str(m, "text"))
			}
		}
	}
	if contra := getList(comparison, "contradictions"); len(contra) > 0 {
		lines = append(lines, "\n■ 相互矛盾點")
		for i, c := range contra {
			if i >= 8 {
				break
			}
			if m, ok := c.(map[string]any); ok {
				if stmts, ok := m["statements"].([]any); ok {
					for _, s := range stmts {
						if sm, ok := s.(map[string]any); ok {
							lines = append(lines, "- ["+str(sm, "provider_id")+"] "+str(sm, "text"))
						}
					}
				}
			}
		}
	}
	if unans := getList(comparison, "unanswered_questions"); len(unans) > 0 {
		lines = append(lines, "\n■ 未獲回答的問題")
		for i, q := range unans {
			if i >= 8 {
				break
			}
			if m, ok := q.(map[string]any); ok {
				lines = append(lines, "- "+str(m, "question")+"（"+str(m, "raised_by")+"）")
			}
		}
	}

	var sources []any
	for _, r := range completed {
		sources = append(sources, map[string]any{
			"provider_id":    str(r, "provider_id"),
			"response_id":    str(r, "response_id"),
			"capture_method": str(r, "capture_method"),
		})
	}
	if sources == nil {
		sources = []any{}
	}
	return map[string]any{
		"task_id":                 taskID,
		"method":                  "deterministic",
		"content_class":           ExternalContentClass,
		"summary":                 strings.Join(lines, "\n"),
		"sources":                 sources,
		"raw_responses_preserved": true,
		"comparison_reference":    "collab_task:" + taskID + ":comparison",
	}
}

func getList(m map[string]any, key string) []any {
	if m == nil {
		return nil
	}
	if v, ok := m[key].([]any); ok {
		return v
	}
	return nil
}
