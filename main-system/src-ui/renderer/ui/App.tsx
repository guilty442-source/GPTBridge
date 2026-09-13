import { useMemo, useState } from 'react'
import { useToolboxApplications } from '@/ui/toolbox/useToolboxApplications'
import { ToolboxEntry } from '@/ui/toolbox/ToolboxEntry'
import { SovereignDashboard } from '@/ui/sovereign/SovereignDashboard'
import { Drawer } from '@/ui/drawer/Drawer'
import { ThirdPartyPanel } from '@/ui/third-party/ThirdPartyPanel'
import { mainSystemLocale } from '@/locales/main-system'
import { useAppState } from '@/ui/useAppState'
import { XingchengDrawer } from '@/ui/AppXingchengDrawer'
import { CapacityDrawer } from '@/ui/AppCapacityDrawer'
import '../App.css'

const t = mainSystemLocale.product
const tp = mainSystemLocale.thirdParty
const xr = mainSystemLocale.xingchengReport
const app = mainSystemLocale.app

function displayVersion(value: string): string {
  const match = /^(\d+)\.(\d+)(?:\.\d+)?$/.exec(value.trim())
  return match ? `${match[1]}.${match[2]}` : '1.0'
}

export default function App() {
  const {
    appVersion,
    maintenanceReady,
    runtimeStatus,
    systemMetrics,
    confirmBusyId,
    switchBusy,
    confirmMessages,
    backendSocket,
    sendCommand,
    connected,
    operational,
    waitForIpcEvent,
    confirmPendingAction,
    setAutomationSwitch,
    pendingActions,
    repairSwitchOn,
    updateSwitchOn,
    cardinality,
  } = useAppState()

  const {
    toolboxTools,
    toolboxSyncing,
    toolboxSyncedAt,
    mainSystemSizeBytes,
    mainSystemFileCount,
    dependencySizeBytes,
    dependencyFileCount,
    sharedLayerSizeBytes,
    sharedLayerFileCount,
    workspaceSizeBytes,
    workspaceFileCount,
    refreshToolboxTools,
    handleToolboxAction,
  } = useToolboxApplications({
    backendStatus: backendSocket.status,
    sendCommand,
    waitForIpcEvent,
  })

  const summary = useMemo(() => {
    const running = toolboxTools.filter((tool) => tool.status === 'running').length
    const issues = toolboxTools.filter(
      (tool) =>
        tool.status === 'error' ||
        (tool.launchable !== false && tool.runtimeAvailable === false)
    ).length
    return { running, issues, total: toolboxTools.length }
  }, [toolboxTools])

  const xingchengReview = useMemo(() => {
    if (backendSocket.status === 'Connecting' || backendSocket.status === 'Repairing') {
      return { tone: 'warning' as const, state: xr.reviewing, detail: xr.reviewingDetail, issues: [{ id: 'backend-reconnecting', source: xr.informationLayer, title: xr.backendInterrupted, detail: `${xr.reviewingDetail}：${backendSocket.status}`, status: xr.autoRepairing }] }
    }
    if (!connected) {
      return { tone: 'warning' as const, state: xr.connectionAnomaly, detail: xr.connectionDetail, issues: [{ id: 'backend-offline', source: xr.informationLayer, title: xr.backendDisconnected, detail: xr.backendDisconnectedDetail, status: xr.waitingRecovery }] }
    }
    if (!maintenanceReady) {
      return { tone: 'warning' as const, state: xr.healthAnomaly, detail: xr.healthDetail, issues: [{ id: 'maintenance-not-ready', source: xr.maintenanceSovereign, title: xr.maintenanceNotReady, detail: xr.maintenanceNotReadyDetail, status: xr.monitoring }] }
    }
    const affected = toolboxTools
      .filter((tool) => tool.status === 'error' || (tool.launchable !== false && tool.runtimeAvailable === false))
    const issues = affected.map((tool) => ({
      id: `tool-${tool.id}`,
      source: tool.name,
      title: tool.status === 'error' ? xr.toolError : xr.runtimeUnavailable,
      detail: tool.note || tool.description || tool.summary || xr.noFurtherDetail,
      status: tool.status === 'error' ? xr.needsAction : xr.waitingRuntime,
    }))
    if (affected.length > 0) {
      return {
        tone: 'warning' as const,
        state: `${affected.length} ${xr.affectedSuffix}`,
        detail: affected.slice(0, 3).map((tool) => tool.name).join('、'),
        issues,
      }
    }
    return { tone: 'ok' as const, state: xr.normal, detail: xr.normalDetail, issues: [] }
  }, [backendSocket.status, connected, maintenanceReady, toolboxTools])

  const connection = connected
    ? { label: xr.systemNormal, detail: xr.normalDetail, tone: 'online' as const }
    : backendSocket.status === 'Connecting' || backendSocket.status === 'Repairing' || backendSocket.status === 'Synchronizing'
      ? { label: xr.systemReviewing, detail: xr.reviewingDetail, tone: 'pending' as const }
      : { label: xr.systemAnomaly, detail: xr.connectionDetail, tone: 'offline' as const }

  const [drawerSovereign, setDrawerSovereign] = useState(false)
  const [drawerXingcheng, setDrawerXingcheng] = useState(false)
  const [drawerCapacity, setDrawerCapacity] = useState(false)
  const [drawerThirdParty, setDrawerThirdParty] = useState(false)

  return (
    <div className="product-shell">
      <header className="product-header">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">G</div>
          <div>
            <strong>GPTBridge</strong>
            <span>{t.brandSubtitle}</span>
          </div>
        </div>

        <div className="header-controls">
          <div
            className="connection-indicator"
            data-tone={connection.tone}
            data-testid="backend-connection"
          >
            <span className="connection-indicator__dot" />
            <span>
              <strong>{connection.label}</strong>
              <small>{connection.detail}</small>
            </span>
          </div>
          <span className="version-badge" data-testid="product-version">
            v{displayVersion(appVersion)}
          </span>
        </div>
      </header>

      <main className="product-main">
        {!operational && (
          <aside className="connection-notice" role="status">
            <strong>{connected ? t.maintenanceIncomplete : t.offlineSafeMode}</strong>
            <span>
              {connected
                ? t.maintenanceNotice
                : t.offlineNotice}
            </span>
          </aside>
        )}

        <section className="hero-grid" aria-label={t.systemOverview}>
          <article className="hero-card hero-card--primary">
            <span className="hero-card__label">{t.availableTools}</span>
            <strong className="hero-card__value">{summary.total}</strong>
            <small className="hero-card__hint">{t.availableToolsHint}</small>
          </article>
          <article className="hero-card">
            <span className="hero-card__label">{t.runningTools}</span>
            <strong className="hero-card__value">{summary.running}</strong>
            <small className="hero-card__hint">{t.runningToolsHint}</small>
          </article>
          <article className="hero-card" data-tone={summary.issues > 0 ? 'warning' : 'ok'}>
            <span className="hero-card__label">{t.issues}</span>
            <strong className="hero-card__value">{summary.issues}</strong>
            <small className="hero-card__hint">{t.issuesHint}</small>
          </article>
          <article className="hero-card">
            <span className="hero-card__label">{t.commandStrategy}</span>
            <strong className="hero-card__value hero-card__value--text">{t.requestToolExecution}</strong>
            <small className="hero-card__hint">{t.commandStrategyHint}</small>
          </article>
          <button
            type="button"
            className="hero-card hero-card--interactive"
            data-tone={xingchengReview.tone}
            data-testid="xingcheng-global-review"
            aria-haspopup="dialog"
            onClick={() => setDrawerXingcheng(true)}
          >
            <span className="hero-card__label">{mainSystemLocale.sovereign.xingchengTitle}</span>
            <strong className="hero-card__value hero-card__value--text">{xingchengReview.state}</strong>
            <small className="hero-card__hint">{xingchengReview.detail}</small>
            <span className="hero-card__action">{xr.viewDetails}</span>
          </button>
        </section>

        <div className="section-heading">
          <div>
            <span className="eyebrow">{app.systemMgmt}</span>
            <h2>{app.commonEntries}</h2>
          </div>
          <span>{app.settingsDesc}</span>
        </div>

        <section className="drawer-triggers">
          <button
            type="button"
            className="drawer-trigger"
            onClick={() => setDrawerSovereign(true)}
          >
            <span className="drawer-trigger__icon" aria-hidden="true">S</span>
            <span className="drawer-trigger__text">
              <strong>{mainSystemLocale.sovereign.title}</strong>
              <small>{mainSystemLocale.sovereign.eyebrow}</small>
            </span>
            <svg className="drawer-trigger__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <button
            type="button"
            className="drawer-trigger"
            onClick={() => setDrawerCapacity(true)}
          >
            <span className="drawer-trigger__icon" aria-hidden="true">D</span>
            <span className="drawer-trigger__text">
              <strong>{t.capacityDetails}</strong>
              <small>{t.systemDisk} · {t.workspaceSize}</small>
            </span>
            <svg className="drawer-trigger__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <button
            type="button"
            className="drawer-trigger"
            onClick={() => setDrawerThirdParty(true)}
          >
            <span className="drawer-trigger__icon" aria-hidden="true">T</span>
            <span className="drawer-trigger__text">
              <strong>{tp.title}</strong>
              <small>{tp.subtitle}</small>
            </span>
            <svg className="drawer-trigger__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </section>

        <ToolboxEntry
          tools={toolboxTools}
          connected={operational}
          syncing={toolboxSyncing}
          syncedAt={toolboxSyncedAt}
          onRefresh={() => void refreshToolboxTools()}
          onToolAction={handleToolboxAction}
        />
      </main>

      <footer className="product-footer">
        <span>GPTBridge v{displayVersion(appVersion)}</span>
        <span>{t.footerPlatform}</span>
      </footer>

      <XingchengDrawer
        open={drawerXingcheng}
        onClose={() => setDrawerXingcheng(false)}
        review={xingchengReview}
        runtimeStatus={runtimeStatus}
        pendingActions={pendingActions}
        confirmBusyId={confirmBusyId}
        confirmMessages={confirmMessages}
        switchBusy={switchBusy}
        repairSwitchOn={repairSwitchOn}
        updateSwitchOn={updateSwitchOn}
        cardinality={cardinality}
        onConfirm={confirmPendingAction}
        onSwitch={setAutomationSwitch}
      />

      <Drawer
        open={drawerSovereign}
        onClose={() => setDrawerSovereign(false)}
        title={mainSystemLocale.sovereign.title}
        eyebrow={mainSystemLocale.sovereign.eyebrow}
        icon="S"
      >
        <SovereignDashboard runtimeStatus={runtimeStatus} />
      </Drawer>

      <Drawer
        open={drawerThirdParty}
        onClose={() => setDrawerThirdParty(false)}
        title={tp.title}
        eyebrow={tp.subtitle}
        icon="T"
      >
        <ThirdPartyPanel />
      </Drawer>

      <CapacityDrawer
        open={drawerCapacity}
        onClose={() => setDrawerCapacity(false)}
        systemMetrics={systemMetrics}
        mainSystemSizeBytes={mainSystemSizeBytes}
        mainSystemFileCount={mainSystemFileCount}
        dependencySizeBytes={dependencySizeBytes}
        dependencyFileCount={dependencyFileCount}
        sharedLayerSizeBytes={sharedLayerSizeBytes}
        sharedLayerFileCount={sharedLayerFileCount}
        workspaceSizeBytes={workspaceSizeBytes}
        workspaceFileCount={workspaceFileCount}
      />
    </div>
  )
}
