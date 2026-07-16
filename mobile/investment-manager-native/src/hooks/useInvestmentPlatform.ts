import { useCallback, useEffect, useState } from 'react'

import type {
  InvestmentMobileSnapshot,
  SavedConnection,
} from '../api/contracts'
import {
  getInvestmentSnapshot,
  getPlatformContract,
  pairNativeDevice,
  queueLocalAiCommand,
} from '../api/investmentClient'
import {
  clearConnection,
  loadConnection,
  saveConnection,
} from '../security/sessionStore'

export function useInvestmentPlatform() {
  const [connection, setConnection] = useState<SavedConnection | null>(null)
  const [snapshot, setSnapshot] = useState<InvestmentMobileSnapshot | null>(null)
  const [busy, setBusy] = useState(true)
  const [message, setMessage] = useState('正在讀取安全配對狀態…')
  const [lastSyncedAt, setLastSyncedAt] = useState('')

  const refresh = useCallback(
    async (selected = connection, silent = false) => {
      if (!selected) return
      if (!silent) setBusy(true)
      try {
        const state = await getInvestmentSnapshot(
          selected.baseUrl,
          selected.sessionToken
        )
        setSnapshot(state)
        setLastSyncedAt(new Date().toISOString())
        setMessage('已與桌面共用資料核心即時同步')
      } catch (error) {
        setMessage(error instanceof Error ? error.message : '同步失敗')
        if (!silent) throw error
      } finally {
        if (!silent) setBusy(false)
      }
    },
    [connection]
  )

  useEffect(() => {
    let active = true
    void loadConnection()
      .then(async (saved) => {
        if (!active || !saved) return
        setConnection(saved)
        try {
          const state = await getInvestmentSnapshot(
            saved.baseUrl,
            saved.sessionToken
          )
          if (!active) return
          setSnapshot(state)
          setLastSyncedAt(new Date().toISOString())
          setMessage('已恢復安全工作階段')
        } catch {
          if (!active) return
          await clearConnection()
          setConnection(null)
          setMessage('工作階段已失效，請重新配對')
        }
      })
      .finally(() => {
        if (active) setBusy(false)
      })
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    if (!connection) return
    let active = true
    let inFlight = false
    const synchronize = async () => {
      if (!active || inFlight) return
      inFlight = true
      try {
        await refresh(connection, true)
      } finally {
        inFlight = false
      }
    }
    const timer = setInterval(() => void synchronize(), 2_000)
    return () => {
      active = false
      clearInterval(timer)
    }
  }, [connection, refresh])

  const pair = useCallback(async (baseUrl: string, code: string) => {
    setBusy(true)
    try {
      await getPlatformContract(baseUrl)
      const sessionToken = await pairNativeDevice(baseUrl, code)
      const saved = { baseUrl: baseUrl.trim().replace(/\/+$/, ''), sessionToken }
      await saveConnection(saved)
      setConnection(saved)
      const state = await getInvestmentSnapshot(saved.baseUrl, sessionToken)
      setSnapshot(state)
      setLastSyncedAt(new Date().toISOString())
      setMessage('原生手機已與桌面投資資料核心配對')
    } finally {
      setBusy(false)
    }
  }, [])

  const disconnect = useCallback(async () => {
    await clearConnection()
    setConnection(null)
    setSnapshot(null)
    setMessage('此手機的本機配對憑證已清除')
  }, [])

  const queueCommand = useCallback(
    async (instruction: string) => {
      if (!connection) throw new Error('請先完成配對。')
      setBusy(true)
      try {
        const result = await queueLocalAiCommand(
          connection.baseUrl,
          connection.sessionToken,
          instruction
        )
        setMessage(result)
        return result
      } finally {
        setBusy(false)
      }
    },
    [connection]
  )

  return {
    connection,
    snapshot,
    busy,
    message,
    lastSyncedAt,
    pair,
    refresh,
    disconnect,
    queueCommand,
  }
}
