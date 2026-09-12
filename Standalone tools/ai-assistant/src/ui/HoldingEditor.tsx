import { useEffect, useRef, type Dispatch, type FormEvent, type SetStateAction } from 'react'
import type { HoldingDraft } from './aiAssistantDefinitions'

type HoldingEditorProps = {
  open: boolean
  holdingDraft: HoldingDraft
  setHoldingDraft: Dispatch<SetStateAction<HoldingDraft>>
  busyAction: string
  saveHolding: (event: FormEvent<HTMLFormElement>) => void | Promise<void>
  setHoldingEditorOpen: Dispatch<SetStateAction<boolean>>
}

export function HoldingEditor(props: HoldingEditorProps) {
  const {
    open,
    holdingDraft,
    setHoldingDraft,
    busyAction,
    saveHolding,
    setHoldingEditorOpen,
  } = props
  const firstFieldRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    window.requestAnimationFrame(() => firstFieldRef.current?.focus())
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busyAction) {
        setHoldingEditorOpen(false)
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [open, busyAction, setHoldingEditorOpen])

  if (!open) return null

  return (
    <div
      className="nexus-holding-editor-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="holding-editor-title"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busyAction) {
          setHoldingEditorOpen(false)
        }
      }}
    >
    <form
                className="nexus-holding-editor nexus-holding-editor--dialog"
                onSubmit={(event) => void saveHolding(event)}
              >
                <div className="nexus-holding-editor-head">
                  <strong id="holding-editor-title">{holdingDraft.holding_id ? '修改持股' : '新增持股'}</strong>
                  <span>本機修訂 · 不回寫來源檔</span>
                </div>
                <div className="nexus-holding-editor-grid">
                  <label>
                    代號
                    <input
                      ref={firstFieldRef}
                      required
                      value={holdingDraft.symbol}
                      onChange={(event) =>
                        setHoldingDraft({
                          ...holdingDraft,
                          symbol: event.target.value.toUpperCase(),
                        })
                      }
                    />
                  </label>
                  <label>
                    名稱
                    <input
                      value={holdingDraft.name}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, name: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    市場
                    <select
                      value={holdingDraft.market}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, market: event.target.value })
                      }
                    >
                      <option value="TW">台灣</option>
                      <option value="US">美國</option>
                      <option value="HK">香港</option>
                      <option value="FUND">共同基金</option>
                      <option value="CRYPTO">加密資產</option>
                      <option value="OTHER">其他</option>
                    </select>
                  </label>
                  <label>
                    資產類型
                    <select
                      value={holdingDraft.asset_type}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, asset_type: event.target.value })
                      }
                    >
                      <option value="STOCK">股票</option>
                      <option value="ETF">ETF</option>
                      <option value="FUND">共同基金</option>
                      <option value="BOND">債券</option>
                      <option value="CRYPTO">加密資產</option>
                      <option value="OTHER">其他</option>
                    </select>
                  </label>
                  {holdingDraft.asset_type === 'FUND' ||
                  holdingDraft.market === 'FUND' ? (
                    <>
                      <label>
                        基金代碼
                        <input
                          value={holdingDraft.fund_code}
                          onChange={(event) =>
                            setHoldingDraft({
                              ...holdingDraft,
                              fund_code: event.target.value.toUpperCase(),
                            })
                          }
                        />
                      </label>
                      <label>
                        ISIN
                        <input
                          value={holdingDraft.fund_isin}
                          onChange={(event) =>
                            setHoldingDraft({
                              ...holdingDraft,
                              fund_isin: event.target.value.toUpperCase(),
                            })
                          }
                        />
                      </label>
                      <label>
                        級別
                        <input
                          value={holdingDraft.fund_share_class}
                          onChange={(event) =>
                            setHoldingDraft({
                              ...holdingDraft,
                              fund_share_class: event.target.value,
                            })
                          }
                        />
                      </label>
                      <label>
                        網路報價代號
                        <input
                          value={holdingDraft.fund_quote_symbol}
                          onChange={(event) =>
                            setHoldingDraft({
                              ...holdingDraft,
                              fund_quote_symbol: event.target.value.toUpperCase(),
                            })
                          }
                        />
                      </label>
                    </>
                  ) : null}
                  <label>
                    數量
                    <input
                      required
                      type="number"
                      min="0"
                      step="any"
                      value={holdingDraft.quantity}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, quantity: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    平均成本
                    <input
                      required
                      type="number"
                      min="0"
                      step="any"
                      value={holdingDraft.average_cost}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, average_cost: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    幣別
                    <input
                      required
                      value={holdingDraft.currency}
                      onChange={(event) =>
                        setHoldingDraft({
                          ...holdingDraft,
                          currency: event.target.value.toUpperCase(),
                        })
                      }
                    />
                  </label>
                  <label>
                    本金幣別
                    <input
                      required
                      value={holdingDraft.principal_currency}
                      onChange={(event) =>
                        setHoldingDraft({
                          ...holdingDraft,
                          principal_currency: event.target.value.toUpperCase(),
                        })
                      }
                    />
                  </label>
                  <label>
                    配息頻率
                    <select
                      value={holdingDraft.dividend_frequency}
                      onChange={(event) =>
                        setHoldingDraft({
                          ...holdingDraft,
                          dividend_frequency: event.target.value,
                        })
                      }
                    >
                      <option value="unknown">待確認</option>
                      <option value="none">無配息</option>
                      <option value="weekly">每週</option>
                      <option value="biweekly">每兩週</option>
                      <option value="monthly">每月</option>
                      <option value="bimonthly">每兩月</option>
                      <option value="quarterly">每季</option>
                      <option value="semiannual">每半年</option>
                      <option value="annual">每年</option>
                      <option value="irregular">不定期</option>
                    </select>
                  </label>
                  {(
                    [
                      ['principal_amount', '原始本金'],
                      ['principal_twd', '本金 TWD'],
                      ['current_value_twd', '目前市值 TWD'],
                      ['dividend_amount_twd', '配息金額 TWD'],
                      ['dividend_per_unit', '單位配息'],
                      ['monthly_dividend_twd', '每月配息 TWD'],
                      ['annual_dividend_yield_percent', '年化配息率 %'],
                      ['payback_rate_percent', '回本率 %'],
                    ] as const
                  ).map(([key, label]) => (
                    <label key={key}>
                      {label}
                      <input
                        type="number"
                        min="0"
                        step="any"
                        value={holdingDraft[key]}
                        onChange={(event) =>
                          setHoldingDraft({
                            ...holdingDraft,
                            [key]: event.target.value,
                          })
                        }
                      />
                    </label>
                  ))}
                </div>
                <div className="nexus-holding-editor-actions">
                  <button
                    type="button"
                    onClick={() => setHoldingEditorOpen(false)}
                    disabled={Boolean(busyAction)}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className="nexus-primary"
                    disabled={Boolean(busyAction)}
                  >
                    儲存持股
                  </button>
                </div>
              </form>
    </div>
  )
}
