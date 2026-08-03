import type { Dispatch, FormEvent, SetStateAction } from 'react'
import {
  formatInvestmentNumber,
  investmentCodeLabel,
  type ExcelHorizontalSheetPreview,
  type ExcelMappingPreview,
  type ExcelMappingSheetPreview,
} from './investmentWatchFeature'
import {
  CONSOLIDATED_COLUMN_FIELDS,
  CONSOLIDATED_ROW_FIELDS,
  EXCEL_COLUMN_OPTIONS,
  EXCEL_MAPPING_FIELDS,
  excelColumnLetter,
  type ExcelColumnMapping,
  type ExcelConsolidatedDraft,
  type ExcelHorizontalSheetDraft,
  type ExcelLayoutMode,
} from './aiAssistantDefinitions'

type ExcelMappingEditorProps = {
  open: boolean
  excelMappingPreview: ExcelMappingPreview | null
  selectedExcelSheet: ExcelMappingSheetPreview | null
  excelLayoutMode: ExcelLayoutMode
  setExcelLayoutMode: Dispatch<SetStateAction<ExcelLayoutMode>>
  excelConsolidatedDraft: ExcelConsolidatedDraft
  setExcelConsolidatedDraft: Dispatch<SetStateAction<ExcelConsolidatedDraft>>
  excelHorizontalSheets: ExcelHorizontalSheetDraft[]
  selectedHorizontalSheets: ExcelHorizontalSheetDraft[]
  horizontalHoldingCount: number
  horizontalSampleHoldings: ExcelHorizontalSheetPreview['sample_holdings']
  updateHorizontalSheet: (
    sheetName: string,
    changes: Partial<ExcelHorizontalSheetDraft>
  ) => void
  excelHeaderRow: number
  setExcelHeaderRow: Dispatch<SetStateAction<number>>
  excelDataStartRow: number
  setExcelDataStartRow: Dispatch<SetStateAction<number>>
  excelColumnMapping: ExcelColumnMapping
  setExcelColumnMapping: Dispatch<SetStateAction<ExcelColumnMapping>>
  excelHeaderValues: string[]
  mappedExcelPreviewRows: string[][]
  busyAction: string
  message: string
  importExcelWithMapping: (event: FormEvent<HTMLFormElement>) => void | Promise<void>
  applySmartExcelRepair: () => void
  selectExcelSheet: (
    preview: ExcelMappingPreview,
    sheet: ExcelMappingSheetPreview,
    preferSaved: boolean
  ) => void
  openExcelMapper: (chooseNewFile: boolean) => void | Promise<void>
  setExcelMapperOpen: Dispatch<SetStateAction<boolean>>
}

export function ExcelMappingEditor(props: ExcelMappingEditorProps) {
  const {
    open,
    excelMappingPreview,
    selectedExcelSheet,
    excelLayoutMode,
    setExcelLayoutMode,
    excelConsolidatedDraft,
    setExcelConsolidatedDraft,
    excelHorizontalSheets,
    selectedHorizontalSheets,
    horizontalHoldingCount,
    horizontalSampleHoldings,
    updateHorizontalSheet,
    excelHeaderRow,
    setExcelHeaderRow,
    excelDataStartRow,
    setExcelDataStartRow,
    excelColumnMapping,
    setExcelColumnMapping,
    excelHeaderValues,
    mappedExcelPreviewRows,
    busyAction,
    message,
    importExcelWithMapping,
    applySmartExcelRepair,
    selectExcelSheet,
    openExcelMapper,
    setExcelMapperOpen,
  } = props

  if (!open || !excelMappingPreview || !selectedExcelSheet) return null

  return (
    <form
                className="nexus-excel-mapper"
                onSubmit={(event) => void importExcelWithMapping(event)}
              >
                <div className="nexus-excel-mapper-head">
                  <div>
                    <strong>Excel 欄位設定</strong>
                    <span>{excelMappingPreview.file_name}</span>
                  </div>
                  <span className="nexus-readonly-state">來源唯讀</span>
                </div>
                {excelMappingPreview.smart_repair ? (
                  <div className="nexus-smart-repair">
                    <div>
                      <strong>
                        智慧修復 · {excelMappingPreview.smart_repair.confidence_label || '低'}
                        {' '}{excelMappingPreview.smart_repair.confidence_score || 0}%
                      </strong>
                      <span>{excelMappingPreview.smart_repair.reason}</span>
                    </div>
                    <button
                      type="button"
                      onClick={applySmartExcelRepair}
                      disabled={!excelMappingPreview.smart_repair.recommended}
                    >
                      套用建議
                    </button>
                  </div>
                ) : null}
                <div className="nexus-excel-layout-tabs" role="tablist">
                  {excelMappingPreview.consolidated_layout?.detected ? (
                    <button
                      type="button"
                      className={
                        excelLayoutMode === 'consolidated_report'
                          ? 'is-active'
                          : ''
                      }
                      onClick={() => setExcelLayoutMode('consolidated_report')}
                    >
                      報酬總表 · {excelMappingPreview.consolidated_layout.holding_count || 0}
                    </button>
                  ) : null}
                  {excelMappingPreview.horizontal_layout?.detected ? (
                    <button
                      type="button"
                      className={
                        excelLayoutMode === 'horizontal_matrix'
                          ? 'is-active'
                          : ''
                      }
                      onClick={() => setExcelLayoutMode('horizontal_matrix')}
                    >
                      多工作表
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className={
                      excelLayoutMode === 'row_mapping' ? 'is-active' : ''
                    }
                    onClick={() => setExcelLayoutMode('row_mapping')}
                  >
                    一般欄位
                  </button>
                </div>
                {excelLayoutMode === 'consolidated_report' &&
                excelMappingPreview.consolidated_layout ? (
                  <>
                    <div className="nexus-consolidated-summary">
                      <span>
                        全部資料
                        <strong>
                          {excelMappingPreview.consolidated_layout.holding_count || 0}
                        </strong>
                      </span>
                      <span>
                        共同基金
                        <strong>
                          {excelMappingPreview.consolidated_layout.fund_count || 0}
                        </strong>
                      </span>
                      <span>
                        台股 / ETF
                        <strong>
                          {excelMappingPreview.consolidated_layout.tw_count || 0}
                        </strong>
                      </span>
                      <span>
                        美股 / ETF
                        <strong>
                          {excelMappingPreview.consolidated_layout.us_count || 0}
                        </strong>
                      </span>
                      <span>
                        預估週配息
                        <strong>TWD</strong>
                      </span>
                    </div>
                    <div className="nexus-excel-source-grid nexus-excel-source-grid--report">
                      <label>
                        工作表
                        <select
                          value={excelConsolidatedDraft.sheet_name}
                          onChange={(event) =>
                            setExcelConsolidatedDraft({
                              ...excelConsolidatedDraft,
                              sheet_name: event.target.value,
                            })
                          }
                        >
                          {excelMappingPreview.sheets.map((sheet) => (
                            <option key={sheet.sheet_name} value={sheet.sheet_name}>
                              {sheet.sheet_name}
                            </option>
                          ))}
                        </select>
                      </label>
                      {CONSOLIDATED_ROW_FIELDS.map((field) => (
                        <label key={field.key}>
                          {field.label}
                          <input
                            type="number"
                            min="1"
                            value={excelConsolidatedDraft[field.key]}
                            onChange={(event) =>
                              setExcelConsolidatedDraft({
                                ...excelConsolidatedDraft,
                                [field.key]: event.target.value,
                              })
                            }
                          />
                        </label>
                      ))}
                      <label className="nexus-check-field">
                        <input
                          type="checkbox"
                          checked={excelConsolidatedDraft.include_zero_quantity}
                          onChange={(event) =>
                            setExcelConsolidatedDraft({
                              ...excelConsolidatedDraft,
                              include_zero_quantity: event.target.checked,
                            })
                          }
                        />
                        顯示零持有量
                      </label>
                    </div>
                    <div className="nexus-excel-mapping-grid nexus-excel-mapping-grid--report">
                      {CONSOLIDATED_COLUMN_FIELDS.map((field) => (
                        <label key={field.key}>
                          <span>{field.label}</span>
                          <select
                            value={excelConsolidatedDraft[field.key]}
                            onChange={(event) =>
                              setExcelConsolidatedDraft({
                                ...excelConsolidatedDraft,
                                [field.key]: event.target.value,
                              })
                            }
                          >
                            {EXCEL_COLUMN_OPTIONS.map((index) => (
                              <option key={index} value={String(index)}>
                                {excelColumnLetter(index)}
                              </option>
                            ))}
                          </select>
                        </label>
                      ))}
                    </div>
                    <div className="nexus-asset-defaults-grid">
                      {(
                        [
                          ['fund', '共同基金'],
                          ['tw', '台股 / ETF'],
                          ['us', '美股 / ETF'],
                        ] as const
                      ).map(([prefix, label]) => (
                        <div key={prefix}>
                          <strong>{label}</strong>
                          <label>
                            市場
                            <select
                              value={excelConsolidatedDraft[`${prefix}_market`]}
                              onChange={(event) =>
                                setExcelConsolidatedDraft({
                                  ...excelConsolidatedDraft,
                                  [`${prefix}_market`]: event.target.value,
                                })
                              }
                            >
                              <option value="FUND">共同基金</option>
                              <option value="TW">台灣</option>
                              <option value="US">美國</option>
                              <option value="OTHER">其他</option>
                            </select>
                          </label>
                          <label>
                            類型
                            <select
                              value={excelConsolidatedDraft[`${prefix}_asset_type`]}
                              onChange={(event) =>
                                setExcelConsolidatedDraft({
                                  ...excelConsolidatedDraft,
                                  [`${prefix}_asset_type`]: event.target.value,
                                })
                              }
                            >
                              <option value="AUTO">自動</option>
                              <option value="FUND">共同基金</option>
                              <option value="ETF">ETF</option>
                              <option value="STOCK">股票</option>
                            </select>
                          </label>
                          <label>
                            標的幣別
                            <select
                              value={excelConsolidatedDraft[`${prefix}_currency`]}
                              onChange={(event) =>
                                setExcelConsolidatedDraft({
                                  ...excelConsolidatedDraft,
                                  [`${prefix}_currency`]: event.target.value,
                                })
                              }
                            >
                              <option value="TWD">TWD</option>
                              <option value="USD">USD</option>
                              <option value="JPY">JPY</option>
                              <option value="EUR">EUR</option>
                            </select>
                          </label>
                          <label>
                            本金幣別
                            <select
                              value={
                                excelConsolidatedDraft[
                                  `${prefix}_principal_currency`
                                ]
                              }
                              onChange={(event) =>
                                setExcelConsolidatedDraft({
                                  ...excelConsolidatedDraft,
                                  [`${prefix}_principal_currency`]:
                                    event.target.value,
                                })
                              }
                            >
                              <option value="TWD">TWD</option>
                              <option value="USD">USD</option>
                              <option value="JPY">JPY</option>
                              <option value="EUR">EUR</option>
                              <option value="AUTO">同標的幣別</option>
                            </select>
                          </label>
                        </div>
                      ))}
                    </div>
                    <div className="nexus-excel-preview">
                      <div className="nexus-excel-preview-head">
                        <strong>報酬總表預覽</strong>
                        <span>顯示 TWD · 配息換算每週</span>
                      </div>
                      <div className="nexus-report-preview-table">
                        <div className="nexus-report-preview-row nexus-report-preview-row--head">
                          <span>代號 / 名稱</span>
                          <span>類別</span>
                          <span>數量</span>
                          <span>年化配息率</span>
                          <span>預估週配息</span>
                        </div>
                        {(excelMappingPreview.consolidated_layout.sample_holdings || []).map(
                          (holding, index) => (
                            <div className="nexus-report-preview-row" key={`${holding.symbol}:${index}`}>
                              <span>{holding.symbol || holding.name || '-'}</span>
                              <span>
                                {investmentCodeLabel(holding.asset_type || holding.market)}
                              </span>
                              <span>{formatInvestmentNumber(holding.quantity, 3)}</span>
                              <span>
                                {formatInvestmentNumber(
                                  holding.annual_dividend_yield_percent,
                                  2
                                )}%
                              </span>
                              <span>
                                NT${' '}
                                {formatInvestmentNumber(
                                  holding.estimated_weekly_dividend_twd,
                                  2
                                )}
                              </span>
                            </div>
                          )
                        )}
                      </div>
                    </div>
                  </>
                ) : excelLayoutMode === 'horizontal_matrix' ? (
                  <>
                    <div className="nexus-horizontal-sheet-list">
                      {excelHorizontalSheets.map((sheet) => (
                        <div
                          className={`nexus-horizontal-sheet${sheet.enabled ? ' is-selected' : ''}`}
                          key={sheet.sheet_name}
                        >
                          <label className="nexus-horizontal-sheet-toggle">
                            <input
                              type="checkbox"
                              checked={sheet.enabled}
                              onChange={(event) =>
                                updateHorizontalSheet(sheet.sheet_name, {
                                  enabled: event.target.checked,
                                })
                              }
                            />
                            <span>
                              <strong>{sheet.sheet_name}</strong>
                              {sheet.category_label} · {sheet.holding_count} 筆
                            </span>
                          </label>
                          <div className="nexus-horizontal-sheet-fields">
                            <label>
                              市場
                              <select
                                value={sheet.market}
                                onChange={(event) =>
                                  updateHorizontalSheet(sheet.sheet_name, {
                                    market: event.target.value,
                                  })
                                }
                              >
                                <option value="TW">台灣</option>
                                <option value="US">美國</option>
                                <option value="FUND">共同基金</option>
                                <option value="OTHER">其他</option>
                              </select>
                            </label>
                            <label>
                              類型
                              <select
                                value={sheet.asset_type}
                                onChange={(event) =>
                                  updateHorizontalSheet(sheet.sheet_name, {
                                    asset_type: event.target.value,
                                  })
                                }
                              >
                                <option value="AUTO">自動</option>
                                <option value="ETF">ETF</option>
                                <option value="STOCK">股票</option>
                                <option value="FUND">共同基金</option>
                              </select>
                            </label>
                            <label>
                              幣別
                              <select
                                value={sheet.currency}
                                onChange={(event) =>
                                  updateHorizontalSheet(sheet.sheet_name, {
                                    currency: event.target.value,
                                  })
                                }
                              >
                                <option value="TWD">TWD</option>
                                <option value="USD">USD</option>
                                <option value="JPY">JPY</option>
                                <option value="EUR">EUR</option>
                              </select>
                            </label>
                            {(
                              [
                                ['header_row_number', '標題列'],
                                ['holding_row_number', '持有列'],
                                ['price_row_number', '價格列'],
                                ['quantity_row_number', '數量列'],
                                ['group_width', '每組欄寬'],
                              ] as const
                            ).map(([key, label]) => (
                              <label key={key}>
                                {label}
                                <input
                                  type="number"
                                  min={key === 'group_width' ? 2 : 1}
                                  value={sheet[key]}
                                  onChange={(event) =>
                                    updateHorizontalSheet(sheet.sheet_name, {
                                      [key]: event.target.value,
                                    })
                                  }
                                />
                              </label>
                            ))}
                            <label>
                              起始欄
                              <select
                                value={sheet.first_asset_column_index}
                                onChange={(event) =>
                                  updateHorizontalSheet(sheet.sheet_name, {
                                    first_asset_column_index: event.target.value,
                                  })
                                }
                              >
                                {EXCEL_COLUMN_OPTIONS.map((index) => (
                                  <option key={index} value={String(index)}>
                                    {excelColumnLetter(index)}
                                  </option>
                                ))}
                              </select>
                            </label>
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="nexus-excel-preview">
                      <div className="nexus-excel-preview-head">
                        <strong>合併預覽</strong>
                        <span>
                          {selectedHorizontalSheets.length} 張 · {horizontalHoldingCount} 筆
                        </span>
                      </div>
                      <div className="nexus-report-preview-table">
                        {horizontalSampleHoldings.map((holding, index) => (
                          <div className="nexus-report-preview-row" key={`${holding.source_sheet}:${holding.symbol}:${index}`}>
                            <span>{holding.symbol || holding.name || '-'}</span>
                            <span>{holding.source_sheet}</span>
                            <span>{investmentCodeLabel(holding.asset_type)}</span>
                            <span>{formatInvestmentNumber(holding.quantity, 3)}</span>
                            <span>{holding.currency || 'TWD'}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </>
                ) : (
                  <>
                <div className="nexus-excel-source-grid">
                  <label>
                    工作表
                    <select
                      value={selectedExcelSheet.sheet_name}
                      onChange={(event) => {
                        const sheet = excelMappingPreview.sheets.find(
                          (item) => item.sheet_name === event.target.value
                        )
                        if (sheet) selectExcelSheet(excelMappingPreview, sheet, true)
                      }}
                    >
                      {excelMappingPreview.sheets.map((sheet) => (
                        <option key={sheet.sheet_name} value={sheet.sheet_name}>
                          {sheet.sheet_name} · {sheet.row_count} 列
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    標題列
                    <input
                      type="number"
                      min="1"
                      max={Math.max(1, selectedExcelSheet.preview_row_count)}
                      value={excelHeaderRow}
                      onChange={(event) => {
                        const rowNumber = Math.max(
                          1,
                          Number(event.target.value) || 1
                        )
                        setExcelHeaderRow(rowNumber)
                        setExcelDataStartRow(rowNumber + 1)
                      }}
                    />
                  </label>
                  <label>
                    資料起始列
                    <input
                      type="number"
                      min="1"
                      max={Math.max(1, selectedExcelSheet.preview_row_count)}
                      value={excelDataStartRow}
                      onChange={(event) =>
                        setExcelDataStartRow(
                          Math.max(1, Number(event.target.value) || 1)
                        )
                      }
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() =>
                      selectExcelSheet(
                        excelMappingPreview,
                        selectedExcelSheet,
                        false
                      )
                    }
                  >
                    套用自動建議
                  </button>
                  <button
                    type="button"
                    onClick={() => void openExcelMapper(true)}
                    disabled={Boolean(busyAction)}
                  >
                    選擇其他 Excel
                  </button>
                </div>
                <div className="nexus-excel-mapping-grid">
                  {EXCEL_MAPPING_FIELDS.map((field) => (
                    <label key={field.key}>
                      <span>
                        {field.label}
                        {field.required ? <em>必要</em> : null}
                      </span>
                      <select
                        required={field.required}
                        value={excelColumnMapping[field.key] ?? ''}
                        onChange={(event) =>
                          setExcelColumnMapping({
                            ...excelColumnMapping,
                            [field.key]: event.target.value,
                          })
                        }
                      >
                        <option value="">不讀取</option>
                        {excelHeaderValues.map((header, index) => (
                          <option key={`${field.key}:${index}`} value={String(index)}>
                            {excelColumnLetter(index)} · {header || '空白欄'}
                          </option>
                        ))}
                      </select>
                    </label>
                  ))}
                </div>
                <div className="nexus-excel-preview">
                  <div className="nexus-excel-preview-head">
                    <strong>映射預覽</strong>
                    <span>第 {excelDataStartRow} 列起</span>
                  </div>
                  <div className="nexus-excel-preview-table">
                    <div className="nexus-excel-preview-row nexus-excel-preview-row--head">
                      {EXCEL_MAPPING_FIELDS.map((field) => (
                        <span key={field.key}>{field.label}</span>
                      ))}
                    </div>
                    {mappedExcelPreviewRows.length > 0 ? (
                      mappedExcelPreviewRows.map((row, rowIndex) => (
                        <div
                          className="nexus-excel-preview-row"
                          key={`${selectedExcelSheet.sheet_name}:${excelDataStartRow}:${rowIndex}`}
                        >
                          {EXCEL_MAPPING_FIELDS.map((field) => {
                            const columnIndex = excelColumnMapping[field.key]
                            return (
                              <span key={field.key}>
                                {columnIndex === undefined || columnIndex === ''
                                  ? '-'
                                  : row[Number(columnIndex)] || '-'}
                              </span>
                            )
                          })}
                        </div>
                      ))
                    ) : (
                      <p>此設定暫無可預覽資料</p>
                    )}
                  </div>
                </div>
                  </>
                )}
                <p className="nexus-excel-mapper-message">{message}</p>
                <div className="nexus-excel-mapper-actions">
                  <button
                    type="button"
                    onClick={() => setExcelMapperOpen(false)}
                    disabled={Boolean(busyAction)}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className="nexus-primary"
                    disabled={Boolean(busyAction)}
                  >
                    套用並重新匯入
                  </button>
                </div>
              </form>
  )
}
