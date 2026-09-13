import type { PendingActionApproval } from '@/ui/sovereign/SovereignDashboard'
import { Drawer } from '@/ui/drawer/Drawer'
import { useRuntimeStatusField } from '@/shared/hooks/useRuntimeStatusField'
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

export type XingchengDrawerProps = {
  open: boolean
  onClose: () => void
  review: XingchengReview
  confirmBusyId: string | null
  confirmMessages: Record<string, string>
  switchBusy: string | null
  onConfirm: (actionId: string) => void
  onSwitch: (switchName: string, enabled: boolean) => void
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
  onSwitch,
}: XingchengDrawerProps) {
  // Modular subscriptions: this drawer re-renders only when its own fields
  // change; a failure here is contained by the module boundary upstream.
  const pendingActions = useRuntimeStatusField('pending_actions') ?? []
  const switches = useRuntimeStatusField('automation_switches') ?? {}
  const cardinality = useRuntimeStatusField('pending_action_cardinality') ?? {}
  const repairSwitchOn = switches.automatic_repair_enabled === true
  const updateSwitchOn = switches.automatic_update_enabled === true
  const ordered = orderPendingActions(pendingActions)
  const cardinalityLabel =
    cardinality.mode === 'MULTI_FAULT'
      ? xr.cardinalityMulti
      : cardinality.mode === 'SINGLE_FAULT'
        ? xr.cardinalitySingle
        : xr.cardinalityNoFault
  const switchOnForKind = (kind: string): boolean =>
    kind === 'repair' ? repairSwitchOn : kind === 'update' ? updateSwitchOn : false
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

      <section className="xingcheng-approvals" data-testid="pending-approvals">
        <div className="xingcheng-approvals__head">
          <strong>{xr.switchesTitle}</strong>
          <span>{xr.switchAttribution}</span>
        </div>
        <div className="xingcheng-switches">
          {(
            [
              ['automatic_repair_enabled', xr.switchRepair, repairSwitchOn],
              ['automatic_update_enabled', xr.switchUpdate, updateSwitchOn],
            ] as Array<[string, string, boolean]>
          ).map(([switchName, label, enabled]) => (
            <div className="xingcheng-switch" key={switchName}>
              <span className="xingcheng-switch__label">{label}</span>
              <button
                type="button"
                className="xingcheng-switch__toggle"
                data-tone={enabled ? 'on' : 'off'}
                data-testid={`switch-${switchName}`}
                disabled={switchBusy === switchName}
                onClick={() => void onSwitch(switchName, !enabled)}
              >
                {enabled ? xr.switchOn : xr.switchOff}
              </button>
            </div>
          ))}
        </div>

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
              const switchReady = switchOnForKind(String(action.kind || ''))
              const canConfirm = pending && switchReady && !expired && !busy
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
                    title={!switchReady ? xr.switchDisabledHint : undefined}
                    onClick={() => void onConfirm(actionId)}
                  >
                    {busy
                      ? xr.confirming
                      : pending
                        ? xr.confirm
                        : xr.confirmedDone}
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
