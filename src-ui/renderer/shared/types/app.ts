/**
 * GPTBridge 全局應用狀態類型定義
 * 與後端 GPTBridgeApp 及 SessionStatusEnum 對齊
 */

export type AppStatus = 'ready' | 'safe_mode' | 'error' | 'loading'

export type AIProviderStatus =
  | 'AUTHENTICATED'
  | 'UNAUTHENTICATED'
  | 'ERROR'
  | 'UNOPENED'
  | 'READY'
  | 'RESTARTING'
  | 'CLOSED'

export interface AppType {
  status: AppStatus
  message?: string
  providers: {
    chatgpt: AIProviderStatus
    gemini: AIProviderStatus
  }
  currentAccounts: {
    chatgpt: string
    gemini: string
  }
}
