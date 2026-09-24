import React, { Suspense, lazy, useMemo } from 'react'
import { useLocalBackendSocket } from '../backendSocket'
import { InvestmentUIProvider, PageId, useNotify, useUIState } from './uiState'
import { XingchengPanel } from './XingchengPanel'
import { useCommandQuery, useIpcEvents } from './hooks'
import { DataFreshnessIndicator } from './freshness'

// §22 lazy pages — only the active page mounts
const OverviewPage = lazy(() => import('./pages/OverviewPage'))
const TaiwanEquityPage = lazy(() => import('./pages/TaiwanEquityPage'))
const USEquityPage = lazy(() => import('./pages/USEquityPage'))
const MutualFundPage = lazy(() => import('./pages/MutualFundPage'))
const RecommendationPage = lazy(() => import('./pages/RecommendationPage'))
const AutoTradingDashboard = lazy(() => import('./pages/AutoTradingDashboard'))
const StrategyManagementPage = lazy(() => import('./pages/StrategyManagementPage'))
const StrategyLaboratoryPage = lazy(() => import('./pages/StrategyLaboratoryPage'))
const AssetAllocationPage = lazy(() => import('./pages/AssetAllocationPage'))
const RiskCenterPage = lazy(() => import('./pages/RiskCenterPage'))
const ReportPage = lazy(() => import('./pages/ReportPage'))
const ImportPage = lazy(() => import('./pages/ImportPage'))
const BrokerManagementPage = lazy(() => import('./pages/BrokerManagementPage'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))

const NAV: { group: string; items: { id: PageId; label: string }[] }[] = [
  {
    group: '總覽',
    items: [
      { id: 'overview', label: '全資產首頁' },
      { id: 'tw-equity', label: '台灣股票' },
      { id: 'us-equity', label: '美國股票' },
      { id: 'fund', label: '共同基金' },
    ],
  },
  {
    group: 'AI 與建議',
    items: [
      { id: 'recommendations', label: 'AI 買賣建議' },
      { id: 'reports', label: '投資報告' },
      { id: 'allocation', label: '資產配置' },
    ],
  },
  {
    group: '自動模擬操盤',
    items: [
      { id: 'autotrade', label: '自動操盤控制台' },
      { id: 'strategies', label: '多策略管理' },
      { id: 'laboratory', label: '策略實驗室' },
      { id: 'risk', label: '風險中心' },
    ],
  },
  {
    group: '系統',
    items: [
      { id: 'import', label: '資料匯入' },
      { id: 'brokers', label: '券商管理' },
      { id: 'settings', label: '系統設定' },
    ],
  },
]

type PageProps = {
  sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string }
}
const PAGES: Record<
  PageId,
  React.LazyExoticComponent<React.ComponentType<PageProps>>
> = {
  overview: OverviewPage,
  'tw-equity': TaiwanEquityPage,
  'us-equity': USEquityPage,
  fund: MutualFundPage,
  recommendations: RecommendationPage,
  autotrade: AutoTradingDashboard,
  strategies: StrategyManagementPage,
  laboratory: StrategyLaboratoryPage,
  allocation: AssetAllocationPage,
  risk: RiskCenterPage,
  reports: ReportPage,
  import: ImportPage,
  brokers: BrokerManagementPage,
  settings: SettingsPage,
}

function TopBar(props: {
  status: string
  sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string }
}) {
  const { state, dispatch } = useUIState()
  const send = props.sendCommand
  const market = useCommandQuery<any>(send, 'investment_market_status')
  const mode = useCommandQuery<any>(send, 'investment_trade_mode')
  const tw = market.data?.tw || market.data?.markets?.tw
  const us = market.data?.us || market.data?.markets?.us
  return (
    <header className="ws-topbar">
      <span className="ws-mode">
        模式 <b>{String(mode.data?.mode || state.mode)}</b>
        {String(mode.data?.mode) !== 'LIVE' && <i>LIVE 未啟用</i>}
      </span>
      <span className="ws-market">
        台股 {String(tw?.session || tw?.status || '—')}
      </span>
      <span className="ws-market">
        美股 {String(us?.session || us?.status || '—')}
      </span>
      <span className="ws-conn">後端：{props.status}</span>
      <span className="ws-controls">
        <button
          title="字體縮小"
          onClick={() =>
            dispatch({ type: 'fontScale', fontScale: state.fontScale - 0.1 })
          }
        >
          A−
        </button>
        <button
          title="字體放大"
          onClick={() =>
            dispatch({ type: 'fontScale', fontScale: state.fontScale + 0.1 })
          }
        >
          A+
        </button>
        <button
          onClick={() =>
            dispatch({
              type: 'theme',
              theme: state.theme === 'dark' ? 'light' : 'dark',
            })
          }
        >
          {state.theme === 'dark' ? '淺色' : '深色'}
        </button>
        <button
          onClick={() =>
            dispatch({ type: 'aiPanel', open: !state.aiPanelOpen })
          }
        >
          {state.aiPanelOpen ? '收起星澄' : '星澄助手'}
        </button>
      </span>
    </header>
  )
}

function SideNav() {
  const { state, dispatch } = useUIState()
  if (state.sidebarCollapsed) {
    return (
      <nav className="ws-nav ws-nav-collapsed">
        <button
          className="ws-nav-expand"
          onClick={() => dispatch({ type: 'sidebar', collapsed: false })}
        >
          »
        </button>
        {NAV.flatMap((g) => g.items).map((i) => (
          <button
            key={i.id}
            title={i.label}
            className={i.id === state.page ? 'active' : ''}
            onClick={() => dispatch({ type: 'page', page: i.id })}
          >
            {i.label.slice(0, 2)}
          </button>
        ))}
      </nav>
    )
  }
  return (
    <nav className="ws-nav">
      <button
        className="ws-nav-expand"
        onClick={() => dispatch({ type: 'sidebar', collapsed: true })}
      >
        « 收合
      </button>
      {NAV.map((g) => (
        <div key={g.group} className="ws-nav-group">
          <span className="ws-nav-group-label">{g.group}</span>
          {g.items.map((i) => (
            <button
              key={i.id}
              className={i.id === state.page ? 'active' : ''}
              onClick={() => dispatch({ type: 'page', page: i.id })}
            >
              {i.label}
            </button>
          ))}
        </div>
      ))}
    </nav>
  )
}

function BottomBar() {
  const { state } = useUIState()
  const latest = state.notices[0]
  return (
    <footer className="ws-bottombar">
      {latest ? (
        <span className={`ws-notice ws-notice-${latest.severity}`}>
          {new Date(latest.at).toLocaleTimeString('zh-TW', { hour12: false })}{' '}
          {latest.text}
        </span>
      ) : (
        <span className="ws-notice">無通知</span>
      )}
      <span className="ws-bottom-hint">
        所有交易為 SHADOW/PAPER 模擬；券商連線離線
      </span>
    </footer>
  )
}

function WorkspaceInner() {
  const { state, dispatch } = useUIState()
  const { sendCommand, status } = useLocalBackendSocket()
  const notify = useNotify()

  // Event-driven updates — one subscription at the shell, pages read
  // fresh state via their own bounded queries (§20)
  useIpcEvents((event, payload) => {
    const p = payload as Record<string, unknown> | undefined
    if (!p) return
    if (event === 'investment-mobile-monitor-alert' ||
        event.endsWith('_alert')) {
      notify(`風控/警示：${String(p.title || p.event_type || event)}`,
             String(p.severity || 'WARNING'))
    }
    if (event === 'record_mode' && p.mode) {
      dispatch({ type: 'mode', mode: String(p.mode) })
    }
  })

  const Page = PAGES[state.page]
  return (
    <div
      className={`ws-app theme-${state.theme}`}
      style={{ fontSize: `${state.fontScale}rem` }}
    >
      <TopBar status={status} sendCommand={sendCommand} />
      <div className="ws-body">
        <SideNav />
        <main className="ws-main">
          <Suspense fallback={<div className="inv-empty">載入中…</div>}>
            <Page sendCommand={sendCommand} />
          </Suspense>
        </main>
        {state.aiPanelOpen && <XingchengPanel sendCommand={sendCommand} />}
      </div>
      <BottomBar />
    </div>
  )
}

export function InvestmentWorkspace() {
  return (
    <InvestmentUIProvider>
      <WorkspaceInner />
    </InvestmentUIProvider>
  )
}
