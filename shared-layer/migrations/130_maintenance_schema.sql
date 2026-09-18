-- Database Auto Maintenance v1 schema
-- Migration 130: maintenance_jobs, maintenance_leases, maintenance_decisions

-- Maintenance jobs table
CREATE TABLE IF NOT EXISTS gptbridge_maintenance.maintenance_jobs (
    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id TEXT NOT NULL,
    action_version INT NOT NULL DEFAULT 1,
    engine TEXT NOT NULL CHECK (engine IN ('postgresql', 'sqlite', 'reconcile', 'backup')),
    database_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    risk_class TEXT NOT NULL CHECK (risk_class IN ('M0_OBSERVE', 'M1_SAFE_AUTO', 'M2_GOVERNED_AUTO', 'M3_APPROVAL_REQUIRED')),
    priority INT NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('PLANNED', 'QUEUED', 'RUNNING', 'VERIFYING', 'SUCCEEDED', 'FAILED', 'DEFERRED', 'CANCELLED', 'QUARANTINED')),
    generation BIGINT NOT NULL DEFAULT 0,
    attempt_count INT NOT NULL DEFAULT 0,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    before_state JSONB NOT NULL DEFAULT '{}',
    after_state JSONB NOT NULL DEFAULT '{}',
    result_code TEXT,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_maintenance_jobs_status ON gptbridge_maintenance.maintenance_jobs (status);
CREATE INDEX IF NOT EXISTS idx_maintenance_jobs_engine ON gptbridge_maintenance.maintenance_jobs (engine);
CREATE INDEX IF NOT EXISTS idx_maintenance_jobs_generation ON gptbridge_maintenance.maintenance_jobs (generation);
CREATE INDEX IF NOT EXISTS idx_maintenance_jobs_scheduled ON gptbridge_maintenance.maintenance_jobs (scheduled_at);
CREATE INDEX IF NOT EXISTS idx_maintenance_jobs_module ON gptbridge_maintenance.maintenance_jobs (module_id);

-- Maintenance leases table
CREATE TABLE IF NOT EXISTS gptbridge_maintenance.maintenance_leases (
    scope TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    action_id TEXT NOT NULL,
    generation BIGINT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ NOT NULL,
    job_id UUID REFERENCES gptbridge_maintenance.maintenance_jobs(job_id),
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_maintenance_leases_holder ON gptbridge_maintenance.maintenance_leases (holder);
CREATE INDEX IF NOT EXISTS idx_maintenance_leases_expires ON gptbridge_maintenance.maintenance_leases (lease_until);

-- Maintenance decisions log (M2/M3, integrity failures, security-related, recovery transitions)
CREATE TABLE IF NOT EXISTS gptbridge_maintenance.maintenance_decisions (
    decision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id TEXT NOT NULL,
    rule_version INT NOT NULL DEFAULT 1,
    observed_state JSONB NOT NULL DEFAULT '{}',
    selected_action TEXT,
    reason_code TEXT NOT NULL,
    generation BIGINT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('ALLOWED', 'DENIED', 'DEFERRED', 'CANDIDATE_ONLY')),
    job_id UUID REFERENCES gptbridge_maintenance.maintenance_jobs(job_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_maintenance_decisions_generation ON gptbridge_maintenance.maintenance_decisions (generation);
CREATE INDEX IF NOT EXISTS idx_maintenance_decisions_reason ON gptbridge_maintenance.maintenance_decisions (reason_code);
CREATE INDEX IF NOT EXISTS idx_maintenance_decisions_job ON gptbridge_maintenance.maintenance_decisions (job_id);

-- Maintenance budget state (for persistence across restarts)
CREATE TABLE IF NOT EXISTS gptbridge_maintenance.maintenance_budget_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trigger for updated_at
CREATE OR REPLACE FUNCTION gptbridge_maintenance.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_maintenance_jobs_updated ON gptbridge_maintenance.maintenance_jobs;
CREATE TRIGGER trg_maintenance_jobs_updated
    BEFORE UPDATE ON gptbridge_maintenance.maintenance_jobs
    FOR EACH ROW EXECUTE FUNCTION gptbridge_maintenance.set_updated_at();

DROP TRIGGER IF EXISTS trg_maintenance_leases_updated ON gptbridge_maintenance.maintenance_leases;
CREATE TRIGGER trg_maintenance_leases_updated
    BEFORE UPDATE ON gptbridge_maintenance.maintenance_leases
    FOR EACH ROW EXECUTE FUNCTION gptbridge_maintenance.set_updated_at();

-- Grant permissions
GRANT SELECT, INSERT, UPDATE ON gptbridge_maintenance.maintenance_jobs TO gptbridge_runtime;
GRANT SELECT, INSERT, UPDATE ON gptbridge_maintenance.maintenance_leases TO gptbridge_runtime;
GRANT SELECT, INSERT ON gptbridge_maintenance.maintenance_decisions TO gptbridge_runtime;
GRANT SELECT, UPDATE ON gptbridge_maintenance.maintenance_budget_state TO gptbridge_runtime;