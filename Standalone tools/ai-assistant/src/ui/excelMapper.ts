import { useCallback, useMemo, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import {
  CONSOLIDATED_COLUMN_FIELDS,
  CONSOLIDATED_ROW_FIELDS,
  EMPTY_CONSOLIDATED_DRAFT,
  EXCEL_MAPPING_FIELDS,
  type ExcelColumnMapping,
  type ExcelConsolidatedDraft,
  type ExcelHorizontalSheetDraft,
  type ExcelLayoutMode,
} from './aiAssistantDefinitions'
import {
  openInvestmentFile,
  type ExcelHorizontalHoldingPreview,
  type ExcelImportProfile,
  type ExcelMappingPreview,
  type ExcelMappingSheetPreview,
  type InvestmentResult,
} from './investmentWatchFeature'

type RunV2Command = (
  command: string,
  payload: Record<string, unknown>,
  label: string,
  timeoutMs?: number,
  requireHoldings?: boolean
) => Promise<InvestmentResult | null>

type SetMessage = (message: string) => void

export type ExcelMapper = {
  excelMapperOpen: boolean
  setExcelMapperOpen: Dispatch<SetStateAction<boolean>>
  excelMappingPreview: ExcelMappingPreview | null
  excelSheetName: string
  excelLayoutMode: ExcelLayoutMode
  setExcelLayoutMode: Dispatch<SetStateAction<ExcelLayoutMode>>
  excelHorizontalSheets: ExcelHorizontalSheetDraft[]
  excelConsolidatedDraft: ExcelConsolidatedDraft
  setExcelConsolidatedDraft: Dispatch<SetStateAction<ExcelConsolidatedDraft>>
  selectedExcelSheet: ExcelMappingSheetPreview | null
  excelHeaderRow: number
  setExcelHeaderRow: Dispatch<SetStateAction<number>>
  excelDataStartRow: number
  setExcelDataStartRow: Dispatch<SetStateAction<number>>
  excelColumnMapping: ExcelColumnMapping
  setExcelColumnMapping: Dispatch<SetStateAction<ExcelColumnMapping>>
  excelHeaderValues: string[]
  mappedExcelPreviewRows: string[][]
  selectedHorizontalSheets: ExcelHorizontalSheetDraft[]
  horizontalHoldingCount: number
  horizontalSampleHoldings: ExcelHorizontalSheetPreviewSample[]
  updateHorizontalSheet: (
    sheetName: string,
    changes: Partial<ExcelHorizontalSheetDraft>
  ) => void
  importExcelWithMapping: (event: React.FormEvent) => Promise<void>
  applySmartExcelRepair: () => void
  selectExcelSheet: (
    preview: ExcelMappingPreview,
    sheet: ExcelMappingSheetPreview,
    preferSaved: boolean
  ) => void
  openExcelMapper: (chooseNewFile: boolean) => Promise<void>
}

type ExcelHorizontalSheetPreviewSample = ExcelHorizontalHoldingPreview & {
  source_sheet: string
}

type ExcelMapperOptions = {
  runCommand: RunV2Command
  excelImportProfile: ExcelImportProfile | null | undefined
  portfolioSourcePath: string
  setMessage: SetMessage
}

const CONSOLIDATED_NUMERIC_FIELDS: Array<keyof ExcelConsolidatedDraft> = [
  'fund_start_row_number',
  'security_start_row_number',
  'data_end_row_number',
  'fund_name_column_index',
  'tw_symbol_column_index',
  'tw_name_column_index',
  'us_symbol_column_index',
  'us_name_column_index',
  'quantity_column_index',
  'cost_amount_column_index',
  'price_column_index',
  'current_value_column_index',
  'dividend_amount_column_index',
  'dividend_per_unit_column_index',
  'monthly_dividend_column_index',
  'annual_dividend_yield_column_index',
  'payback_rate_column_index',
]

export function useExcelMapper({
  runCommand,
  excelImportProfile,
  portfolioSourcePath,
  setMessage,
}: ExcelMapperOptions): ExcelMapper {
  const [excelMapperOpen, setExcelMapperOpen] = useState(false)
  const [excelMappingPreview, setExcelMappingPreview] =
    useState<ExcelMappingPreview | null>(null)
  const [excelSheetName, setExcelSheetName] = useState('')
  const [excelHeaderRow, setExcelHeaderRow] = useState(1)
  const [excelDataStartRow, setExcelDataStartRow] = useState(2)
  const [excelColumnMapping, setExcelColumnMapping] =
    useState<ExcelColumnMapping>({})
  const [excelLayoutMode, setExcelLayoutMode] =
    useState<ExcelLayoutMode>('row_mapping')
  const [excelHorizontalSheets, setExcelHorizontalSheets] = useState<
    ExcelHorizontalSheetDraft[]
  >([])
  const [excelConsolidatedDraft, setExcelConsolidatedDraft] =
    useState<ExcelConsolidatedDraft>(EMPTY_CONSOLIDATED_DRAFT)

  const selectedExcelSheet = useMemo<ExcelMappingSheetPreview | null>(
    () =>
      excelMappingPreview?.sheets.find(
        (sheet) => sheet.sheet_name === excelSheetName
      ) ||
      excelMappingPreview?.sheets[0] ||
      null,
    [excelMappingPreview, excelSheetName]
  )

  const excelHeaderValues = useMemo(() => {
    const values = selectedExcelSheet?.rows[excelHeaderRow - 1] || []
    const width = Math.max(
      selectedExcelSheet?.column_count || 0,
      values.length
    )
    return Array.from({ length: width }, (_, index) => values[index] || '')
  }, [selectedExcelSheet, excelHeaderRow])

  const mappedExcelPreviewRows = useMemo(() => {
    if (!selectedExcelSheet) return []
    return selectedExcelSheet.rows
      .slice(Math.max(0, excelDataStartRow - 1))
      .filter((row) =>
        EXCEL_MAPPING_FIELDS.some(({ key }) => {
          const rawIndex = excelColumnMapping[key]
          if (rawIndex === undefined || rawIndex === '') return false
          return String(row[Number(rawIndex)] || '').trim().length > 0
        })
      )
      .slice(0, 6)
  }, [selectedExcelSheet, excelDataStartRow, excelColumnMapping])

  const selectedHorizontalSheets = useMemo(
    () => excelHorizontalSheets.filter((sheet) => sheet.enabled),
    [excelHorizontalSheets]
  )

  const horizontalHoldingCount = useMemo(
    () =>
      selectedHorizontalSheets.reduce(
        (total, sheet) => total + sheet.holding_count,
        0
      ),
    [selectedHorizontalSheets]
  )

  const horizontalSampleHoldings = useMemo(() => {
    const samples: ExcelHorizontalSheetPreviewSample[] = []
    for (const sheet of selectedHorizontalSheets) {
      for (const holding of sheet.sample_holdings || []) {
        samples.push({ ...holding, source_sheet: sheet.sheet_name })
      }
    }
    return samples.slice(0, 10)
  }, [selectedHorizontalSheets])

  const initializeConsolidatedReport = useCallback(
    (preview: ExcelMappingPreview) => {
      const detected = preview.consolidated_layout
      if (!detected?.detected) {
        setExcelConsolidatedDraft(EMPTY_CONSOLIDATED_DRAFT)
        return
      }
      const saved =
        excelImportProfile?.source_path === preview.source_path &&
        excelImportProfile?.layout === 'consolidated_report'
          ? (excelImportProfile as Record<string, unknown>)
          : null
      const detectedValues = detected as Record<string, unknown>
      const next = { ...EMPTY_CONSOLIDATED_DRAFT }
      for (const key of Object.keys(next) as Array<keyof ExcelConsolidatedDraft>) {
        const value = saved?.[key] ?? detectedValues[key] ?? next[key]
        if (key === 'include_zero_quantity') {
          next[key] = Boolean(value) as never
        } else {
          next[key] = String(value ?? '') as never
        }
      }
      setExcelConsolidatedDraft(next)
    },
    [excelImportProfile]
  )

  const initializeHorizontalSheets = useCallback(
    (preview: ExcelMappingPreview) => {
      const detected = preview.horizontal_layout?.sheets || []
      const savedSheets: Array<Record<string, unknown>> =
        excelImportProfile?.source_path === preview.source_path &&
        excelImportProfile?.layout === 'horizontal_matrix' &&
        Array.isArray(excelImportProfile.sheets)
          ? excelImportProfile.sheets
          : []
      setExcelHorizontalSheets(
        detected.map((sheet) => {
          const saved = savedSheets.find(
            (item) => String(item.sheet_name || '') === sheet.sheet_name
          )
          const value = (key: string, fallback: unknown) =>
            String(saved?.[key] ?? fallback ?? '')
          return {
            enabled:
              savedSheets.length > 0
                ? Boolean(saved)
                : Boolean(sheet.selected_by_default),
            sheet_name: sheet.sheet_name,
            category_label: value('category_label', sheet.category_label),
            market: value('market', sheet.market || 'TW'),
            asset_type: value('asset_type', sheet.asset_type || 'AUTO'),
            currency: value('currency', sheet.currency || 'TWD'),
            header_row_number: value(
              'header_row_number',
              sheet.header_row_number || 1
            ),
            holding_row_number: value(
              'holding_row_number',
              sheet.holding_row_number || 1
            ),
            price_row_number: value(
              'price_row_number',
              sheet.price_row_number ?? ''
            ),
            quantity_row_number: value(
              'quantity_row_number',
              sheet.quantity_row_number || 1
            ),
            first_asset_column_index: value(
              'first_asset_column_index',
              sheet.first_asset_column_index ?? 1
            ),
            group_width: value('group_width', sheet.group_width || 4),
            holding_count: sheet.holding_count || 0,
            sample_holdings: sheet.sample_holdings || [],
          }
        })
      )
    },
    [excelImportProfile]
  )

  const updateHorizontalSheet = useCallback(
    (sheetName: string, changes: Partial<ExcelHorizontalSheetDraft>) => {
      setExcelHorizontalSheets((current) =>
        current.map((sheet) =>
          sheet.sheet_name === sheetName ? { ...sheet, ...changes } : sheet
        )
      )
    },
    []
  )

  const mappingForExcelSheet = useCallback(
    (
      preview: ExcelMappingPreview,
      sheet: ExcelMappingSheetPreview,
      preferSaved: boolean
    ): ExcelColumnMapping => {
      const savedMatches =
        preferSaved &&
        excelImportProfile?.source_path === preview.source_path &&
        excelImportProfile?.sheet_name === sheet.sheet_name
      const sourceMapping = savedMatches
        ? excelImportProfile?.column_mapping
        : sheet.suggested_mapping
      return Object.fromEntries(
        Object.entries(sourceMapping || {}).map(([field, index]) => [
          field,
          String(index),
        ])
      )
    },
    [excelImportProfile]
  )

  const selectExcelSheet = useCallback(
    (
      preview: ExcelMappingPreview,
      sheet: ExcelMappingSheetPreview,
      preferSaved: boolean
    ) => {
      const savedMatches =
        preferSaved &&
        excelImportProfile?.source_path === preview.source_path &&
        excelImportProfile?.sheet_name === sheet.sheet_name
      setExcelSheetName(sheet.sheet_name)
      setExcelHeaderRow(
        savedMatches
          ? excelImportProfile?.header_row_number || 1
          : sheet.suggested_header_row_number || 1
      )
      setExcelDataStartRow(
        savedMatches
          ? excelImportProfile?.data_start_row_number || 2
          : sheet.suggested_data_start_row_number ||
              (sheet.suggested_header_row_number || 1) + 1
      )
      setExcelColumnMapping(mappingForExcelSheet(preview, sheet, preferSaved))
    },
    [excelImportProfile, mappingForExcelSheet]
  )

  const applySmartExcelRepair = useCallback(() => {
    if (!excelMappingPreview?.smart_repair?.recommended) return
    const repair = excelMappingPreview.smart_repair
    if (repair.layout === 'consolidated_report') {
      setExcelLayoutMode('consolidated_report')
      initializeConsolidatedReport(excelMappingPreview)
      return
    }
    if (repair.layout === 'horizontal_matrix') {
      setExcelLayoutMode('horizontal_matrix')
      initializeHorizontalSheets(excelMappingPreview)
      return
    }
    const sheet =
      excelMappingPreview.sheets.find(
        (item) => item.sheet_name === repair.sheet_name
      ) || excelMappingPreview.sheets[0]
    if (!sheet) return
    setExcelLayoutMode('row_mapping')
    setExcelSheetName(sheet.sheet_name)
    setExcelHeaderRow(repair.header_row_number || 1)
    setExcelDataStartRow(
      repair.data_start_row_number || (repair.header_row_number || 1) + 1
    )
    setExcelColumnMapping(
      Object.fromEntries(
        Object.entries(repair.column_mapping || {}).map(([field, index]) => [
          field,
          String(index),
        ])
      )
    )
  }, [excelMappingPreview, initializeConsolidatedReport, initializeHorizontalSheets])

  const openExcelMapper = useCallback(
    async (chooseFile: boolean) => {
      let sourcePath = chooseFile ? '' : portfolioSourcePath || ''
      if (!sourcePath.toLowerCase().endsWith('.xlsx')) {
        sourcePath = await openInvestmentFile()
      }
      if (!sourcePath) {
        setMessage('未選擇 Excel 檔案。')
        return
      }
      if (!sourcePath.toLowerCase().endsWith('.xlsx')) {
        setMessage('欄位設定目前支援 .xlsx，請先將舊版 Excel 另存為 .xlsx。')
        return
      }
      const result = await runCommand(
        'investment_watch_preview_excel_mapping',
        { path: sourcePath },
        'Excel 欄位預覽',
        120000
      )
      const preview = result?.excel_mapping_preview as
        | ExcelMappingPreview
        | undefined
      if (!preview?.sheets.length) return
      setExcelMappingPreview(preview)
      initializeConsolidatedReport(preview)
      initializeHorizontalSheets(preview)
      setExcelLayoutMode(
        excelImportProfile?.source_path === preview.source_path &&
          excelImportProfile?.layout === 'consolidated_report'
          ? 'consolidated_report'
          : excelImportProfile?.source_path === preview.source_path &&
              excelImportProfile?.layout === 'horizontal_matrix'
            ? 'horizontal_matrix'
            : preview.consolidated_layout?.recommended
              ? 'consolidated_report'
              : preview.horizontal_layout?.recommended
                ? 'horizontal_matrix'
                : 'row_mapping'
      )
      const savedSheet =
        excelImportProfile?.source_path === preview.source_path
          ? preview.sheets.find(
              (sheet) => sheet.sheet_name === excelImportProfile.sheet_name
            )
          : undefined
      const initialSheet =
        savedSheet ||
        preview.sheets.find(
          (sheet) => sheet.sheet_name === preview.selected_sheet_name
        ) ||
        preview.sheets[0]
      selectExcelSheet(preview, initialSheet, true)
      setExcelMapperOpen(true)
    },
    [
      excelImportProfile,
      initializeConsolidatedReport,
      initializeHorizontalSheets,
      portfolioSourcePath,
      runCommand,
      selectExcelSheet,
      setMessage,
    ]
  )

  const importExcelWithMapping = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault()
      if (!excelMappingPreview || !selectedExcelSheet) return
      if (excelLayoutMode === 'consolidated_report') {
        const config: Record<string, unknown> = { ...excelConsolidatedDraft }
        for (const field of CONSOLIDATED_NUMERIC_FIELDS) {
          config[field] = Number(excelConsolidatedDraft[field])
        }
        const result = await runCommand(
          'investment_watch_import_excel_mapping',
          {
            path: excelMappingPreview.source_path,
            layout: 'consolidated_report',
            config,
          },
          '報酬工作表匯入',
          240000
        )
        if (result) {
          setExcelMapperOpen(false)
          setExcelMappingPreview(null)
        }
        return
      }
      if (excelLayoutMode === 'horizontal_matrix') {
        if (selectedHorizontalSheets.length === 0) {
          setMessage('請至少選擇一張 ETF、台股、美股或共同基金工作表。')
          return
        }
        const invalidSheet = selectedHorizontalSheets.find(
          (sheet) =>
            !sheet.header_row_number ||
            !sheet.holding_row_number ||
            !sheet.quantity_row_number ||
            !sheet.group_width
        )
        if (invalidSheet) {
          setMessage(`請完成 ${invalidSheet.sheet_name} 的列與欄位設定。`)
          return
        }
        const result = await runCommand(
          'investment_watch_import_excel_mapping',
          {
            path: excelMappingPreview.source_path,
            layout: 'horizontal_matrix',
            sheets: selectedHorizontalSheets.map((sheet) => ({
              enabled: true,
              sheet_name: sheet.sheet_name,
              category_label: sheet.category_label,
              market: sheet.market,
              asset_type: sheet.asset_type,
              currency: sheet.currency,
              header_row_number: Number(sheet.header_row_number),
              holding_row_number: Number(sheet.holding_row_number),
              price_row_number:
                sheet.price_row_number === ''
                  ? null
                  : Number(sheet.price_row_number),
              quantity_row_number: Number(sheet.quantity_row_number),
              first_asset_column_index: Number(sheet.first_asset_column_index),
              group_width: Number(sheet.group_width),
            })),
          },
          'Excel 橫向持股匯入',
          240000
        )
        if (result) {
          setExcelMapperOpen(false)
          setExcelMappingPreview(null)
        }
        return
      }
      const missing = EXCEL_MAPPING_FIELDS.filter(
        (field) =>
          field.required &&
          (excelColumnMapping[field.key] === undefined ||
            excelColumnMapping[field.key] === '')
      )
      if (missing.length > 0) {
        setMessage(
          `請設定必要欄位：${missing.map((field) => field.label).join('、')}`
        )
        return
      }
      const columnMapping = Object.fromEntries(
        Object.entries(excelColumnMapping)
          .filter(([, index]) => index !== '')
          .map(([field, index]) => [field, Number(index)])
      )
      const result = await runCommand(
        'investment_watch_import_excel_mapping',
        {
          path: excelMappingPreview.source_path,
          sheet_name: selectedExcelSheet.sheet_name,
          header_row_number: excelHeaderRow,
          data_start_row_number: excelDataStartRow,
          column_mapping: columnMapping,
        },
        'Excel 欄位匯入',
        240000
      )
      if (result) {
        setExcelMapperOpen(false)
        setExcelMappingPreview(null)
      }
    },
    [
      excelColumnMapping,
      excelConsolidatedDraft,
      excelDataStartRow,
      excelHeaderRow,
      excelLayoutMode,
      excelMappingPreview,
      runCommand,
      selectedExcelSheet,
      selectedHorizontalSheets,
      setMessage,
    ]
  )

  return {
    excelMapperOpen,
    setExcelMapperOpen,
    excelMappingPreview,
    excelSheetName,
    excelHeaderRow,
    setExcelHeaderRow,
    excelDataStartRow,
    setExcelDataStartRow,
    excelColumnMapping,
    setExcelColumnMapping,
    excelLayoutMode,
    setExcelLayoutMode,
    excelHorizontalSheets,
    excelConsolidatedDraft,
    setExcelConsolidatedDraft,
    selectedExcelSheet,
    excelHeaderValues,
    mappedExcelPreviewRows,
    selectedHorizontalSheets,
    horizontalHoldingCount,
    horizontalSampleHoldings,
    updateHorizontalSheet,
    importExcelWithMapping,
    applySmartExcelRepair,
    selectExcelSheet,
    openExcelMapper,
  }
}
