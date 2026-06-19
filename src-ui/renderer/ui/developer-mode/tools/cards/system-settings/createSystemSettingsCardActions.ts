import type { UrlConfigKey } from '../types'

interface CreateSystemSettingsCardActionsInput {
  onUrlChange: (key: UrlConfigKey, value: string) => void
  onSaveUrl: () => void
  onIncreaseFont: () => void
  onDecreaseFont: () => void
  onOpenBrowser: (
    provider: 'chatgpt' | 'gemini' | 'claude' | 'perplexity' | 'deepseek'
  ) => void
  onStop: () => void
}

export interface SystemSettingsCardActions {
  handleUrlChange: (key: UrlConfigKey, value: string) => void
  handleSaveUrl: () => void
  handleIncreaseFont: () => void
  handleDecreaseFont: () => void
  handleOpenBrowser: (
    provider: 'chatgpt' | 'gemini' | 'claude' | 'perplexity' | 'deepseek'
  ) => void
  handleStop: () => void
}

export function createSystemSettingsCardActions({
  onUrlChange,
  onSaveUrl,
  onIncreaseFont,
  onDecreaseFont,
  onOpenBrowser,
  onStop,
}: CreateSystemSettingsCardActionsInput): SystemSettingsCardActions {
  return {
    handleUrlChange: (key: UrlConfigKey, value: string) => {
      onUrlChange(key, value)
    },
    handleSaveUrl: () => {
      onSaveUrl()
    },
    handleIncreaseFont: () => {
      onIncreaseFont()
    },
    handleDecreaseFont: () => {
      onDecreaseFont()
    },
    handleOpenBrowser: (provider) => {
      onOpenBrowser(provider)
    },
    handleStop: () => {
      onStop()
    },
  }
}

