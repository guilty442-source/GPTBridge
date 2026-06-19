import { useMemo } from 'react'
import type { BusyActions, UrlConfigKey } from '../types'

interface UseSystemSettingsCardStateInput {
  busyActions: BusyActions
  urlDraft: Record<UrlConfigKey, string>
  feedback: string
}

export interface SystemSettingsCardState {
  isConfigLoading: boolean
  isConfigSaving: boolean
  isChatgptOpening: boolean
  isGeminiOpening: boolean
  isClaudeOpening: boolean
  isPerplexityOpening: boolean
  isDeepseekOpening: boolean
  isIncreasingFont: boolean
  isDecreasingFont: boolean
  isBusy: boolean
  statusTone: 'ok' | 'warn'
  statusLabel: string
  urlDraft: Record<UrlConfigKey, string>
  feedback: string
}

export function useSystemSettingsCardState({
  busyActions,
  urlDraft,
  feedback,
}: UseSystemSettingsCardStateInput): SystemSettingsCardState {
  return useMemo(() => {
    const isConfigLoading = busyActions.includes('config-load')
    const isConfigSaving = busyActions.includes('config-save')
    const isChatgptOpening = busyActions.includes('browser-chatgpt')
    const isGeminiOpening = busyActions.includes('browser-gemini')
    const isClaudeOpening = busyActions.includes('browser-claude')
    const isPerplexityOpening = busyActions.includes('browser-perplexity')
    const isDeepseekOpening = busyActions.includes('browser-deepseek')
    const isIncreasingFont = busyActions.includes('font-increase')
    const isDecreasingFont = busyActions.includes('font-decrease')

    const isBusy =
      isConfigLoading ||
      isConfigSaving ||
      isChatgptOpening ||
      isGeminiOpening ||
      isClaudeOpening ||
      isPerplexityOpening ||
      isDeepseekOpening ||
      isIncreasingFont ||
      isDecreasingFont

    return {
      isConfigLoading,
      isConfigSaving,
      isChatgptOpening,
      isGeminiOpening,
      isClaudeOpening,
      isPerplexityOpening,
      isDeepseekOpening,
      isIncreasingFont,
      isDecreasingFont,
      isBusy,
      statusTone: isBusy ? 'warn' : 'ok',
      statusLabel: isBusy ? '執行中' : '待命',
      urlDraft,
      feedback,
    }
  }, [busyActions, urlDraft, feedback])
}

