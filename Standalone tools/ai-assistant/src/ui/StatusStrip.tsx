type StatusStripProps = {
  holdingValueLabel: string
  portfolioFileName: string
  localAiStatusLabel: string
  localAiScore: string
  localAiQuoteHealthLabel: string
  localAiNetworkCoverage: string
  portfolioUpdatedAt: string
  message: string
}

export function StatusStrip(props: StatusStripProps) {
  const {
    holdingValueLabel,
    portfolioFileName,
    localAiStatusLabel,
    localAiScore,
    localAiQuoteHealthLabel,
    localAiNetworkCoverage,
    portfolioUpdatedAt,
    message,
  } = props
  return (
    <section className="nexus-status-strip" aria-label="投資管家狀態">
      <div>
        <span>投資組合</span>
        <strong>
          {holdingValueLabel} · {portfolioFileName || '尚未匯入'}
        </strong>
      </div>
      <div>
        <span>投資管家</span>
        <strong>
          {localAiStatusLabel} · {localAiScore}
        </strong>
      </div>
      <div>
        <span>市場資料</span>
        <strong>
          {localAiQuoteHealthLabel} · {localAiNetworkCoverage}
        </strong>
      </div>
      <div className="nexus-status-message">
        <span>{portfolioUpdatedAt}</span>
        <strong>{message}</strong>
      </div>
    </section>
  )
}
