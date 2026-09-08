import {
  formatInvestmentNumber,
  investmentCodeLabel,
  type InvestmentHolding,
} from './investmentWatchFeature'

type HoldingTableProps = {
  holdings: InvestmentHolding[]
  busyAction: string
  onEdit: (holding: InvestmentHolding) => void
  onDelete: (holding: InvestmentHolding) => void
}

export function HoldingTable({
  holdings,
  busyAction,
  onEdit,
  onDelete,
}: HoldingTableProps) {
  const busy = Boolean(busyAction)
  return (
    <div className="nexus-holding-table">
      <div className="nexus-holding-head">
        <span>標的</span>
        <span>數量</span>
        <span>成本</span>
        <span>市值 TWD</span>
        <span>配息</span>
        <span>週配息 TWD</span>
        <span>操作</span>
      </div>
      {holdings.length === 0 ? (
        <p className="nexus-empty-line">尚無持股資料</p>
      ) : (
        holdings.map((holding, index) => (
          <div
            key={`${holding.symbol || 'holding'}:${index}`}
            className="nexus-holding-row"
          >
            <div className="nexus-holding-identity">
              <strong>{holding.symbol || '-'}</strong>
              <span>{holding.name || '-'}</span>
              {holding.fund_identity_status ? (
                <small>
                  {holding.fund_identity_status === 'confirmed'
                    ? `已辨識 ${holding.fund_quote_symbol || ''}`
                    : holding.fund_identity_status === 'suggested'
                      ? `待確認 ${holding.fund_candidate_symbol || ''}`
                      : '基金代號待補'}
                </small>
              ) : null}
              <small>
                {investmentCodeLabel(holding.market)} · {holding.currency || '-'}
              </small>
            </div>
            <span>{formatInvestmentNumber(holding.quantity, 4)}</span>
            <span>
              {holding.currency || '-'} {formatInvestmentNumber(holding.average_cost, 4)}
            </span>
            <span>
              NT${' '}
              {formatInvestmentNumber(
                holding.web_current_value_twd ?? holding.current_value_twd,
                2
              )}
            </span>
            <span>
              {holding.dividend_frequency_label || '待同步'}
              {holding.dividend_frequency_source === 'manual' ? ' · 手動' : ''}
            </span>
            <span>
              NT${' '}
              {formatInvestmentNumber(holding.estimated_weekly_dividend_twd, 2)}
            </span>
            <div className="nexus-holding-row-actions">
              <button type="button" onClick={() => onEdit(holding)} disabled={busy}>
                編輯
              </button>
              <button
                type="button"
                className="nexus-danger"
                onClick={() => onDelete(holding)}
                disabled={busy}
              >
                刪除
              </button>
            </div>
          </div>
        ))
      )}
    </div>
  )
}
