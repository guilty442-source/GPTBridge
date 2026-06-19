import type { ToolDefinition } from '@/ui/developer-mode/tools/types'

/**
 * Application registry keeps standalone window entries and child applications.
 * Child runtime implementations live under platform_tools/<tool-id>/.
 */
export const toolboxToolRegistry: ToolDefinition[] = [
  {
    id: 'ai-assistant',
    name: 'AI 協作工具',
    summary: '集中管理 AI 登入、提示內容與多來源回覆。',
    folderPath: 'platform_tools/ai-assistant',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'project-cleaner',
    name: '清理工具',
    summary: '清理專案垃圾、快取與臨時檔。',
    folderPath: 'platform_tools/project-cleaner',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'agent-coder',
    name: '系統救援工具',
    summary: '管理應用程式程式碼、修補指令與單元測試。',
    folderPath: 'platform_tools/agent-coder',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'vaultly',
    name: '影音下載自動化',
    summary: '集中模組：platform_tools/vaultly。',
    folderPath: 'platform_tools/vaultly',
    launchable: true,
  },
  {
    id: 'file-sorter',
    name: '自動化檔案管理',
    summary: '開啟工具或偵測新檔案後可自動分類，並支援跨硬碟目的地。',
    folderPath: 'platform_tools/file-sorter',
    launchable: true,
  },
  {
    id: 'tool-mpz30cfk-hfnf',
    name: '自動檔案清理',
    summary: '逐資料夾顯示進度，可停止掃描並直接開啟結果檔案檢查。',
    folderPath: 'platform_tools/tool-mpz30cfk-hfnf',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'tool-mqi8uv5x-fo9f',
    name: '投資管家',
    summary: '匯入庫存檔並以多來源連網監控報價，收盤後停止更新。',
    folderPath: 'platform_tools/tool-mqi8uv5x-fo9f',
    launchable: true,
    windowOnly: true,
  },
]
