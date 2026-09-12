import type { InvestmentDiagnostics } from './investmentWatchFeature'

type SystemWorkspaceProps = {
  diagnosticState: string
  diagnosticStateLabel: string
  diagnosticMessage: string
  portfolioAgeLabel: string
  workbookDiagnosticLabel: string
  localAiNetworkCoverage: string
  localAiVerificationLabel: string
  latestRunLabel: string
  diagnostics: InvestmentDiagnostics | null
  socketLabel: string
  hasLatestPortfolioVersion: boolean
  busyAction: string
  openExcelMapper: (chooseNewFile: boolean) => Promise<void>
  restoreLatestPortfolioVersion: () => Promise<void>
  exportInvestmentReport: () => Promise<void>
  clearInvestmentData: () => Promise<void>
}

export function SystemWorkspace(props: SystemWorkspaceProps) {
  const {
    diagnosticState,
    diagnosticStateLabel,
    diagnosticMessage,
    portfolioAgeLabel,
    workbookDiagnosticLabel,
    localAiNetworkCoverage,
    localAiVerificationLabel,
    latestRunLabel,
    diagnostics,
    socketLabel,
    hasLatestPortfolioVersion,
    busyAction,
    openExcelMapper,
    restoreLatestPortfolioVersion,
    exportInvestmentReport,
    clearInvestmentData,
  } = props
  const busy = Boolean(busyAction)

  return (
    <>
      <section
        className={`nexus-diagnostics-panel nexus-diagnostics-panel--${diagnosticState}`}
        aria-label="本地診斷"
      >
        <div className="nexus-diagnostics-summary">
          <span>{diagnosticStateLabel}</span>
          <strong>{diagnosticMessage}</strong>
        </div>
        <div className="nexus-diagnostic-grid">
          <div>
            <span>資料年齡</span>
            <strong>{portfolioAgeLabel}</strong>
          </div>
          <div>
            <span>Excel 掃描</span>
            <strong>{workbookDiagnosticLabel}</strong>
          </div>
          <div>
            <span>投資管家風險</span>
            <strong>
              {diagnostics?.xingcheng?.warning_count ?? 0} /{' '}
              {diagnostics?.xingcheng?.critical_count ?? 0}
            </strong>
          </div>
          <div>
            <span>網路報價</span>
            <strong>{localAiNetworkCoverage}</strong>
          </div>
          <div>
            <span>交叉驗證</span>
            <strong>{localAiVerificationLabel}</strong>
          </div>
          <div>
            <span>最近執行</span>
            <strong>{latestRunLabel}</strong>
          </div>
          <div>
            <span>本地錯誤記錄</span>
            <strong>{diagnostics?.error_logging?.count ?? 0} 筆</strong>
          </div>
        </div>
      </section>

      <section className="nexus-system-overview" aria-label="系統與治理">
        <article className="nexus-surface">
          <div className="nexus-section-head">
            <span>服務架構</span>
            <strong>{socketLabel}</strong>
          </div>
          <p>投資管家只管理本機持股與設定；分析及帳務決策由投資管家提供。</p>
          <div className="nexus-system-flow">
            <span>投資管家</span>
            <strong>→ AI 通道 →</strong>
            <span>投資管家</span>
          </div>
        </article>
        <article className="nexus-surface">
          <div className="nexus-section-head">
            <span>自動資料</span>
            <strong>{localAiNetworkCoverage}</strong>
          </div>
          <p>配息、股價與淨值由投資管家搜尋；未知值不覆寫手動資料。</p>
          <span>{localAiVerificationLabel}</span>
        </article>
        <article className="nexus-surface">
          <div className="nexus-section-head">
            <span>手機工具</span>
            <strong>已分離</strong>
          </div>
          <p>investment-mobile 經投資管家連線；桌面管家不開啟 LAN 服務。</p>
        </article>
        <article className="nexus-surface">
          <div className="nexus-section-head">
            <span>治理</span>
            <strong>最高權限</strong>
          </div>
          <p>投資管家負責分析與自主帳務；外部 AI 不能直接寫入投資管家。</p>
        </article>
        <article className="nexus-surface nexus-system-tools">
          <div className="nexus-section-head">
            <span>維護工具</span>
            <strong>必要時使用</strong>
          </div>
          <p>低頻率維護操作集中在這裡，不占用日常持股與投資管家工作區。</p>
          <div>
            <button
              type="button"
              onClick={() => void openExcelMapper(false)}
              disabled={busy}
            >
              Excel 欄位設定
            </button>
            <button
              type="button"
              onClick={() => void restoreLatestPortfolioVersion()}
              disabled={busy || !hasLatestPortfolioVersion}
            >
              還原最近持股
            </button>
            <button
              type="button"
              onClick={() => void exportInvestmentReport()}
              disabled={busy}
            >
              匯出去識別診斷
            </button>
            <button
              type="button"
              className="nexus-danger"
              onClick={() => {
                if (
                  window.confirm(
                    '確定刪除投資管家的本機舊資料？此操作不會修改原始 Excel。'
                  )
                ) {
                  void clearInvestmentData()
                }
              }}
              disabled={busy}
            >
              刪除舊資料
            </button>
          </div>
        </article>
      </section>
    </>
  )
}
