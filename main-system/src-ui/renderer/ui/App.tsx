import { useMemo, useState } from 'react'
import { useToolboxApplications } from '@/ui/toolbox/useToolboxApplications'
import { ToolboxEntry } from '@/ui/toolbox/ToolboxEntry'
import { SovereignDashboard } from '@/ui/sovereign/SovereignDashboard'
import { Drawer } from '@/ui/drawer/Drawer'
import { ThirdPartyPanel } from '@/ui/panel/ThirdPartyPanel'
import { mainSystemLocale } from '@/locales/main-system'
import { useAppState } from '@/ui/useAppState'
import { XingchengDrawer } from '@/ui/AppXingchengDrawer'
import { CapacityDrawer } from '@/ui/AppCapacityDrawer'
import { AppSloDrawer } from '@/ui/AppSloDrawer'
import { ExternalCollaborationPanel } from '@/ui/panel/ExternalCollaborationPanel'
import { SagaVisualizerPanel } from '@/ui/panel/SagaVisualizerPanel'
import { ModuleBoundary } from '@/shared/components/ModuleBoundary'
import { useRuntimeStatusField } from '@/shared/hooks/useRuntimeStatusField'
import { filterActiveFaults } from '@/shared/utils/faultPresentation'
import type { GlobalFaults, RuntimeStatusPayload } from '@/ui/sovereign/runtimeStatusTypes'
import '../App.css'

const t = mainSystemLocale.product
const tp = mainSystemLocale.thirdParty
const xr = mainSystemLocale.xingchengReport
const app = mainSystemLocale.app
const tb = mainSystemLocale.toolbox
const ec = mainSystemLocale.externalCollaboration

function displayVersion(value: string): string {
  const match = /^(\d+)\.(\d+)(?:\.\d+)?$/.exec(value.trim())
  return match ? `${match[1]}.${match[2]}` : '1.0'
}

export default function App() {
  const {
    appVersion,
    maintenanceReady,
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
    denyPendingAction,
    setAutomationSwitch,
    setXingchengNativeModelEnabled,
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

  const globalFaults = useRuntimeStatusField('global_faults') as
    | GlobalFaults
    | undefined
  const nativeModel = useRuntimeStatusField('xingcheng_native_model_runtime') as
    | RuntimeStatusPayload['xingcheng_native_model_runtime']
    | undefined

  const activeGlobalFaults = useMemo(
    () => filterActiveFaults(globalFaults?.recent_faults),
    [globalFaults]
  )

  const xingchengReview = useMemo(() => {
    if (
      backendSocket.status === 'Connecting' ||
      backendSocket.status === 'Synchronizing'
    ) {
      return { tone: 'warning' as const, state: xr.reviewing, detail: xr.reviewingDetail, issues: [{ id: 'backend-connecting', source: xr.informationLayer, title: xr.backendInterrupted, detail: xr.reviewingDetail, status: xr.autoRepairing }] }
    }
    if (!connected) {
      return { tone: 'warning' as const, state: xr.connectionAnomaly, detail: xr.connectionDetail, issues: [{ id: 'backend-offline', source: xr.informationLayer, title: xr.backendDisconnected, detail: xr.backendDisconnectedDetail, status: xr.waitingRecovery }] }
    }
    if (!maintenanceReady) {
      return { tone: 'warning' as const, state: xr.healthAnomaly, detail: xr.healthDetail, issues: [{ id: 'maintenance-not-ready', source: xr.maintenanceSovereign, title: xr.maintenanceNotReady, detail: xr.maintenanceNotReadyDetail, status: xr.monitoring }] }
    }
    // Global fault tracking: Xingcheng reports faults from every module,
    // not only the tool cards below.
    const unresolvedFaults = Number(globalFaults?.unresolved || 0)
    if (globalFaults && unresolvedFaults > 0) {
      const issues = activeGlobalFaults.slice(0, 5).map((fault, index) => ({
        id: `global-fault-${String(fault.fault_id || index)}`,
        source: String(fault.source || xr.globalFaultsTitle),
        title: String(fault.error_class || fault.fault_type || xr.toolError),
        detail: String(fault.error_message || xr.noFurtherDetail),
        status: String(fault.repair_outcome || 'pending'),
      }))
      const severityCounts: Record<string, number> = {}
      for (const fault of activeGlobalFaults) {
        const severity = String(fault.severity || 'info')
        severityCounts[severity] = (severityCounts[severity] || 0) + 1
      }
      const severity = Object.entries(severityCounts)
        .map(([key, count]) => `${key} ${count}`)
        .join('、')
      return {
        tone: 'warning' as const,
        state: xr.globalFaultsState.replace('{count}', String(unresolvedFaults)),
        detail: severity || xr.globalFaultsHint,
        issues,
      }
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
  }, [activeGlobalFaults, backendSocket.status, connected, globalFaults, maintenanceReady, toolboxTools])

  const connection = connected
    ? { label: xr.systemNormal, detail: xr.normalDetail, tone: 'online' as const }
    : backendSocket.status === 'Connecting' || backendSocket.status === 'Synchronizing'
      ? { label: xr.systemReviewing, detail: xr.reviewingDetail, tone: 'pending' as const }
      : { label: xr.systemAnomaly, detail: xr.connectionDetail, tone: 'offline' as const }

  const [drawerSovereign, setDrawerSovereign] = useState(false)
  const [drawerXingcheng, setDrawerXingcheng] = useState(false)
  const [drawerCapacity, setDrawerCapacity] = useState(false)
  const [drawerSlo, setDrawerSlo] = useState(false)
  const [drawerThirdParty, setDrawerThirdParty] = useState(false)
  const [drawerExternalCollaboration, setDrawerExternalCollaboration] = useState(false)
  const [drawerSagaVisualizer, setDrawerSagaVisualizer] = useState(false)

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
            data-tone={nativeModel?.running ? 'online' : nativeModel?.available ? 'offline' : 'pending'}
            data-testid="xingcheng-native-model-indicator"
            title={xr.nativeModelTitle}
          >
            <span className="connection-indicator__dot" />
            <span>
              <strong>{xr.nativeModelTitle}</strong>
              <small>{nativeModel?.running ? xr.nativeModelRunning : nativeModel?.available ? xr.nativeModelStopped : xr.nativeModelUnavailable}</small>
            </span>
          </div>
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
            onClick={() => {
              setDrawerXingcheng(true)
              // Periodic pushes are compact; fetch the full snapshot (with
              // the per-item pending list) when the panel opens.
              sendCommand('app:get-runtime-status', {
                source: 'xingcheng_drawer_open',
              })
            }}
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
            onClick={() => setDrawerSlo(true)}
          >
            <span className="drawer-trigger__icon" aria-hidden="true">P</span>
            <span className="drawer-trigger__text">
              <strong>{t.sloDashboard}</strong>
              <small>{t.sloDashboardHint}</small>
            </span>
            <svg className="drawer-trigger__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          <button
            type="button"
            className="drawer-trigger"
            onClick={() => setDrawerExternalCollaboration(true)}
          >
            <span className="drawer-trigger__icon" aria-hidden="true">{ec.icon}</span>
            <span className="drawer-trigger__text">
              <strong>{ec.title}</strong>
              <small>{ec.subtitle}</small>
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
          <button
            type="button"
            className="drawer-trigger"
            onClick={() => setDrawerSagaVisualizer(true)}
          >
            <span className="drawer-trigger__icon" aria-hidden="true">📊</span>
            <span className="drawer-trigger__text">
              <strong>Saga 視覺化</strong>
              <small>跨引擎操作追蹤</small>
            </span>
            <svg className="drawer-trigger__chevron" width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </section>

        <ModuleBoundary name={tb.title}>
          <ToolboxEntry
            tools={toolboxTools}
            connected={operational}
            syncing={toolboxSyncing}
            syncedAt={toolboxSyncedAt}
            onRefresh={() => void refreshToolboxTools()}
            onToolAction={handleToolboxAction}
          />
        </ModuleBoundary>
      </main>

      <footer className="product-footer">
        <span>GPTBridge v{displayVersion(appVersion)}</span>
        <span>{t.footerPlatform}</span>
      </footer>

      <ModuleBoundary name={xr.title}>
        <XingchengDrawer
          open={drawerXingcheng}
          onClose={() => setDrawerXingcheng(false)}
          review={xingchengReview}
          confirmBusyId={confirmBusyId}
          confirmMessages={confirmMessages}
          switchBusy={switchBusy}
          onConfirm={confirmPendingAction}
          onDeny={denyPendingAction}
          onSwitch={setAutomationSwitch}
          onNativeModelSwitch={setXingchengNativeModelEnabled}
          sendCommand={sendCommand}
          waitForIpcEvent={waitForIpcEvent}
        />
      </ModuleBoundary>

      <Drawer
        open={drawerSovereign}
        onClose={() => setDrawerSovereign(false)}
        title={mainSystemLocale.sovereign.title}
        eyebrow={mainSystemLocale.sovereign.eyebrow}
        icon="S"
      >
        <ModuleBoundary name="主權面板">
          <SovereignDashboard />
        </ModuleBoundary>
      </Drawer>

      <ModuleBoundary name="第三方軟體">
        <ThirdPartyPanel
          open={drawerThirdParty}
          onClose={() => setDrawerThirdParty(false)}
          sendCommand={sendCommand}
          waitForIpcEvent={waitForIpcEvent}
          backendSocket={backendSocket}
        />
      </ModuleBoundary>

      <ModuleBoundary name={ec.moduleBoundaryName}>
        <ExternalCollaborationPanel
          open={drawerExternalCollaboration}
          onClose={() => setDrawerExternalCollaboration(false)}
          sendCommand={sendCommand}
          waitForIpcEvent={waitForIpcEvent}
          backendSocket={backendSocket}
        />
      </ModuleBoundary>

      <ModuleBoundary name="Saga 視覺化">
        <SagaVisualizerPanel
          open={drawerSagaVisualizer}
          onClose={() => setDrawerSagaVisualizer(false)}
          sendCommand={sendCommand}
          waitForIpcEvent={waitForIpcEvent}
          backendSocket={backendSocket}
        />
      </ModuleBoundary>

      <ModuleBoundary name="容量資訊">
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
      </ModuleBoundary>

      <ModuleBoundary name="效能 SLO">
        <AppSloDrawer
          open={drawerSlo}
          onClose={() => setDrawerSlo(false)}
        />
      </ModuleBoundary>
    </div>
  )
}
