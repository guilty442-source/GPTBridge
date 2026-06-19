export type UrlConfigKey =
  | 'chatgpt_main_url'
  | 'gemini_main_url'
  | 'claude_main_url'
  | 'perplexity_main_url'
  | 'deepseek_main_url'

export type BusyAction =
  | 'tools-auto-start'
  | 'config-load'
  | 'config-save'
  | 'browser-chatgpt'
  | 'browser-gemini'
  | 'browser-claude'
  | 'browser-perplexity'
  | 'browser-deepseek'
  | 'font-increase'
  | 'font-decrease'
  | 'profile-reset-chatgpt'
  | 'profile-reset-gemini'
  | 'sandbox-auto'
  | 'sandbox-maintain'
  | 'sandbox-health'
  | 'update-auto'
  | 'update-refresh'
  | 'update-restart'
  | 'update-apply'
  | 'backup-auto'
  | 'backup-record'
  | 'backup-delete'
  | 'logs-auto'
  | 'logs-export'
  | 'logs-export-errors'

export type BusyActions = BusyAction[]

export const initialUrlDraft: Record<UrlConfigKey, string> = {
  chatgpt_main_url: '',
  gemini_main_url: '',
  claude_main_url: '',
  perplexity_main_url: '',
  deepseek_main_url: '',
}
