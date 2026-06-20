import { CSSProperties, useCallback, useEffect } from 'react'
import {
  serviceManager,
  startStartupPipeline,
} from '@/services/RuntimeServiceManager'
import { useBackendSocket } from '@/hooks/useBackendSocket'
import { zhTW } from '@/i18n/zhTW'
import { ToolboxEntry } from '@/ui/toolbox/ToolboxEntry'
import { useToolboxApplications } from '@/ui/toolbox/useToolboxApplications'

const UI_ZOOM_STORAGE_KEY = 'gptbridge_ui_zoom_factor'
const MIN_UI_ZOOM = 0.85
const MAX_UI_ZOOM = 1.3

function clampUiZoom(value: number): number {
  return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value))
}

export default function App() {
  const backendSocket = useBackendSocket()
  const sendCommand = backendSocket.sendCommand

  const waitForIpcEvent = useCallback(
    (
      eventName: string,
      timeoutMs: number,
      predicate?: (payload: Record<string, unknown>) => boolean
    ): Promise<Record<string, unknown>> => {
      return new Promise((resolve, reject) => {
        const timer = window.setTimeout(() => {
          window.removeEventListener('ipc_event', handler)
          reject(new Error(`等待事件逾時：${eventName}`))
        }, timeoutMs)

        const handler = (event: Event) => {
          const customEvent = event as CustomEvent
          const detail = customEvent.detail || {}
          if (detail.event !== eventName) return
          const payload = (detail.payload || {}) as Record<string, unknown>
          if (predicate && !predicate(payload)) return
          window.clearTimeout(timer)
          window.removeEventListener('ipc_event', handler)
          resolve(payload)
        }

        window.addEventListener('ipc_event', handler)
      })
    },
    []
  )

  const {
    toolboxTools,
    toolboxSyncing,
    toolboxSyncedAt,
    handleToolboxAction,
  } = useToolboxApplications({
    backendStatus: backendSocket.status,
    sendCommand,
    waitForIpcEvent,
  })

  useEffect(() => {
    const api = (window as any).electron
    if (!api?.invoke) return

    const saved = (() => {
      try {
        const raw = window.localStorage.getItem(UI_ZOOM_STORAGE_KEY)
        const parsed = raw ? Number(raw) : 1
        if (Number.isNaN(parsed) || parsed <= 0) return 1
        return clampUiZoom(parsed)
      } catch {
        return 1
      }
    })()

    void api.invoke('app:set-ui-zoom', { factor: saved })
  }, [])

  useEffect(() => {
    if (serviceManager.getAllStates().length === 0) {
      void startStartupPipeline()
    }
  }, [])

  return (
    <div style={styles.app}>
      <header style={styles.header}>
        <div>
          <div style={styles.brandName}>{zhTW.app.platformName}</div>
          <div style={styles.headerTitle}>應用程式啟動器</div>
        </div>
        <div style={styles.launcherBadge}>獨立應用程式</div>
      </header>

      <main style={styles.contentArea}>
        <ToolboxEntry
          tools={toolboxTools}
          syncing={toolboxSyncing}
          syncedAt={toolboxSyncedAt}
          onToolAction={handleToolboxAction}
        />
      </main>
    </div>
  )
}

const styles: Record<string, CSSProperties> = {
  app: {
    width: '100vw',
    height: '100vh',
    background: '#0b0f17',
    color: '#f5f7fb',
    fontFamily: '"Noto Sans TC", "Segoe UI", sans-serif',
    overflow: 'hidden',
    display: 'flex',
    flexDirection: 'column',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '16px',
    padding: '14px 18px 10px',
    borderBottom: '1px solid #1e293b',
    background: '#0f172a',
  },
  brandName: {
    fontSize: '12px',
    fontWeight: 900,
    color: '#94a3b8',
    lineHeight: 1.1,
  },
  headerTitle: {
    marginTop: '4px',
    color: '#f8fafc',
    fontSize: '18px',
    fontWeight: 900,
  },
  launcherBadge: {
    flexShrink: 0,
    border: '1px solid #334155',
    borderRadius: '999px',
    color: '#cbd5e1',
    background: '#111827',
    fontSize: '12px',
    fontWeight: 800,
    padding: '7px 10px',
  },
  contentArea: {
    flex: 1,
    width: '100%',
    overflowY: 'auto',
    padding: '12px',
  },
}
