import { zhTW } from '@/i18n/zhTW'
import type { GovernanceCommandSender } from './types'
import { useGovernanceRules } from './useGovernanceRules'
import './governance-rules.css'

interface GovernanceRulesPageProps {
  sendCommand: GovernanceCommandSender
  socketStatus: string
  onBack?: () => void
}

function ruleLabel(rule: string): string {
  return (zhTW as any).governanceRules?.[rule] || `全域規則：${rule}`
}

export function GovernanceRulesPage({
  sendCommand,
  socketStatus,
  onBack,
}: GovernanceRulesPageProps) {
  const governance = useGovernanceRules({ sendCommand, socketStatus })
  const labels = zhTW.governanceRulesPage
  const selectedLabel = governance.selectedRule
    ? ruleLabel(governance.selectedRule)
    : labels.noSelectedRule

  return (
    <section className="gov-shell">
      <div className="gov-background-grid" />

      <header className="gov-header">
        <div>
          <p className="gov-eyebrow">{labels.eyebrow}</p>
          <h1 className="gov-title">{labels.title}</h1>
          <p className="gov-description">{labels.description}</p>
        </div>
        {onBack ? (
          <button type="button" className="gov-secondary-action" onClick={onBack}>
            {labels.backHome}
          </button>
        ) : null}
      </header>

      <div className="gov-summary-grid">
        <article className="gov-summary-card">
          <span>{labels.connectionStatus}</span>
          <strong>{socketStatus}</strong>
          <small>{labels.connectionHint}</small>
        </article>
        <article className="gov-summary-card gov-summary-card--active">
          <span>{labels.activeRules}</span>
          <strong>{governance.activeRules.length}</strong>
          <small>{labels.activeRulesHint}</small>
        </article>
        <article className="gov-summary-card">
          <span>{labels.availableRules}</span>
          <strong>{governance.rules.length}</strong>
          <small>{labels.availableRulesHint}</small>
        </article>
      </div>

      <div className="gov-layout">
        <article className="gov-panel gov-panel--primary">
          <div className="gov-panel-head">
            <div>
              <h2>{labels.controlTitle}</h2>
              <p>{labels.controlDesc}</p>
            </div>
            <button
              type="button"
              className="gov-action"
              disabled={governance.loading || governance.busy}
              onClick={() => void governance.refreshRules()}
            >
              {governance.loading ? labels.syncing : labels.refresh}
            </button>
          </div>

          <label className="gov-field">
            <span>{labels.ruleSelect}</span>
            <select
              className="gov-select"
              value={governance.selectedRule}
              disabled={governance.loading || governance.busy || governance.rules.length === 0}
              onChange={(event) => governance.setSelectedRule(event.target.value)}
            >
              {governance.rules.length === 0 ? (
                <option value="">{labels.emptyRules}</option>
              ) : (
                governance.rules.map((rule) => (
                  <option key={rule} value={rule}>
                    {`${ruleLabel(rule)} [${labels.activeBadge}]`}
                  </option>
                ))
              )}
            </select>
          </label>

          <div className="gov-selected-rule">
            <span>{labels.selectedRule}</span>
            <strong>{selectedLabel}</strong>
            <p>{labels.globalActiveNotice}</p>
          </div>

          <label className="gov-field gov-field--draft">
            <span>{labels.inputLabel}</span>
            <input
              type="text"
              className="gov-input"
              value={governance.ruleDraft}
              disabled={governance.loading || governance.busy}
              onChange={(event) => governance.setRuleDraft(event.target.value)}
              placeholder={labels.inputPlaceholder}
            />
          </label>

          <div className="gov-code-preview">
            <span>{labels.convertedCode}</span>
            <code>{governance.convertedRuleCode || labels.noConvertedCode}</code>
            {governance.isConvertedFromChinese ? (
              <small>{labels.chineseConvertedHint}</small>
            ) : null}
          </div>

          <div className="gov-action-row">
            <button
              type="button"
              className="gov-action gov-action--strong"
              disabled={governance.loading || governance.busy}
              onClick={() => void governance.addRule()}
            >
              {governance.busy ? labels.processing : labels.addOrEnable}
            </button>
            <button
              type="button"
              className="gov-action"
              disabled={
                governance.loading ||
                governance.busy ||
                !governance.selectedRule ||
                !governance.convertedRuleCode
              }
              onClick={() => void governance.updateRule()}
            >
              {labels.modifyRule}
            </button>
            <button
              type="button"
              className="gov-action gov-action--danger"
              disabled={
                governance.loading || governance.busy || !governance.selectedRule
              }
              onClick={() => void governance.removeCustomRule(governance.selectedRule)}
            >
              {labels.removeCustomRule}
            </button>
          </div>

          {governance.feedback ? (
            <div className="gov-feedback">{governance.feedback}</div>
          ) : null}
        </article>

        <aside className="gov-panel">
          <div className="gov-panel-head">
            <div>
              <h2>{labels.activeListTitle}</h2>
              <p>{labels.activeListDesc}</p>
            </div>
          </div>

          <div className="gov-rule-list">
            {governance.activeRules.length === 0 ? (
              <div className="gov-empty">{labels.emptyActiveRules}</div>
            ) : (
              governance.activeRules.map((rule) => (
                <button
                  type="button"
                  key={rule}
                  className="gov-rule-item"
                  onClick={() => governance.setSelectedRule(rule)}
                >
                  <span>{ruleLabel(rule)}</span>
                  <b>{labels.activeBadge}</b>
                </button>
              ))
            )}
          </div>
        </aside>
      </div>
    </section>
  )
}
