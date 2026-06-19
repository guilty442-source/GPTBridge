import { useCallback, useEffect, useMemo, useState } from 'react'
import { hasChineseInput, toGovernanceRuleCode } from './ruleCode'
import type { GovernanceCommandSender, GovernanceRulesSnapshot } from './types'

const IPC_TIMEOUT_MS = 8000

function normalizeRules(payload: unknown): string[] {
  if (!Array.isArray(payload)) return []
  return payload.map((item) => String(item).trim()).filter(Boolean)
}

function dedupeRules(rules: string[]): string[] {
  return Array.from(new Set(rules))
}

function parseRulesPayload(payload: Record<string, unknown>): GovernanceRulesSnapshot {
  const activeRules = normalizeRules(payload.active_rules ?? payload.rules)
  const availableRules = normalizeRules(payload.available_rules ?? payload.rules)
  const rules = dedupeRules([...availableRules, ...activeRules])
  return {
    rules,
    activeRules: activeRules.length > 0 ? dedupeRules(activeRules) : rules,
  }
}

function waitForIpcEvent(
  eventName: string,
  timeoutMs: number
): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      window.removeEventListener('ipc_event', handler)
      reject(new Error(`等待 IPC 事件逾時：${eventName}`))
    }, timeoutMs)

    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      if (detail.event !== eventName) return
      window.clearTimeout(timer)
      window.removeEventListener('ipc_event', handler)
      resolve((detail.payload || {}) as Record<string, unknown>)
    }

    window.addEventListener('ipc_event', handler)
  })
}

interface UseGovernanceRulesOptions {
  sendCommand: GovernanceCommandSender
  socketStatus: string
}

export function useGovernanceRules({
  sendCommand,
  socketStatus,
}: UseGovernanceRulesOptions) {
  const [rules, setRules] = useState<string[]>([])
  const [activeRules, setActiveRules] = useState<string[]>([])
  const [selectedRule, setSelectedRule] = useState('')
  const [ruleDraft, setRuleDraft] = useState('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState('')

  const activeRuleSet = useMemo(() => new Set(activeRules), [activeRules])
  const convertedRuleCode = useMemo(
    () => toGovernanceRuleCode(ruleDraft),
    [ruleDraft]
  )
  const isConvertedFromChinese = useMemo(
    () => Boolean(ruleDraft.trim()) && hasChineseInput(ruleDraft),
    [ruleDraft]
  )

  const applySnapshot = useCallback((snapshot: GovernanceRulesSnapshot) => {
    const nextRules = dedupeRules([...snapshot.rules, ...snapshot.activeRules])
    setRules(nextRules)
    setActiveRules(nextRules)
  }, [])

  const sendCommandAndWait = useCallback(
    async (
      command: string,
      eventName: string,
      payload: Record<string, unknown>
    ) => {
      const waitPromise = waitForIpcEvent(eventName, IPC_TIMEOUT_MS)
      sendCommand(command, payload)
      return waitPromise
    },
    [sendCommand]
  )

  const refreshRules = useCallback(async () => {
    if (socketStatus !== 'Connected') {
      setFeedback('後端尚未連線，請稍後再同步治理規則。')
      return
    }

    setLoading(true)
    setFeedback('')
    try {
      const result = await sendCommandAndWait(
        'app:get-governance-rules',
        'app:get-governance-rules_result',
        {}
      )
      if (result.ok === false) {
        setFeedback(String(result.message || '治理規則載入失敗'))
        return
      }
      applySnapshot(parseRulesPayload(result))
      setFeedback('治理規則已同步；全部規則已全域啟動。')
    } catch {
      setFeedback('治理規則載入逾時，請稍後再試。')
    } finally {
      setLoading(false)
    }
  }, [applySnapshot, sendCommandAndWait, socketStatus])

  useEffect(() => {
    if (rules.length === 0) {
      setSelectedRule('')
      return
    }
    if (!selectedRule || !rules.includes(selectedRule)) {
      setSelectedRule(rules[0])
    }
  }, [rules, selectedRule])

  useEffect(() => {
    if (socketStatus === 'Connected') {
      void refreshRules()
    }
  }, [refreshRules, socketStatus])

  useEffect(() => {
    const handler = () => {
      void refreshRules()
    }

    window.addEventListener('gptbridge:global-data-reload', handler)
    return () => window.removeEventListener('gptbridge:global-data-reload', handler)
  }, [refreshRules])

  const addRule = useCallback(async () => {
    const rule = convertedRuleCode
    if (!rule) {
      setFeedback('請先輸入中文描述或規則代碼。')
      return
    }
    if (activeRuleSet.has(rule)) {
      setFeedback('此規則已全域啟動。')
      return
    }

    setBusy(true)
    setFeedback('')
    try {
      const result = await sendCommandAndWait(
        'app:add-governance-rule',
        'app:add-governance-rule_result',
        { rule, source_text: ruleDraft }
      )
      if (result.ok === false) {
        setFeedback(String(result.message || '新增規則失敗'))
        return
      }
      applySnapshot(parseRulesPayload(result))
      setRuleDraft('')
      setSelectedRule(rule)
      setFeedback(`治理規則已新增並全域啟動：${rule}`)
    } catch {
      setFeedback('新增規則逾時，請稍後再試。')
    } finally {
      setBusy(false)
    }
  }, [
    activeRuleSet,
    applySnapshot,
    convertedRuleCode,
    ruleDraft,
    sendCommandAndWait,
  ])

  const updateRule = useCallback(async () => {
    const nextRule = convertedRuleCode
    if (!selectedRule) {
      setFeedback('請先選取要修改的治理規則。')
      return
    }
    if (!nextRule) {
      setFeedback('請先輸入中文描述或新規則代碼。')
      return
    }
    if (selectedRule === nextRule) {
      setFeedback('規則代碼沒有變更。')
      return
    }

    setBusy(true)
    setFeedback('')
    try {
      const result = await sendCommandAndWait(
        'app:update-governance-rule',
        'app:update-governance-rule_result',
        {
          old_rule: selectedRule,
          rule: nextRule,
          source_text: ruleDraft,
        }
      )
      if (result.ok === false) {
        setFeedback(String(result.message || '修改規則失敗'))
        return
      }
      applySnapshot(parseRulesPayload(result))
      setRuleDraft('')
      setSelectedRule(nextRule)
      setFeedback(`治理規則已修改並全域啟動：${nextRule}`)
    } catch {
      setFeedback('修改規則逾時，請稍後再試。')
    } finally {
      setBusy(false)
    }
  }, [
    applySnapshot,
    convertedRuleCode,
    ruleDraft,
    selectedRule,
    sendCommandAndWait,
  ])

  const removeCustomRule = useCallback(
    async (rule: string) => {
      if (!rule) {
        setFeedback('請先選取治理規則。')
        return
      }

      setBusy(true)
      setFeedback('')
      try {
        const result = await sendCommandAndWait(
          'app:delete-governance-rule',
          'app:delete-governance-rule_result',
          { rule }
        )
        if (result.ok === false) {
          setFeedback(String(result.message || '移除規則失敗'))
          return
        }
        applySnapshot(parseRulesPayload(result))
        setFeedback(String(result.message || '自訂治理規則已移除；內建規則仍全域啟動。'))
      } catch {
        setFeedback('移除規則逾時，請稍後再試。')
      } finally {
        setBusy(false)
      }
    },
    [applySnapshot, sendCommandAndWait]
  )

  return {
    rules,
    activeRules,
    activeRuleSet,
    selectedRule,
    setSelectedRule,
    ruleDraft,
    setRuleDraft,
    convertedRuleCode,
    isConvertedFromChinese,
    loading,
    busy,
    feedback,
    refreshRules,
    addRule,
    updateRule,
    removeCustomRule,
  }
}
