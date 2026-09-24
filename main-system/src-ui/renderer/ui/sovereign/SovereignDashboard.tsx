import { mainSystemLocale } from '@/locales/main-system'
import './sovereign.css'
import type {
  PendingActionApproval,
  AutomationSwitches,
  GlobalFault,
  GlobalFaultPattern,
  GlobalFaults,
  PendingActionCardinality,
  RuntimeStatusPayload,
  SovereignSnapshot,
} from './runtimeStatusTypes'

// Re-export for backward compatibility with existing imports.
export type {
  PendingActionApproval,
  AutomationSwitches,
  GlobalFault,
  GlobalFaultPattern,
  GlobalFaults,
  PendingActionCardinality,
  RuntimeStatusPayload,
  SovereignSnapshot,
}

const t = mainSystemLocale.sovereign

import { useRuntimeStatusField } from '@/shared/hooks/useRuntimeStatusField'

function asString(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number') return String(value)
  return fallback
}

function asBoolean(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null
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
  runtimeStatus?: RuntimeStatusPayload
}) {
  // Modular subscription: this dashboard updates only when the decision
  // sovereign snapshot changes; an error here is contained by the module
  // boundary and cannot stall the rest of the UI.
  const subscribedSovereign = useRuntimeStatusField('decision_sovereign')
  const sovereign =
    subscribedSovereign ?? runtimeStatus?.decision_sovereign
  const codex = sovereign?.governance_rules
  const xingcheng = sovereign?.peer_systems?.xingcheng

  const dependencyState = asString(sovereign?.dependency_state, 'UNKNOWN')
  const executor = asString(sovereign?.executor, t.governedExecutorOnly)
  const healthOwner = asString(sovereign?.health_owner, '—')
  const ownedBy = asString(sovereign?.owned_by, '')
  const startedAt = asString(sovereign?.started_at, '')

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
              {codex?.codex_schema && codex?.codex_version
                ? `${asString(codex.codex_schema)} v${asString(codex.codex_version)}`
                : '—'}
            </strong>
          </div>
          <dl className="codex-facts">
            <div>
              <dt>{t.authority}</dt>
              <dd>{asString(codex?.authority_rank, t.supreme)}</dd>
            </div>
            <div>
              <dt>{t.authoritySource}</dt>
              <dd>{asString(codex?.authority_source, '—')}</dd>
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

    </section>
  )
}
