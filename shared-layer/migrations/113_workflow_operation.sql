-- 113_workflow_operation.sql
-- Cross-engine workflow (Saga) authority in PostgreSQL.
--
-- No distributed transactions: the operation row is the single source of
-- truth for multi-engine work (PostgreSQL + SQLite + Qdrant + NTFS), with
-- step state, leases for crash recovery, checkpoints, a transactional
-- outbox and an inbox for de-duplication.
--
-- Additive and idempotent; no runtime behaviour changes on apply.

CREATE SCHEMA IF NOT EXISTS gptbridge_workflow;

CREATE TABLE IF NOT EXISTS gptbridge_workflow.operation (
    operation_id text PRIMARY KEY,
    operation_type text NOT NULL,
    module_id text NOT NULL,
    resource_id text NOT NULL DEFAULT '',
    generation bigint NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'COMPENSATING', 'COMPLETED',
                          'FAILED', 'REQUIRES_RECONCILE', 'QUARANTINED')),
    current_step text NOT NULL DEFAULT '',
    idempotency_key text NOT NULL DEFAULT '',
    correlation_id text NOT NULL DEFAULT '',
    fingerprint text NOT NULL DEFAULT '',
    claimed_by text NOT NULL DEFAULT '',
    claimed_at timestamptz,
    lease_until timestamptz,
    worker_generation integer NOT NULL DEFAULT 0,
    checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS operation_idempotency_idx
    ON gptbridge_workflow.operation (idempotency_key)
    WHERE idempotency_key <> '';

CREATE UNIQUE INDEX IF NOT EXISTS operation_fingerprint_idx
    ON gptbridge_workflow.operation (module_id, operation_type, fingerprint)
    WHERE fingerprint <> '';

CREATE INDEX IF NOT EXISTS operation_claim_idx
    ON gptbridge_workflow.operation (status, lease_until, created_at);

CREATE TABLE IF NOT EXISTS gptbridge_workflow.operation_step (
    operation_id text NOT NULL REFERENCES gptbridge_workflow.operation(operation_id) ON DELETE CASCADE,
    step_id text NOT NULL,
    step_order integer NOT NULL,
    engine text NOT NULL CHECK (engine IN ('postgresql', 'sqlite', 'qdrant', 'filesystem', 'model')),
    action_type text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'TIMEOUT', 'UNKNOWN',
                          'COMPENSATED', 'INVALIDATED', 'SUPERSEDED', 'SKIPPED')),
    attempt_count integer NOT NULL DEFAULT 0,
    started_at timestamptz,
    completed_at timestamptz,
    result_hash text NOT NULL DEFAULT '',
    error_code text NOT NULL DEFAULT '',
    PRIMARY KEY (operation_id, step_id)
);

CREATE TABLE IF NOT EXISTS gptbridge_workflow.operation_event (
    event_id bigserial PRIMARY KEY,
    operation_id text NOT NULL,
    event_type text NOT NULL
        CHECK (event_type IN ('operation_created', 'step_started', 'step_completed', 'step_failed',
                              'compensation_started', 'compensation_completed',
                              'operation_completed', 'operation_quarantined')),
    step_id text NOT NULL DEFAULT '',
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS operation_event_idx
    ON gptbridge_workflow.operation_event (operation_id, created_at);

CREATE TABLE IF NOT EXISTS gptbridge_workflow.outbox_event (
    event_id text PRIMARY KEY,
    operation_id text NOT NULL DEFAULT '',
    step_id text NOT NULL DEFAULT '',
    engine text NOT NULL DEFAULT 'postgresql',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENT', 'FAILED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);

CREATE INDEX IF NOT EXISTS outbox_pending_idx
    ON gptbridge_workflow.outbox_event (status, created_at)
    WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS gptbridge_workflow.inbox_message (
    message_id text PRIMARY KEY,
    idempotency_key text NOT NULL,
    source_module text NOT NULL DEFAULT '',
    payload_hash text NOT NULL DEFAULT '',
    result jsonb,
    status text NOT NULL DEFAULT 'processed',
    processed_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS inbox_idempotency_idx
    ON gptbridge_workflow.inbox_message (idempotency_key);

CREATE OR REPLACE FUNCTION gptbridge_workflow.operation_fingerprint(
    p_operation_type text,
    p_module_id text,
    p_resource_id text,
    p_revision text,
    p_payload_hash text
)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE
AS $$
    SELECT encode(
        sha256(convert_to(
            concat_ws('|', p_operation_type, p_module_id, p_resource_id, p_revision, p_payload_hash),
            'UTF8')),
        'hex');
$$;

CREATE OR REPLACE FUNCTION gptbridge_workflow.claim_next_operation(
    p_worker text,
    p_lease_seconds integer DEFAULT 120
)
    RETURNS text
    LANGUAGE plpgsql
AS $$
DECLARE
    claimed text;
BEGIN
    IF p_worker IS NULL OR btrim(p_worker) = '' THEN
        RAISE EXCEPTION 'WORKER_ID_REQUIRED';
    END IF;
    SELECT operation_id INTO claimed
    FROM gptbridge_workflow.operation
    WHERE status IN ('PENDING', 'RUNNING', 'REQUIRES_RECONCILE')
      AND (lease_until IS NULL OR lease_until < now())
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED;
    IF claimed IS NULL THEN
        RETURN NULL;
    END IF;
    UPDATE gptbridge_workflow.operation
       SET status = 'RUNNING',
           claimed_by = p_worker,
           claimed_at = now(),
           lease_until = now() + make_interval(secs => p_lease_seconds),
           updated_at = now()
     WHERE operation_id = claimed;
    RETURN claimed;
END;
$$;

REVOKE ALL ON gptbridge_workflow.operation FROM PUBLIC;
REVOKE ALL ON gptbridge_workflow.operation_step FROM PUBLIC;
REVOKE ALL ON gptbridge_workflow.operation_event FROM PUBLIC;
REVOKE ALL ON gptbridge_workflow.outbox_event FROM PUBLIC;
REVOKE ALL ON gptbridge_workflow.inbox_message FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_owner') THEN
        EXECUTE 'GRANT ALL ON ALL TABLES IN SCHEMA gptbridge_workflow TO gptbridge_owner';
        EXECUTE 'GRANT ALL ON ALL SEQUENCES IN SCHEMA gptbridge_workflow TO gptbridge_owner';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_runtime') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA gptbridge_workflow TO gptbridge_runtime';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA gptbridge_workflow TO gptbridge_runtime';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_xingcheng_reader') THEN
        EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA gptbridge_workflow TO gptbridge_xingcheng_reader';
    END IF;
END $$;
