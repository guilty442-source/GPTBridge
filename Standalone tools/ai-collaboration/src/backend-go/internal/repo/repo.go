// Package repo is the PostgreSQL repository for the ai-collaboration
// tool — parity with the retired collab_repo_* mixins. Data authority:
// schema gptbridge_collab (AI_COLLAB_PG_SCHEMA override), governed DSN,
// fail-closed. All rows are returned as JSON-shaped map[string]any with
// the exact Python key names.
package repo

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

var schemaPattern = regexp.MustCompile(`^[a-z_][a-z0-9_]{0,62}$`)

// PG is the PostgreSQL-backed repository.
type PG struct {
	pool   *pgxpool.Pool
	schema string
}

// Schema resolves the governed schema name.
func Schema() string {
	if v := strings.TrimSpace(os.Getenv("AI_COLLAB_PG_SCHEMA")); v != "" && schemaPattern.MatchString(v) {
		return v
	}
	return "gptbridge_collab"
}

// DBPath reports the authority identifier string.
func (r *PG) DBPath() string { return "postgresql:" + r.schema }

// UtcNow mirrors datetime.now(timezone.utc).isoformat().
func UtcNow() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05.999999999+00:00")
}

// Open connects with a bounded timeout and applies schema + seed data.
func Open(ctx context.Context, dsn, schema string) (*PG, error) {
	if !schemaPattern.MatchString(schema) {
		return nil, errors.New("PERMISSION_DENIED:invalid-schema")
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, err
	}
	cfg.ConnConfig.ConnectTimeout = 5 * time.Second
	cfg.MaxConns = 4
	cfg.AfterConnect = func(ctx context.Context, c *pgx.Conn) error {
		_, err := c.Exec(ctx, "SET search_path TO "+schema)
		return err
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	r := &PG{pool: pool, schema: schema}
	if err := r.bootstrap(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return r, nil
}

// Close releases the pool.
func (r *PG) Close() { r.pool.Close() }

func (r *PG) bootstrap(ctx context.Context) error {
	// The schema is provisioned by the governed admin lane; the runtime
	// identity may lack CREATE on the database. Tolerate 42501 only when
	// the schema already exists — otherwise fail closed.
	if _, err := r.pool.Exec(ctx, "CREATE SCHEMA IF NOT EXISTS "+r.schema); err != nil {
		var pgErr *pgconn.PgError
		exists := false
		if errors.As(err, &pgErr) && pgErr.Code == "42501" {
			var n int
			if qErr := r.pool.QueryRow(ctx,
				"SELECT count(*) FROM information_schema.schemata WHERE schema_name=$1",
				r.schema).Scan(&n); qErr == nil && n > 0 {
				exists = true
			}
		}
		if !exists {
			return fmt.Errorf("schema: %w", err)
		}
	}
	if _, err := r.pool.Exec(ctx, ddlTables); err != nil {
		return fmt.Errorf("ddl: %w", err)
	}
	for _, m := range migrationColumns {
		stmt := fmt.Sprintf("ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s", m[0], m[1], m[2])
		if _, err := r.pool.Exec(ctx, stmt); err != nil {
			return fmt.Errorf("migrate %s.%s: %w", m[0], m[1], err)
		}
	}
	if _, err := r.pool.Exec(ctx,
		"UPDATE ai_nexus_agents SET general_url=home_url WHERE TRIM(general_url)=''"); err != nil {
		return err
	}
	if _, err := r.pool.Exec(ctx,
		"UPDATE ai_nexus_agents SET star_training_url=general_url WHERE agent_id='chatgpt' AND TRIM(star_training_url)=''"); err != nil {
		return err
	}
	return r.ensureDefaultAgents(ctx)
}

func (r *PG) ensureDefaultAgents(ctx context.Context) error {
	now := UtcNow()
	for _, a := range defaultAgents {
		caps, _ := json.Marshal(a.Capabilities)
		starURL := ""
		if a.AgentID == "chatgpt" {
			starURL = a.HomeURL
		}
		if _, err := r.pool.Exec(ctx, `
			INSERT INTO ai_nexus_agents (agent_id,name,provider,home_url,general_url,investment_url,
				star_training_url,general_enabled,investment_enabled,business_capabilities_json,
				enabled,selected,status,session_state,login_state,sort_seq,updated_at)
			VALUES ($1,$2,$3,$4,$4,$4,$5,1,1,$6,1,1,'idle','closed','unknown',$7,$8)
			ON CONFLICT (agent_id) DO NOTHING`,
			a.AgentID, a.Name, a.Provider, a.HomeURL, starURL, string(caps), a.SortSeq, now); err != nil {
			return err
		}
	}
	if _, err := r.pool.Exec(ctx,
		"UPDATE ai_nexus_agents SET investment_url=home_url WHERE TRIM(investment_url)=''"); err != nil {
		return err
	}
	// Refill capabilities where stored empty/'[]'.
	for _, a := range defaultAgents {
		caps, _ := json.Marshal(a.Capabilities)
		if _, err := r.pool.Exec(ctx,
			"UPDATE ai_nexus_agents SET business_capabilities_json=$2 WHERE agent_id=$1 AND business_capabilities_json IN ('','[]')",
			a.AgentID, string(caps)); err != nil {
			return err
		}
	}
	for _, id := range retiredAgentIDs {
		if _, err := r.pool.Exec(ctx, "DELETE FROM ai_nexus_agents WHERE agent_id=$1", id); err != nil {
			return err
		}
	}
	// sort_seq backfill: seeds get 1..6 when 0; others 1000+row_number by updated_at.
	for _, a := range defaultAgents {
		if _, err := r.pool.Exec(ctx,
			"UPDATE ai_nexus_agents SET sort_seq=$2 WHERE agent_id=$1 AND sort_seq=0",
			a.AgentID, a.SortSeq); err != nil {
			return err
		}
	}
	if _, err := r.pool.Exec(ctx, `
		WITH numbered AS (
			SELECT agent_id, ROW_NUMBER() OVER (ORDER BY updated_at, agent_id) AS rn
			FROM ai_nexus_agents WHERE sort_seq=0
		)
		UPDATE ai_nexus_agents a SET sort_seq = 1000 + n.rn
		FROM numbered n WHERE a.agent_id = n.agent_id`); err != nil {
		return err
	}
	// Capability normalization: chatgpt gains comprehensive+orchestration;
	// every other agent has both stripped.
	rows, err := r.pool.Query(ctx,
		"SELECT agent_id, business_capabilities_json FROM ai_nexus_agents")
	if err != nil {
		return err
	}
	type capRow struct {
		id   string
		caps []string
	}
	var updates []capRow
	for rows.Next() {
		var id, raw string
		if err := rows.Scan(&id, &raw); err != nil {
			rows.Close()
			return err
		}
		var caps []string
		if json.Unmarshal([]byte(raw), &caps) != nil {
			caps = nil
		}
		var filtered []string
		for _, c := range caps {
			if id != "chatgpt" && (c == "comprehensive" || c == "orchestration") {
				continue
			}
			filtered = append(filtered, c)
		}
		if id == "chatgpt" {
			for _, extra := range []string{"comprehensive", "orchestration"} {
				found := false
				for _, c := range filtered {
					if c == extra {
						found = true
						break
					}
				}
				if !found {
					filtered = append(filtered, extra)
				}
			}
		}
		updates = append(updates, capRow{id, filtered})
	}
	rows.Close()
	for _, u := range updates {
		raw, _ := json.Marshal(u.caps)
		if _, err := r.pool.Exec(ctx,
			"UPDATE ai_nexus_agents SET business_capabilities_json=$2 WHERE agent_id=$1",
			u.id, string(raw)); err != nil {
			return err
		}
	}
	return nil
}

const agentColumns = `agent_id,name,provider,home_url,general_url,investment_url,
	star_training_url,general_enabled,investment_enabled,business_capabilities_json,
	enabled,selected,status,last_error,session_state,login_state,adapter_version,updated_at`

func scanAgent(row pgx.Row) (map[string]any, error) {
	var id, name, provider, home, general, investment, star, capsRaw string
	var ge, ie, enabled, selected int
	var status, lastError, sessionState, loginState, adapterVersion, updatedAt string
	if err := row.Scan(&id, &name, &provider, &home, &general, &investment, &star,
		&ge, &ie, &capsRaw, &enabled, &selected, &status, &lastError,
		&sessionState, &loginState, &adapterVersion, &updatedAt); err != nil {
		return nil, err
	}
	var caps []any
	if err := json.Unmarshal([]byte(capsRaw), &caps); err != nil || caps == nil {
		caps = []any{}
	}
	return map[string]any{
		"agent_id": id, "name": name, "provider": provider,
		"home_url": home, "general_url": general, "investment_url": investment,
		"star_training_url": star,
		"general_enabled":   ge != 0, "investment_enabled": ie != 0,
		"business_capabilities": caps,
		"enabled":               enabled != 0, "selected": selected != 0,
		"status": status, "last_error": lastError,
		"session_state": sessionState, "login_state": loginState,
		"adapter_version": adapterVersion, "updated_at": updatedAt,
	}, nil
}

// ListAgents returns agents ordered by sort_seq, agent_id.
func (r *PG) ListAgents(ctx context.Context) ([]map[string]any, error) {
	rows, err := r.pool.Query(ctx,
		"SELECT "+agentColumns+" FROM ai_nexus_agents ORDER BY sort_seq, agent_id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return collectAgents(rows)
}

// GetAgents returns agents for the given ids, reordered to input order,
// missing ids dropped.
func (r *PG) GetAgents(ctx context.Context, ids []string) ([]map[string]any, error) {
	if len(ids) == 0 {
		return nil, nil
	}
	rows, err := r.pool.Query(ctx,
		"SELECT "+agentColumns+" FROM ai_nexus_agents WHERE agent_id = ANY($1) ORDER BY sort_seq, agent_id",
		ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	found, err := collectAgents(rows)
	if err != nil {
		return nil, err
	}
	byID := map[string]map[string]any{}
	for _, a := range found {
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

// GetAgent returns one agent row or nil.
func (r *PG) GetAgent(ctx context.Context, agentID string) (map[string]any, error) {
	row := r.pool.QueryRow(ctx,
		"SELECT "+agentColumns+" FROM ai_nexus_agents WHERE agent_id=$1", agentID)
	a, err := scanAgent(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return a, err
}

func collectAgents(rows pgx.Rows) ([]map[string]any, error) {
	var out []map[string]any
	for rows.Next() {
		a, err := scanAgent(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// SaveAgentSelection clears selection then sets the listed ids.
func (r *PG) SaveAgentSelection(ctx context.Context, ids []string) error {
	if _, err := r.pool.Exec(ctx, "UPDATE ai_nexus_agents SET selected=0"); err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := r.pool.Exec(ctx,
			"UPDATE ai_nexus_agents SET selected=1, updated_at=$2 WHERE agent_id=$1",
			id, UtcNow()); err != nil {
			return err
		}
	}
	return nil
}

// MaxSortSeq returns the maximum sort_seq (0 when empty).
func (r *PG) MaxSortSeq(ctx context.Context) (int, error) {
	var n int
	err := r.pool.QueryRow(ctx, "SELECT COALESCE(MAX(sort_seq),0) FROM ai_nexus_agents").Scan(&n)
	return n, err
}

// AgentNameExists reports whether an agent with the name already exists.
func (r *PG) AgentNameExists(ctx context.Context, name string) (bool, error) {
	var n int
	err := r.pool.QueryRow(ctx, "SELECT COUNT(*) FROM ai_nexus_agents WHERE name=$1", name).Scan(&n)
	return n > 0, err
}

// AgentIDExists reports whether the agent_id is taken.
func (r *PG) AgentIDExists(ctx context.Context, id string) (bool, error) {
	var n int
	err := r.pool.QueryRow(ctx, "SELECT COUNT(*) FROM ai_nexus_agents WHERE agent_id=$1", id).Scan(&n)
	return n > 0, err
}

// AddAgent inserts a custom agent row.
func (r *PG) AddAgent(ctx context.Context, a map[string]any) error {
	caps, _ := json.Marshal(a["business_capabilities"])
	_, err := r.pool.Exec(ctx, `
		INSERT INTO ai_nexus_agents (agent_id,name,provider,home_url,general_url,investment_url,
			star_training_url,general_enabled,investment_enabled,business_capabilities_json,
			enabled,selected,status,sort_seq,updated_at)
		VALUES ($1,$2,$3,$4,$4,$4,'',1,1,$5,1,0,'idle',$6,$7)`,
		a["agent_id"], a["name"], a["provider"], a["home_url"],
		string(caps), a["sort_seq"], UtcNow())
	return err
}

// SaveAgentBusinessSettings updates business URLs/flags; home_url is
// overwritten with the general URL (parity with the retired mixin).
func (r *PG) SaveAgentBusinessSettings(ctx context.Context, agentID, generalURL, investmentURL, starTrainingURL string, generalEnabled, investmentEnabled bool, capabilities []string) error {
	caps, _ := json.Marshal(capabilities)
	_, err := r.pool.Exec(ctx, `
		UPDATE ai_nexus_agents SET home_url=$2, general_url=$2, investment_url=$3,
			star_training_url=$4, general_enabled=$5, investment_enabled=$6,
			business_capabilities_json=$7, updated_at=$8
		WHERE agent_id=$1`,
		agentID, generalURL, investmentURL, starTrainingURL,
		boolInt(generalEnabled), boolInt(investmentEnabled), string(caps), UtcNow())
	return err
}

// UpdateAgentStatus sets status + last_error.
func (r *PG) UpdateAgentStatus(ctx context.Context, agentID, status, lastError string) error {
	_, err := r.pool.Exec(ctx,
		"UPDATE ai_nexus_agents SET status=$2, last_error=$3, updated_at=$4 WHERE agent_id=$1",
		agentID, status, lastError, UtcNow())
	return err
}

// UpdateAgentRuntimeState dynamically updates session/login/adapter.
func (r *PG) UpdateAgentRuntimeState(ctx context.Context, agentID string, sessionState, loginState, adapterVersion *string) error {
	sets := []string{"updated_at=$2"}
	args := []any{agentID, UtcNow()}
	idx := 3
	if sessionState != nil {
		sets = append(sets, fmt.Sprintf("session_state=$%d", idx))
		args = append(args, *sessionState)
		idx++
	}
	if loginState != nil {
		sets = append(sets, fmt.Sprintf("login_state=$%d", idx))
		args = append(args, *loginState)
		idx++
	}
	if adapterVersion != nil {
		sets = append(sets, fmt.Sprintf("adapter_version=$%d", idx))
		args = append(args, *adapterVersion)
		idx++
	}
	_, err := r.pool.Exec(ctx,
		"UPDATE ai_nexus_agents SET "+strings.Join(sets, ",")+" WHERE agent_id=$1", args...)
	return err
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
