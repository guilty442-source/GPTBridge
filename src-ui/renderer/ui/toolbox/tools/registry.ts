import type { ToolDefinition } from './types'

/** Launcher metadata only. Business logic and writable data stay tool-owned. */
export const toolboxToolRegistry: ToolDefinition[] = [
  {
    id: 'ai-assistant',
    name: 'AI 投資管家',
    summary: '整合星澄與外部 AI，提供跨電腦與 Android 的即時投資協作。',
    folderPath: 'platform_tools/ai-assistant',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'local-ai',
    name: '星澄',
    summary: '在本機獨立執行的 AI 服務，模型、資料庫與權限皆不與外部 AI 共用。',
    folderPath: 'platform_tools/local-ai',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'ai-collaboration',
    name: '外部 AI 協作',
    summary: '以驗證連線存取外部 AI；離線指令立即拒絕，不保存待送佇列。',
    folderPath: 'platform_tools/ai-collaboration',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'project-cleaner',
    name: '專案清理與系統救援',
    summary: '合併清理、異常修正與救援能力，權限嚴格限制在本專案內。',
    folderPath: 'platform_tools/project-cleaner',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'vaultly',
    name: '影音下載自動化',
    summary: '使用獨立登入狀態與資料庫管理媒體下載、重試與背景監控。',
    folderPath: 'platform_tools/vaultly',
    launchable: true,
  },
  {
    id: 'file-sorter',
    name: '自動化檔案管理',
    summary: '監看新檔並即時分類，關鍵字規則與索引由工具自己的資料庫管理。',
    folderPath: 'platform_tools/file-sorter',
    launchable: true,
  },
]
