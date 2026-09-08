import type { Dispatch, SetStateAction } from 'react'
import { HoldingEditor } from './HoldingEditor'
import { ExcelMappingEditor } from './ExcelMappingEditor'
import { HoldingTable } from './HoldingTable'
import type { ExcelMapper } from './excelMapper'
import type { HoldingDraft } from './aiAssistantDefinitions'
import type {
  InvestmentHolding,
  InvestmentResult,
  WorkbookScanQuality,
  WorkbookSheetScan,
} from './investmentWatchFeature'

type RunV2Command = (
  command: string,
  payload: Record<string, unknown>,
  label: string,
  timeoutMs?: number,
  requireHoldings?: boolean
) => Promise<InvestmentResult | null>

type PortfolioWorkspaceProps = {
  holdingCount: number
  busyAction: string
  holdings: InvestmentHolding[]
  workbookScanLabel: string
  selectedWorkbookSheet: WorkbookSheetScan | null
  workbookQuality: WorkbookScanQuality | null | undefined
  excel: ExcelMapper
  holdingEditorOpen: boolean
  setHoldingEditorOpen: Dispatch<SetStateAction<boolean>>
  holdingDraft: HoldingDraft
  setHoldingDraft: Dispatch<SetStateAction<HoldingDraft>>
  saveHolding: (event: React.FormEvent) => Promise<void>
  deleteHolding: (holding: InvestmentHolding) => Promise<void>
  openNewHolding: () => void
  openHoldingEditor: (holding: InvestmentHolding) => void
  runCommand: RunV2Command
  message: string
}

export function PortfolioWorkspace(props: PortfolioWorkspaceProps) {
  const {
    holdingCount,
    busyAction,
    holdings,
    workbookScanLabel,
    selectedWorkbookSheet,
    workbookQuality,
    excel,
    holdingEditorOpen,
    setHoldingEditorOpen,
    holdingDraft,
    setHoldingDraft,
    saveHolding,
    deleteHolding,
    openNewHolding,
    openHoldingEditor,
    runCommand,
    message,
  } = props
  const busy = Boolean(busyAction)

  return (
    <section className="nexus-column nexus-column--main">
      <section className="nexus-surface nexus-holdings-surface">
        <div className="nexus-section-head">
          <span>持股資料</span>
          <div className="nexus-holding-head-actions">
            <strong>{holdingCount}</strong>
            <button
              type="button"
              className="nexus-toggle-button"
              onClick={() => void excel.openExcelMapper(false)}
              disabled={busy}
            >
              欄位設定
            </button>
            <button
              type="button"
              className="nexus-toggle-button"
              onClick={() =>
                void runCommand(
                  'investment_watch_sync_dividends',
                  {},
                  'AI投資管家配息搜尋',
                  240000
                )
              }
              disabled={busy}
            >
              {busyAction === 'investment:investment_watch_sync_dividends'
                ? '同步中...'
                : 'AI投資管家搜尋配息'}
            </button>
            <button
              type="button"
              className="nexus-toggle-button"
              title="依基金名稱搜尋基金代號、級別與可用報價代號"
              onClick={() =>
                void runCommand(
                  'investment_watch_resolve_fund_identities',
                  { limit: 80 },
                  '共同基金辨識',
                  240000,
                  true
                )
              }
              disabled={busy}
            >
              {busyAction ===
              'investment:investment_watch_resolve_fund_identities'
                ? '辨識中...'
                : '辨識基金'}
            </button>
            <button
              type="button"
              className="nexus-toggle-button"
              onClick={openNewHolding}
              disabled={busy}
            >
              新增持股
            </button>
          </div>
        </div>
        <p className="nexus-scan-note">
          {workbookScanLabel}
          {selectedWorkbookSheet?.header_mode
            ? ` · ${selectedWorkbookSheet.header_mode}`
            : ''}
        </p>
        {workbookQuality ? (
          <div className="nexus-scan-quality">
            <span>
              掃描品質
              <strong>{workbookQuality.state_label || '-'}</strong>
            </span>
            <span>
              分數
              <strong>{workbookQuality.score ?? '-'}</strong>
            </span>
            <span>
              有效資料
              <strong>{workbookQuality.valid_data_row_count ?? '-'}</strong>
            </span>
            <span>
              表頭深度
              <strong>{workbookQuality.header_depth || 1} 層</strong>
            </span>
            <p>{workbookQuality.recommendation}</p>
          </div>
        ) : null}
        <ExcelMappingEditor
          open={excel.excelMapperOpen}
          excelMappingPreview={excel.excelMappingPreview}
          selectedExcelSheet={excel.selectedExcelSheet}
          excelLayoutMode={excel.excelLayoutMode}
          setExcelLayoutMode={excel.setExcelLayoutMode}
          excelConsolidatedDraft={excel.excelConsolidatedDraft}
          setExcelConsolidatedDraft={excel.setExcelConsolidatedDraft}
          excelHorizontalSheets={excel.excelHorizontalSheets}
          selectedHorizontalSheets={excel.selectedHorizontalSheets}
          horizontalHoldingCount={excel.horizontalHoldingCount}
          horizontalSampleHoldings={excel.horizontalSampleHoldings}
          updateHorizontalSheet={excel.updateHorizontalSheet}
          excelHeaderRow={excel.excelHeaderRow}
          setExcelHeaderRow={excel.setExcelHeaderRow}
          excelDataStartRow={excel.excelDataStartRow}
          setExcelDataStartRow={excel.setExcelDataStartRow}
          excelColumnMapping={excel.excelColumnMapping}
          setExcelColumnMapping={excel.setExcelColumnMapping}
          excelHeaderValues={excel.excelHeaderValues}
          mappedExcelPreviewRows={excel.mappedExcelPreviewRows}
          busyAction={busyAction}
          message={message}
          importExcelWithMapping={excel.importExcelWithMapping}
          applySmartExcelRepair={excel.applySmartExcelRepair}
          selectExcelSheet={excel.selectExcelSheet}
          openExcelMapper={excel.openExcelMapper}
          setExcelMapperOpen={excel.setExcelMapperOpen}
        />
        <HoldingEditor
          open={holdingEditorOpen}
          holdingDraft={holdingDraft}
          setHoldingDraft={setHoldingDraft}
          busyAction={busyAction}
          saveHolding={saveHolding}
          setHoldingEditorOpen={setHoldingEditorOpen}
        />
        <HoldingTable
          holdings={holdings}
          busyAction={busyAction}
          onEdit={openHoldingEditor}
          onDelete={deleteHolding}
        />
      </section>
    </section>
  )
}
