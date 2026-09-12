import { useMemo } from 'react'
import { mainSystemLocale } from '@/locales/main-system'
import './sovereign.css'

const t = mainSystemLocale.sovereign

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

interface SovereignSnapshot {
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

export interface RuntimeStatusPayload {
  maintenance_ready?: boolean
  decision_sovereign?: SovereignSnapshot
}

const ROLE_LABELS: Record<string, string> = {
  'runtime-sovereign': 'roleRuntime',
  'health-maintenance-test-sub-sovereign': 'roleMaintenance',
  'resource-dependency-sync-sub-sovereign': 'roleResource',
  'data-governance-sub-sovereign': 'roleData',
  'channel-contract-sync-sub-sovereign': 'roleIntegration',
  'permission-sovereign': 'rolePermission',
}

const ROLE_SCOPES: Record<string, string> = {
  'runtime-sovereign': 'scopeRuntime',
  'health-maintenance-test-sub-sovereign': 'scopeMaintenance',
  'resource-dependency-sync-sub-sovereign': 'scopeResource',
  'data-governance-sub-sovereign': 'scopeData',
  'channel-contract-sync-sub-sovereign': 'scopeIntegration',
  'permission-sovereign': 'scopePermission',
}

function asString(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number') return String(value)
  return fallback
}

function asBoolean(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null
}

interface SovereignEntry {
  role: string
  label: string
  state: string
  scope: string
}

function sovereignEntries(
  rawList: unknown,
  permission: Record<string, unknown> | undefined
): SovereignEntry[] {
  const rows: Array<Record<string, unknown>> = Array.isArray(rawList)
    ? (rawList as Array<Record<string, unknown>>)
    : []

  const permissionEntry =
    permission && Object.keys(permission).length > 0
      ? {
          role: 'permission-sovereign',
          row: permission,
          hasStarted: null,
          stateOverride: asString(permission['state']) || t.governing,
        }
      : null

  const entries: Array<{
    role: string
    row: Record<string, unknown>
    hasStarted: boolean | null
    stateOverride?: string
  }> = rows.map((row) => ({
    role: asString(row['role']),
    row,
    hasStarted: asBoolean(row['started']),
  }))
  if (permissionEntry) entries.push(permissionEntry)

  return entries.map((entry) => {
    let state: string
    if (entry.stateOverride) {
      state = entry.stateOverride
    } else if (entry.hasStarted === true) {
      state = t.running
    } else if (entry.hasStarted === false) {
      state = t.stopped
    } else {
      state = asString(entry.row['state']) || t.stateUnknown
    }

    const scopedLabel = asString(entry.row['scope'])
    const scope =
      asString(t[ROLE_SCOPES[entry.role] as keyof typeof t]) || scopedLabel || entry.role

    return {
      role: entry.role,
      label:
        asString(t[ROLE_LABELS[entry.role] as keyof typeof t]) || entry.role,
      state,
      scope,
    }
  })
}

function dependencyTone(state: string): 'success' | 'warning' | 'danger' | 'muted' {
  const normalized = state.toUpperCase()
  if (normalized === 'READY') return 'success'
  if (normalized === 'DEGRADED' || normalized === 'RECOVERING') return 'warning'
  if (normalized === 'FAILED') return 'danger'
  return 'muted'
}

function formatCount(list: unknown): string {
  const count = Array.isArray(list) ? list.length : 0
  return new Intl.NumberFormat('zh-TW').format(count)
}

function powerLabel(value: unknown): string {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    return (
      asString(record['id']) ||
      asString(record['statement']) ||
      asString(record['edict']) ||
      '—'
    )
  }
  return '—'
}

export function SovereignDashboard({
  runtimeStatus,
}: {
  runtimeStatus: RuntimeStatusPayload
}) {
  const sovereign = runtimeStatus.decision_sovereign
  const codex = sovereign?.governance_rules
  const permission = sovereign?.permission
  const xingcheng = sovereign?.peer_systems?.xingcheng

  const dependencyState = asString(sovereign?.dependency_state, 'UNKNOWN')
  const executor = asString(sovereign?.executor, t.governedExecutorOnly)
  const healthOwner = asString(sovereign?.health_owner, 'maintenance-sovereign')
  const ownedBy = asString(sovereign?.owned_by, '')
  const startedAt = asString(sovereign?.started_at, '')

  const subSovereigns = useMemo(
    () => sovereignEntries(sovereign?.sub_sovereigns, permission),
    [sovereign, permission]
  )

  const xingchengPowers = Array.isArray(xingcheng?.powers?.empowered)
    ? (xingcheng?.powers?.empowered as unknown[])
    : []
  const xingchengProhibited = Array.isArray(xingcheng?.powers?.prohibited)
    ? (xingcheng?.powers?.prohibited as unknown[])
    : []

  const xingchengAuthority = xingcheng?.authority ?? {}
  const xingchengExecutionDenied = asBoolean(xingchengAuthority['execution']) === false

  return (
    <section className="sovereign-panel" aria-labelledby="sovereign-title">
      <header className="sovereign-header">
        <div>
          <span className="eyebrow">{t.eyebrow}</span>
          <h2 id="sovereign-title">{t.title}</h2>
          <p>{t.description}</p>
        </div>
        <div
          className="dependency-badge"
          data-tone={dependencyTone(dependencyState)}
          data-testid="sovereign-dependency-state"
        >
          <span className="dependency-badge__dot" />
          <span>
            <strong>{t.dependencyState}</strong>
            <small>{dependencyState}</small>
          </span>
        </div>
      </header>

      <div className="sovereign-meta">
        <div className="sovereign-meta__item">
          <span>{t.executor}</span>
          <strong>{executor}</strong>
        </div>
        <div className="sovereign-meta__item">
          <span>{t.healthOwner}</span>
          <strong>{healthOwner}</strong>
        </div>
        {ownedBy && (
          <div className="sovereign-meta__item">
            <span>{t.decisionSovereignOwnedBy}</span>
            <strong>{ownedBy}</strong>
          </div>
        )}
        {startedAt && (
          <div className="sovereign-meta__item">
            <span>{t.startedAt}</span>
            <strong className="sovereign-meta__time">
              {new Date(startedAt).toLocaleString('zh-TW', {
                hour12: false,
              })}
            </strong>
          </div>
        )}
      </div>

      <div className="sovereign-grid">
        <div className="sovereign-card sovereign-card--wide">
          <div className="sovereign-card__header">
            <span className="eyebrow">{t.codexTitle}</span>
            <strong className="sovereign-card__codex-version">
              {asString(codex?.codex_schema, 'codex')} v
              {asString(codex?.codex_version, '1')}
            </strong>
          </div>
          <dl className="codex-facts">
            <div>
              <dt>{t.authority}</dt>
              <dd>{asString(codex?.authority_rank, t.supreme)}</dd>
            </div>
            <div>
              <dt>{t.bindingScope}</dt>
              <dd>{asString(codex?.binding_scope, t.scopeDefault)}</dd>
            </div>
            <div>
              <dt>{t.function}</dt>
              <dd>{asString(codex?.function, t.functionNone)}</dd>
            </div>
            <div>
              <dt>{t.mutability}</dt>
              <dd>{asString(codex?.mutability, t.immutableSealed)}</dd>
            </div>
            <div>
              <dt>{t.amendment}</dt>
              <dd>{asString(codex?.amendment, t.fullVersionedReplacement)}</dd>
            </div>
            <div>
              <dt>{t.interpretation}</dt>
              <dd>{asString(codex?.interpretation, t.selfInterpreter)}</dd>
            </div>
          </dl>
          <footer className="codex-provisions">
            <span>{t.provisionsTitle}</span>
            <ul>
              <li>
                <strong>{formatCount(codex?.sections)}</strong>
                <small>{t.sections}</small>
              </li>
              <li>
                <strong>{formatCount(codex?.principles)}</strong>
                <small>{t.principles}</small>
              </li>
              <li>
                <strong>{formatCount(codex?.articles)}</strong>
                <small>{t.articles}</small>
              </li>
              <li>
                <strong>{formatCount(codex?.edicts)}</strong>
                <small>{t.edicts}</small>
              </li>
            </ul>
          </footer>
        </div>

        <div className="sovereign-card">
          <div className="sovereign-card__header">
            <span className="eyebrow">{t.xingchengRole}</span>
            <strong>{t.xingchengTitle}</strong>
          </div>
          <dl className="codex-facts">
            <div>
              <dt>{t.xingchengRole}</dt>
              <dd>{asString(xingcheng?.rank, t.xingchengPeer)}</dd>
            </div>
            <div>
              <dt>{t.xingchengKind}</dt>
              <dd>{asString(xingcheng?.kind, t.xingchengLocalNativeModel)}</dd>
            </div>
            <div>
              <dt>{t.xingchengMode}</dt>
              <dd>{asString(xingcheng?.mode, t.xingchengIntelligentManagement)}</dd>
            </div>
          </dl>
          <div className="xingcheng-powers">
            <div>
              <span>{t.empoweredPowers}</span>
              <ul>
                {xingchengPowers.map((power) => (
                  <li key={powerLabel(power)}>{powerLabel(power)}</li>
                ))}
                {xingchengPowers.length === 0 && (
                  <li className="xingcheng-powers__empty">—</li>
                )}
              </ul>
            </div>
            <div className="xingcheng-powers--prohibited">
              <span>{t.prohibitedPowers}</span>
              <ul>
                {xingchengProhibited.map((power) => (
                  <li key={powerLabel(power)}>{powerLabel(power)}</li>
                ))}
                {xingchengProhibited.length === 0 && (
                  <li className="xingcheng-powers__empty">—</li>
                )}
              </ul>
            </div>
          </div>
          <footer className="xingcheng-constraints">
            {xingchengExecutionDenied && <span>{t.noExecution}</span>}
          </footer>
        </div>
      </div>

      <header className="sovereign-sub-title">
        <div>
          <span className="eyebrow">{t.subSovereignsTitle}</span>
          <h3>{t.subSovereignsTitle}</h3>
          <p>{t.subSovereignsHint}</p>
        </div>
      </header>
      <div className="sovereign-sub-grid">
        {subSovereigns.map((sub) => (
          <div
            key={sub.role}
            className="sovereign-sub-card"
            data-state={
              sub.state === t.running || sub.state === t.governing ? 'on' : 'off'
            }
          >
            <div className="sovereign-sub-card__top">
              <span className="sovereign-sub-card__dot" />
              <strong>{sub.label}</strong>
            </div>
            <small>{sub.state}</small>
            <p>{sub.scope}</p>
          </div>
        ))}
        {subSovereigns.length === 0 && (
          <div className="sovereign-sub-card" data-state="off">
            <div className="sovereign-sub-card__top">
              <span className="sovereign-sub-card__dot" />
              <strong>{t.stateUnknown}</strong>
            </div>
            <small>{t.stateUnknown}</small>
            <p>—</p>
          </div>
        )}
      </div>
    </section>
  )
}