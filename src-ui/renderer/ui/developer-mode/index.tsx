import type { RuntimeService } from '@/shared/types/runtime'
import { zhTW } from '@/i18n/zhTW'
import { ToolRuntimePanel } from './tools/ToolRuntimePanel'
import type { ToolControlCenterState } from './tools/controllers/useToolControlCenter'
import type { ToolAction, ToolRuntimeState } from './tools/types'
import './developer-mode.css'

interface DeveloperModeProps {
  services: RuntimeService[]
  systemStatus: string
  tools: ToolRuntimeState[]
  toolControl: ToolControlCenterState
  onToolAction: (toolId: string, action: ToolAction) => void
}

export function DeveloperMode(props: DeveloperModeProps) {
  const { tools, toolControl, onToolAction } = props

  const handleFullRestart = () => {
    const api = (window as any).electron
    if (!api?.invoke) return

    const confirmed = window.confirm(
      '這會強制重啟整個應用程式並中止目前工具流程，是否繼續？'
    )
    if (confirmed) {
      void api.invoke('app:restart')
    }
  }

  return (
    <section className="devm-shell">
      <div className="devm-noise" />

      <header className="devm-header">
        <div className="devm-title-wrap">
          <p className="devm-eyebrow">Developer Workspace</p>
          <h1 className="devm-title">{zhTW.developer.title}</h1>
        </div>
      </header>

      <div className="devm-workspace devm-workspace--tools-only">
        <div className="devm-main-column">
          <div className="devm-section">
            <ToolRuntimePanel
              tools={tools}
              control={toolControl}
              onToolAction={onToolAction}
            />
          </div>
        </div>
      </div>

      <button type="button" className="devm-restart-fab" onClick={handleFullRestart}>
        立即重啟系統
      </button>
    </section>
  )
}
