import { formatInvestmentNumber } from './investmentWatchFeature'

type TopbarProps = {
  liveUpdateLabel: string
  holdingValueLabel: string
  estimatedWeeklyDividendTwd: number
  busyAction: string
  readPortfolioFile: () => Promise<void>
  loadInvestmentState: (silent?: boolean, force?: boolean) => Promise<void>
}

export function Topbar(props: TopbarProps) {
  const {
    liveUpdateLabel,
    holdingValueLabel,
    estimatedWeeklyDividendTwd,
    busyAction,
    readPortfolioFile,
    loadInvestmentState,
  } = props
  const busy = Boolean(busyAction)
  return (
    <header className="nexus-topbar">
      <div className="nexus-title-block">
        <p className="nexus-eyebrow">投資管理</p>
        <h1>AI投資管家</h1>
        <div className="nexus-top-meta">
          <span>{liveUpdateLabel}</span>
          <span>獨立投資對話 · 本機主模型＋備援模型</span>
          <span>{holdingValueLabel}</span>
          <span>
            週配息 NT$ {formatInvestmentNumber(estimatedWeeklyDividendTwd, 2)}
          </span>
        </div>
      </div>
      <div className="nexus-toolbar" aria-label="主要操作">
        <button
          type="button"
          className="nexus-primary"
          onClick={() => void readPortfolioFile()}
          disabled={busy}
        >
          {busyAction === 'investment:read-file' ? '讀取中...' : '讀取檔案'}
        </button>
        <button
          type="button"
          onClick={() => void loadInvestmentState()}
          disabled={busy}
        >
          重新整理
        </button>
      </div>
    </header>
  )
}
