/**
 * Runtime status type definitions extracted from SovereignDashboard to break
 * the circular dependency between SovereignDashboard.tsx and
 * shared/hooks/useRuntimeStatusField.ts.
 */

interface CodexSnapshot {
  authority?: unknown
  rule_layer?: unknown
  codex_schema?: unknown
  codex_version?: unknown
  authority_rank?: unknown
  binding_scope?: unknown
  function?: unknown
  mutability?: unknown
  amendment?: unknown
  interpretation?: unknown
  sections?: Array<Record<string, unknown>>
  principles?: Array<Record<string, unknown>>
  articles?: Array<Record<string, unknown>>
  edicts?: Array<Record<string, unknown>>
  active_rule?: unknown
}

interface XingchengSnapshot {
  module_id?: unknown
  rank?: unknown
  kind?: unknown
  mode?: unknown
  authority?: Record<string, unknown>
  powers?: { empowered?: unknown; prohibited?: unknown }
}

export interface SovereignSnapshot {
  owned_by?: unknown
  dependency_state?: unknown
  executor?: unknown
  health_owner?: unknown
  started_at?: unknown
  sub_sovereigns?: Array<Record<string, unknown>>
  peer_systems?: { xingcheng?: XingchengSnapshot }
  governance_rules?: CodexSnapshot
  permission?: Record<string, unknown>
}

export interface PendingActionApproval {
  action_id?: string
  kind?: string
  summary?: string
  detail?: Record<string, unknown>
  status?: string
  created_at?: string
  updated_at?: string
  fault_id?: string
  update_id?: string
  scope?: string
  target?: string
  proposed_method?: string
  repair_plan?: Record<string, unknown>
  repair_requirements?: Record<string, unknown>
  risk?: string
  rollback?: string
  expires_at?: string
  evidence_digest?: string
}

export interface AutomationSwitches {
  automatic_repair_enabled?: boolean
  automatic_repair_managed_by?: string
  automatic_update_enabled?: boolean
  updated_at?: string
  updated_by?: string
}

export interface GlobalFault {
  fault_id?: string
  fault_type?: string
  source?: string
  timestamp?: string
  severity?: string
  error_class?: string
  error_message?: string
  target_entity?: string
  repair_action?: string
  repair_outcome?: string
  raw_evidence?: Record<string, unknown>
}

export interface GlobalFaultPattern {
  pattern_id?: string
  error_class?: string
  occurrence_count?: number
  affected_entities?: string[]
  common_repair_action?: string
  success_rate?: number
  severity_trend?: string
  last_seen?: string
}

export interface GlobalFaults {
  tracked?: number
  unresolved?: number
  quarantined?: number
  severity_distribution?: Record<string, number>
  source_distribution?: Record<string, number>
  top_patterns?: GlobalFaultPattern[]
  recent_faults?: GlobalFault[]
  generated_at?: string
}

export interface PendingActionCardinality {
  mode?: string
  unresolved?: number
  fault_count?: number
  update_count?: number
  total_actionable?: number
}

export interface RuntimeStatusPayload {
  maintenance_ready?: boolean
  decision_sovereign?: SovereignSnapshot
  pending_actions?: PendingActionApproval[]
  pending_action_count?: number
  automation_switches?: AutomationSwitches
  pending_action_cardinality?: PendingActionCardinality
  authority_reanchor?: Record<string, unknown>
  automation_modules?: Array<Record<string, unknown>>
  global_faults?: GlobalFaults
  xingcheng_native_model_runtime?: {
    model_id?: string
    state?: 'running' | 'stopped' | 'unavailable'
    running?: boolean
    available?: boolean
    checked_at?: string
    message?: string
  }
}
