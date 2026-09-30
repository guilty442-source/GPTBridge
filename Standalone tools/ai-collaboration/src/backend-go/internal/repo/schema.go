package repo

// DDL parity with the retired collab_repo_schema.py — schema
// gptbridge_collab (env override AI_COLLAB_PG_SCHEMA), all CREATE TABLE
// IF NOT EXISTS + idempotent migration ALTERs.
const ddlTables = `
CREATE TABLE IF NOT EXISTS ai_nexus_agents (
	agent_id TEXT PRIMARY KEY,
	name TEXT NOT NULL,
	provider TEXT NOT NULL,
	home_url TEXT NOT NULL,
	general_url TEXT NOT NULL DEFAULT '',
	investment_url TEXT NOT NULL DEFAULT '',
	star_training_url TEXT NOT NULL DEFAULT '',
	general_enabled INTEGER NOT NULL DEFAULT 1,
	investment_enabled INTEGER NOT NULL DEFAULT 1,
	business_capabilities_json TEXT NOT NULL DEFAULT '[]',
	enabled INTEGER NOT NULL DEFAULT 1,
	selected INTEGER NOT NULL DEFAULT 1,
	status TEXT NOT NULL DEFAULT 'idle',
	last_error TEXT NOT NULL DEFAULT '',
	session_state TEXT NOT NULL DEFAULT 'closed',
	login_state TEXT NOT NULL DEFAULT 'unknown',
	adapter_version TEXT NOT NULL DEFAULT '',
	sort_seq INTEGER NOT NULL DEFAULT 0,
	updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_nexus_group_messages (
	message_id TEXT PRIMARY KEY,
	role TEXT NOT NULL,
	content TEXT NOT NULL,
	selected_agents_json TEXT NOT NULL DEFAULT '[]',
	business_scope TEXT NOT NULL DEFAULT 'general',
	request_id TEXT NOT NULL DEFAULT '',
	runtime_generation TEXT NOT NULL DEFAULT '',
	created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_nexus_group_messages_created
	ON ai_nexus_group_messages (created_at DESC);
CREATE TABLE IF NOT EXISTS ai_nexus_agent_responses (
	response_id TEXT PRIMARY KEY,
	message_id TEXT NOT NULL REFERENCES ai_nexus_group_messages(message_id) ON DELETE CASCADE,
	agent_id TEXT NOT NULL,
	status TEXT NOT NULL,
	content TEXT NOT NULL DEFAULT '',
	error TEXT NOT NULL DEFAULT '',
	error_code TEXT NOT NULL DEFAULT '',
	execution_provider TEXT NOT NULL DEFAULT '',
	transport TEXT NOT NULL DEFAULT '',
	fallback_json TEXT NOT NULL DEFAULT '{}',
	memory_candidates_json TEXT NOT NULL DEFAULT '[]',
	request_id TEXT NOT NULL DEFAULT '',
	runtime_generation TEXT NOT NULL DEFAULT '',
	response_state TEXT NOT NULL DEFAULT '',
	result_reference TEXT NOT NULL DEFAULT '',
	created_at TEXT NOT NULL,
	completed_at TEXT NOT NULL DEFAULT '',
	updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_nexus_agent_responses_msg
	ON ai_nexus_agent_responses (message_id, agent_id);
CREATE TABLE IF NOT EXISTS ai_nexus_workspaces (
	workspace_id TEXT PRIMARY KEY,
	name TEXT NOT NULL,
	root_path TEXT NOT NULL DEFAULT '',
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_nexus_tasks (
	task_id TEXT PRIMARY KEY,
	title TEXT NOT NULL,
	status TEXT NOT NULL DEFAULT 'pending',
	source_message_id TEXT NOT NULL DEFAULT '',
	participant_agents_json TEXT NOT NULL DEFAULT '[]',
	conclusion TEXT NOT NULL DEFAULT '',
	files_json TEXT NOT NULL DEFAULT '[]',
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_nexus_memory_items (
	memory_id TEXT PRIMARY KEY,
	kind TEXT NOT NULL,
	title TEXT NOT NULL,
	content TEXT NOT NULL,
	business_scope TEXT NOT NULL DEFAULT 'general',
	source_agent_id TEXT NOT NULL DEFAULT '',
	owner_model_id TEXT NOT NULL DEFAULT 'ai-collaboration',
	status TEXT NOT NULL DEFAULT 'accepted',
	content_hash TEXT NOT NULL DEFAULT '',
	source_message_id TEXT NOT NULL DEFAULT '',
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_nexus_knowledge_files (
	file_id TEXT PRIMARY KEY,
	file_path TEXT NOT NULL,
	file_type TEXT NOT NULL DEFAULT '',
	title TEXT NOT NULL DEFAULT '',
	content_text TEXT NOT NULL DEFAULT '',
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_nexus_tools (
	tool_id TEXT PRIMARY KEY,
	name TEXT NOT NULL,
	command TEXT NOT NULL DEFAULT '',
	enabled INTEGER NOT NULL DEFAULT 1,
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_nexus_collab_tasks (
	task_id TEXT PRIMARY KEY,
	request_id TEXT NOT NULL DEFAULT '',
	mode TEXT NOT NULL,
	selected_providers_json TEXT NOT NULL DEFAULT '[]',
	original_request TEXT NOT NULL DEFAULT '',
	task_generation TEXT NOT NULL DEFAULT '',
	attempt_id TEXT NOT NULL DEFAULT '',
	overall_status TEXT NOT NULL DEFAULT 'created',
	comparison_json TEXT NOT NULL DEFAULT '{}',
	synthesis_json TEXT NOT NULL DEFAULT '{}',
	summary_reference TEXT NOT NULL DEFAULT '',
	fault_reference TEXT NOT NULL DEFAULT '',
	created_at TEXT NOT NULL,
	started_at TEXT NOT NULL DEFAULT '',
	completed_at TEXT NOT NULL DEFAULT '',
	updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_nexus_collab_tasks_created
	ON ai_nexus_collab_tasks (created_at DESC);
CREATE TABLE IF NOT EXISTS ai_nexus_collab_results (
	result_pk TEXT PRIMARY KEY,
	task_id TEXT NOT NULL REFERENCES ai_nexus_collab_tasks(task_id) ON DELETE CASCADE,
	request_id TEXT NOT NULL DEFAULT '',
	provider_id TEXT NOT NULL,
	response_id TEXT NOT NULL DEFAULT '',
	attempt_id TEXT NOT NULL DEFAULT '',
	response_text TEXT NOT NULL DEFAULT '',
	response_status TEXT NOT NULL DEFAULT '',
	capture_method TEXT NOT NULL DEFAULT '',
	captured_at TEXT NOT NULL DEFAULT '',
	completion_evidence TEXT NOT NULL DEFAULT '',
	adapter_version TEXT NOT NULL DEFAULT '',
	content_class TEXT NOT NULL DEFAULT 'UNTRUSTED_EXTERNAL_CONTENT',
	suggested_actions_json TEXT NOT NULL DEFAULT '[]',
	created_at TEXT NOT NULL,
	UNIQUE (task_id, provider_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_nexus_collab_results_task
	ON ai_nexus_collab_results (task_id);
`

// migrationColumns mirrors _ensure_migration_columns — idempotent
// ALTERs applied on every open.
var migrationColumns = [][3]string{
	{"ai_nexus_agents", "general_url", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_agents", "investment_url", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_agents", "star_training_url", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_agents", "general_enabled", "INTEGER NOT NULL DEFAULT 1"},
	{"ai_nexus_agents", "investment_enabled", "INTEGER NOT NULL DEFAULT 1"},
	{"ai_nexus_agents", "business_capabilities_json", "TEXT NOT NULL DEFAULT '[]'"},
	{"ai_nexus_agents", "session_state", "TEXT NOT NULL DEFAULT 'closed'"},
	{"ai_nexus_agents", "login_state", "TEXT NOT NULL DEFAULT 'unknown'"},
	{"ai_nexus_agents", "adapter_version", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_agents", "sort_seq", "INTEGER NOT NULL DEFAULT 0"},
	{"ai_nexus_agent_responses", "error_code", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_agent_responses", "response_state", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_agent_responses", "result_reference", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_collab_tasks", "task_generation", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_collab_tasks", "attempt_id", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_collab_tasks", "summary_reference", "TEXT NOT NULL DEFAULT ''"},
	{"ai_nexus_collab_tasks", "fault_reference", "TEXT NOT NULL DEFAULT ''"},
}

// defaultAgents is the exact DEFAULT_AGENTS seed (sort_seq ordering).
var defaultAgents = []seedAgent{
	{1, "chatgpt", "ChatGPT", "chatgpt", "https://chatgpt.com/", []string{"general", "comprehensive", "orchestration"}},
	{2, "claude", "Claude", "claude", "https://claude.ai/", []string{"general", "longform"}},
	{3, "gemini", "Gemini", "gemini", "https://gemini.google.com/", []string{"general", "search"}},
	{4, "grok", "Grok", "grok", "https://grok.com/", []string{"general", "social_media", "trends", "breaking_news"}},
	{5, "deepseek", "DeepSeek", "deepseek", "https://chat.deepseek.com/", []string{"general", "reasoning"}},
	{6, "perplexity", "Perplexity", "perplexity", "https://www.perplexity.ai/", []string{"general", "advanced_search", "calculation"}},
}

var retiredAgentIDs = []string{"google-search"}

type seedAgent struct {
	SortSeq      int
	AgentID      string
	Name         string
	Provider     string
	HomeURL      string
	Capabilities []string
}
