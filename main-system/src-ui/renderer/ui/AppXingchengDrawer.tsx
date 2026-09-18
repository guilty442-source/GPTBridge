import { useState } from 'react'
import type {
  GlobalFault,
  GlobalFaults,
  PendingActionApproval,
} from '@/ui/sovereign/runtimeStatusTypes'
import { Drawer } from '@/ui/drawer/Drawer'
import { useRuntimeStatusField } from '@/shared/hooks/useRuntimeStatusField'
import {
  filterActiveFaults,
  filterActivePendingActions,
} from '@/shared/utils/faultPresentation'
import { mainSystemLocale } from '@/locales/main-system'

const xr = mainSystemLocale.xingchengReport

type XingchengReview = {
  tone: 'ok' | 'warning'
  state: string
  detail: string
  issues: Array<{
    id: string
    source: string
    title: string
    detail: string
    status: string
  }>
}

type Cardinality = {
  mode?: string
  unresolved?: number
}

type AutomationSwitches = {
  automatic_repair_enabled?: boolean
  automatic_update_enabled?: boolean
}

type SendCommand = (
  command: string,
  payload?: unknown
) => { ok: boolean; queued: boolean; message?: string }

type WaitForIpcEvent = (
  eventName: string,
  timeoutMs: number,
  predicate?: (payload: Record<string, unknown>) => boolean
) => Promise<Record<string, unknown>>

export type XingchengDrawerProps = {
  open: boolean
  onClose: () => void
  review: XingchengReview
  confirmBusyId: string | null
  confirmMessages: Record<string, string>
  switchBusy: string | null
  onConfirm: (actionId: string) => void
  onDeny: (actionId: string) => void
  onSwitch: (switchName: string, enabled: boolean) => void
  onNativeModelSwitch: (enabled: boolean) => void
  sendCommand: SendCommand
  waitForIpcEvent: WaitForIpcEvent
}

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low', 'info'] as const

function severitySummary(faults: GlobalFault[]): Array<[string, number]> {
  const distribution: Record<string, number> = {}
  for (const fault of faults) {
    const severity = String(fault.severity || 'info')
    distribution[severity] = (distribution[severity] || 0) + 1
  }
  return SEVERITY_ORDER.map(
    (severity) => [severity, Number(distribution[severity] || 0)] as [string, number]
  ).filter(([, count]) => count > 0)
}

function sourceCount(faults: GlobalFault[]): number {
  return new Set(
    faults
      .map((fault) => String(fault.source || '').trim())
      .filter((source) => source.length > 0)
  ).size
}

function faultTime(value: unknown): string {
  const raw = String(value || '')
  if (!raw) return ''
  const parsed = Date.parse(raw)
  return Number.isFinite(parsed)
    ? new Date(parsed).toLocaleString('zh-TW', { hour12: false })
    : raw
}

function actionExpired(action: PendingActionApproval): boolean {
  const raw = String(action.expires_at || '')
  if (!raw) return false
  const parsed = Date.parse(raw)
  return Number.isFinite(parsed) ? Date.now() > parsed : true
}

function actionSeverityRank(action: PendingActionApproval): number {
  const numeric = Number(action.risk)
  if (Number.isFinite(numeric) && numeric > 0) return numeric
  const risk = String(action.risk || '').toLowerCase()
  if (risk.includes('critical') || risk.includes('high')) return 4
  if (risk.includes('medium')) return 3
  if (risk.includes('low')) return 2
  return 1
}

export function orderPendingActions(
  actions: PendingActionApproval[]
): PendingActionApproval[] {
  return [...actions].sort((a, b) => {
    const severity = actionSeverityRank(b) - actionSeverityRank(a)
    if (severity !== 0) return severity
    const aTime = Date.parse(String(a.created_at || '')) || 0
    const bTime = Date.parse(String(b.created_at || '')) || 0
    if (aTime !== bTime) return aTime - bTime
    return String(a.fault_id || a.action_id || '').localeCompare(
      String(b.fault_id || b.action_id || '')
    )
  })
}

export function XingchengDrawer({
  open,
  onClose,
  review,
  confirmBusyId,
  confirmMessages,
  switchBusy,
  onConfirm,
  onDeny,
  onSwitch,
  onNativeModelSwitch,
  sendCommand,
  waitForIpcEvent,
}: XingchengDrawerProps) {
  // Modular subscriptions: this drawer re-renders only when its own fields
  // change; a failure here is contained by the module boundary upstream.
  const pendingActions = useRuntimeStatusField('pending_actions') ?? []
  const switches = useRuntimeStatusField('automation_switches') ?? {}
  const cardinality = useRuntimeStatusField('pending_action_cardinality') ?? {}
  const nativeModel = useRuntimeStatusField('xingcheng_native_model_runtime')
  const faults = useRuntimeStatusField('global_faults') as
    | GlobalFaults
    | undefined
  const [faultBusy, setFaultBusy] = useState(false)
  const [faultMessage, setFaultMessage] = useState('')
  const [faultDetail, setFaultDetail] = useState<GlobalFault | null>(null)
  const repairSwitchOn = switches.automatic_repair_enabled === true
  const updateSwitchOn = switches.automatic_update_enabled === true
  const activeActions = filterActivePendingActions(pendingActions)
  const recentFaults = filterActiveFaults(faults?.recent_faults)
  const unresolvedFaults =
    typeof faults?.unresolved === 'number'
      ? faults.unresolved
      : recentFaults.length
  const ordered = orderPendingActions(activeActions)
  const cardinalityLabel =
    cardinality.mode === 'MULTI_FAULT'
      ? xr.cardinalityMulti
      : cardinality.mode === 'SINGLE_FAULT'
        ? xr.cardinalitySingle
        : xr.cardinalityNoFault

  const refreshGlobalFaults = async () => {
    setFaultBusy(true)
    setFaultMessage('')
    try {
      const sent = sendCommand('app:get-fault-analysis', {
        query: 'overview',
        requester: 'ui-xingcheng-drawer',
      })
      if (!sent.ok) {
        setFaultMessage(sent.message || xr.globalFaultsFailed)
        return
      }
      const result = await waitForIpcEvent(
        'app:get-fault-analysis_result',
        15000
      )
      if (result.ok !== true) {
        setFaultMessage(String(result.message || '') || xr.globalFaultsFailed)
        return
      }
      sendCommand('app:get-runtime-status', {
        source: 'xingcheng_fault_refresh',
      })
    } catch {
      setFaultMessage(xr.globalFaultsFailed)
    } finally {
      setFaultBusy(false)
    }
  }

  const openFaultDetail = (fault: GlobalFault) => {
    setFaultMessage('')
    setFaultDetail(fault)
  }
  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={xr.title}
      eyebrow={xr.eyebrow}
      icon={mainSystemLocale.app.star}
    >
      <div className="xingcheng-report" data-testid="xingcheng-report-detail">
        <section className="xingcheng-report__summary" data-tone={review.tone}>
          <span>{xr.currentDecision}</span>
          <strong>{review.state}</strong>
          <p>{review.detail}</p>
        </section>
        {review.issues.length > 0 ? (
          <div className="xingcheng-report__issues">
            {review.issues.map((issue) => (
              <article className="xingcheng-issue" key={issue.id}>
                <div className="xingcheng-issue__head">
                  <strong>{issue.title}</strong>
                  <span>{issue.status}</span>
                </div>
                <dl>
                  <div><dt>{xr.source}</dt><dd>{issue.source}</dd></div>
                  <div><dt>{xr.details}</dt><dd>{issue.detail}</dd></div>
                </dl>
              </article>
            ))}
          </div>
        ) : (
          <p className="xingcheng-report__empty">{xr.empty}</p>
        )}
      </div>

      <section className="xingcheng-approvals" data-testid="global-faults">
        <div className="xingcheng-approvals__head">
          <strong>{xr.globalFaultsTitle}</strong>
          <button
            type="button"
            className="button button--ghost"
            data-testid="refresh-global-faults"
            disabled={faultBusy}
            onClick={() => void refreshGlobalFaults()}
          >
            {faultBusy ? xr.globalFaultsLoading : xr.globalFaultsRefresh}
          </button>
        </div>
        <p className="xingcheng-report__empty">{xr.globalFaultsHint}</p>
        {!faults || (faults.tracked || 0) === 0 ? (
          <p className="xingcheng-report__empty">{xr.globalFaultsEmpty}</p>
        ) : (
          <>
            <div className="xingcheng-switches">
              <div className="xingcheng-switch">
                <span className="xingcheng-switch__label">
                  {xr.globalFaultsTracked}
                </span>
                <strong>{faults.tracked || 0}</strong>
              </div>
              <div className="xingcheng-switch">
                <span className="xingcheng-switch__label">
                  {xr.globalFaultsUnresolved}
                </span>
                <strong>{unresolvedFaults}</strong>
              </div>
              <div className="xingcheng-switch">
                <span className="xingcheng-switch__label">
                  {xr.globalFaultsSources}
                </span>
                <strong>{sourceCount(recentFaults)}</strong>
              </div>
            </div>
            {severitySummary(recentFaults).length > 0 && (
              <p className="xingcheng-approval__message">
                {xr.globalFaultsSeverity}：
                {severitySummary(recentFaults)
                  .map(([severity, count]) => `${severity} ${count}`)
                  .join('、')}
              </p>
            )}
            {recentFaults.length > 0 && (
              <div className="xingcheng-approvals__list">
                <div className="xingcheng-approvals__head">
                  <strong>{xr.globalFaultsRecent}</strong>
                </div>
                {recentFaults.map((fault, index) => (
                  <article
                    className="xingcheng-approval"
                    key={String(fault.fault_id || `fault-${index}`)}
                  >
                    <div className="xingcheng-approval__head">
                      <span className="xingcheng-approval__kind">
                        {fault.severity || 'info'}
                      </span>
                      <strong>{fault.error_class || fault.fault_type || '—'}</strong>
                    </div>
                    <dl className="xingcheng-approval__detail">
                      <div>
                        <dt>{xr.source}</dt>
                        <dd>{fault.source || '—'}</dd>
                      </div>
                      <div>
                        <dt>{xr.globalFaultsTarget}</dt>
                        <dd>{fault.target_entity || '—'}</dd>
                      </div>
                      <div>
                        <dt>{xr.globalFaultsOutcome}</dt>
                        <dd>{fault.repair_outcome || 'none'}</dd>
                      </div>
                      <div>
                        <dt>{xr.globalFaultsLastSeen}</dt>
                        <dd>{faultTime(fault.timestamp)}</dd>
                      </div>
                    </dl>
                    <button
                      type="button"
                      className="button button--ghost"
                      onClick={() => openFaultDetail(fault)}
                    >
                      {xr.viewDetails}
                    </button>
                  </article>
                ))}
              </div>
            )}
            {(faults.top_patterns || []).length > 0 && (
              <div className="xingcheng-approvals__list">
                <div className="xingcheng-approvals__head">
                  <strong>{xr.globalFaultsPatterns}</strong>
                </div>
                {(faults.top_patterns || []).map((pattern) => (
                  <article
                    className="xingcheng-approval"
                    key={String(pattern.pattern_id || pattern.error_class || '')}
                  >
                    <div className="xingcheng-approval__head">
                      <span className="xingcheng-approval__kind">
                        {pattern.severity_trend || 'unknown'}
                      </span>
                      <strong>{pattern.error_class || '—'}</strong>
                    </div>
                    <dl className="xingcheng-approval__detail">
                      <div>
                        <dt>{xr.globalFaultsOccurrences}</dt>
                        <dd>{pattern.occurrence_count || 0}</dd>
                      </div>
                      <div>
                        <dt>{xr.globalFaultsSuccessRate}</dt>
                        <dd>
                          {typeof pattern.success_rate === 'number'
                            ? `${Math.round(pattern.success_rate * 100)}%`
                            : '—'}
                        </dd>
                      </div>
                      <div>
                        <dt>{xr.globalFaultsAffected}</dt>
                        <dd>{(pattern.affected_entities || []).join('、') || '—'}</dd>
                      </div>
                      <div>
                        <dt>{xr.globalFaultsLastSeen}</dt>
                        <dd>{faultTime(pattern.last_seen)}</dd>
                      </div>
                    </dl>
                  </article>
                ))}
              </div>
            )}
          </>
        )}
        {faultDetail && (
          <article className="xingcheng-approval" data-testid="global-fault-detail">
            <div className="xingcheng-approval__head">
              <span className="xingcheng-approval__kind">
                {faultDetail.severity || 'info'}
              </span>
              <strong>{faultDetail.fault_id || faultDetail.error_class || '—'}</strong>
            </div>
            <dl className="xingcheng-approval__detail">
              <div><dt>{xr.globalFaultsClass}</dt><dd>{faultDetail.error_class || '—'}</dd></div>
              <div><dt>{xr.globalFaultsMessage}</dt><dd>{faultDetail.error_message || '—'}</dd></div>
              <div><dt>{xr.globalFaultsAction}</dt><dd>{faultDetail.repair_action || '—'}</dd></div>
              <div><dt>{xr.globalFaultsOutcome}</dt><dd>{faultDetail.repair_outcome || 'none'}</dd></div>
              <div><dt>{xr.globalFaultsLastSeen}</dt><dd>{faultTime(faultDetail.timestamp)}</dd></div>
            </dl>
            <button
              type="button"
              className="button button--ghost"
              onClick={() => setFaultDetail(null)}
            >
              {xr.globalFaultsClose}
            </button>
          </article>
        )}
        {faultMessage ? (
          <p className="xingcheng-approval__message">{faultMessage}</p>
        ) : null}
      </section>

      <section className="xingcheng-approvals" data-testid="pending-approvals">
        <div className="xingcheng-approvals__head">
          <strong>{xr.switchesTitle}</strong>
          <span>{xr.switchAttribution}</span>
        </div>
        <div className="xingcheng-switches">
          <div className="xingcheng-switch">
            <span className="xingcheng-switch__label">
              {xr.nativeModelSwitch}
            </span>
            <button
              type="button"
              className="xingcheng-switch__toggle"
              data-tone={nativeModel?.running ? 'on' : 'off'}
              data-testid="switch-xingcheng-native-model"
              disabled={switchBusy === 'xingcheng_native_model' || nativeModel?.available === false}
              onClick={() => void onNativeModelSwitch(nativeModel?.running !== true)}
            >
              {switchBusy === 'xingcheng_native_model'
                ? xr.nativeModelChanging
                : nativeModel?.running ? xr.switchOn : xr.switchOff}
            </button>
          </div>
          {(
            [
              ['automatic_repair_enabled', xr.switchRepair, repairSwitchOn, true],
              ['automatic_update_enabled', xr.switchUpdate, updateSwitchOn, false],
            ] as Array<[string, string, boolean, boolean]>
          ).map(([switchName, label, enabled, managed]) => (
            <div className="xingcheng-switch" key={switchName}>
              <span className="xingcheng-switch__label">
                {label}
                {managed ? ` · ${xr.switchManaged}` : ''}
              </span>
              <button
                type="button"
                className="xingcheng-switch__toggle"
                data-tone={enabled ? 'on' : 'off'}
                data-testid={`switch-${switchName}`}
                disabled={managed || switchBusy === switchName}
                onClick={managed ? undefined : () => void onSwitch(switchName, !enabled)}
              >
                {enabled ? xr.switchOn : xr.switchOff}
              </button>
            </div>
          ))}
        </div>
        {confirmMessages.xingcheng_native_model ? (
          <p className="xingcheng-approval__message">{confirmMessages.xingcheng_native_model}</p>
        ) : null}

        <div className="xingcheng-approvals__head">
          <strong>{xr.pendingTitle}</strong>
          <span>
            {cardinalityLabel}
            {typeof cardinality.unresolved === 'number'
              ? ` · ${xr.cardinalityCounts.replace(
                  '{count}',
                  String(cardinality.unresolved)
                )}`
              : ''}
          </span>
        </div>
        {ordered.length === 0 ? (
          <p className="xingcheng-report__empty">{xr.pendingEmpty}</p>
        ) : (
          <div className="xingcheng-approvals__list">
            {ordered.map((action, index) => {
              const actionId = String(action.action_id || '')
              const busy = confirmBusyId === actionId
              const message = confirmMessages[actionId]
              const pending = action.status === 'awaiting-confirmation'
              const expired = pending && actionExpired(action)
              const canConfirm = pending && !expired && !busy
              const repairPlan = action.repair_plan || {}
              const repairSteps = Array.isArray(repairPlan.steps)
                ? repairPlan.steps as Array<Record<string, unknown>>
                : []
              const verification = Array.isArray(repairPlan.verification_criteria)
                ? repairPlan.verification_criteria.map(String).join('、')
                : ''
              return (
                <article
                  className="xingcheng-approval"
                  key={actionId || `pending-${index}`}
                >
                  <div className="xingcheng-approval__head">
                    <span className="xingcheng-approval__kind">
                      {action.kind === 'repair' ? xr.kindRepair : xr.kindUpdate}
                    </span>
                    <strong>{action.summary || actionId}</strong>
                  </div>
                  <dl className="xingcheng-approval__detail">
                    {action.fault_id ? (
                      <div><dt>{xr.faultId}</dt><dd>{action.fault_id}</dd></div>
                    ) : null}
                    {action.update_id ? (
                      <div><dt>{xr.updateId}</dt><dd>{action.update_id}</dd></div>
                    ) : null}
                    {action.scope ? (
                      <div><dt>{xr.scopeLabel}</dt><dd>{action.scope}</dd></div>
                    ) : null}
                    {action.target ? (
                      <div><dt>{xr.targetLabel}</dt><dd>{action.target}</dd></div>
                    ) : null}
                    {action.proposed_method ? (
                      <div><dt>{xr.methodLabel}</dt><dd>{action.proposed_method}</dd></div>
                    ) : null}
                    {repairSteps.length > 0 ? (
                      <div>
                        <dt>{xr.repairPlanLabel}</dt>
                        <dd>{repairSteps.map((step) => `${String(step.action || '')} → ${String(step.target || '')}`).join('；')}</dd>
                      </div>
                    ) : null}
                    {verification ? (
                      <div><dt>{xr.verificationLabel}</dt><dd>{verification}</dd></div>
                    ) : null}
                    {action.risk ? (
                      <div><dt>{xr.riskLabel}</dt><dd>{action.risk}</dd></div>
                    ) : null}
                    {action.rollback ? (
                      <div><dt>{xr.rollbackLabel}</dt><dd>{action.rollback}</dd></div>
                    ) : null}
                    {action.expires_at ? (
                      <div><dt>{xr.expiresLabel}</dt><dd>{action.expires_at}</dd></div>
                    ) : null}
                    {action.evidence_digest ? (
                      <div>
                        <dt>{xr.evidenceLabel}</dt>
                        <dd>{action.evidence_digest.slice(0, 16)}</dd>
                      </div>
                    ) : null}
                  </dl>
                  <div className="xingcheng-approval__meta">
                    <span>
                      {expired
                        ? xr.expired
                        : action.status || 'awaiting-confirmation'}
                    </span>
                    <span>{action.created_at || ''}</span>
                  </div>
                  <button
                    type="button"
                    className="xingcheng-approval__confirm"
                    data-testid={`confirm-pending-${actionId}`}
                    disabled={!canConfirm}
                    title={xr.singleItemPermitHint}
                    onClick={() => void onConfirm(actionId)}
                  >
                    {busy
                      ? xr.confirming
                      : pending
                        ? xr.singleItemPermit
                        : xr.confirmedDone}
                  </button>
                  <button
                    type="button"
                    className="button button--ghost"
                    data-testid={`deny-pending-${actionId}`}
                    disabled={!pending || expired || busy}
                    onClick={() => void onDeny(actionId)}
                  >
                    {xr.doNotPermit}
                  </button>
                  {message ? (
                    <p className="xingcheng-approval__message">{message}</p>
                  ) : null}
                </article>
              )
            })}
          </div>
        )}
      </section>

    </Drawer>
  )
}
