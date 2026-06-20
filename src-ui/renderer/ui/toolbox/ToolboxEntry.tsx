import { useMemo, useState } from 'react'
import { zhTW } from '@/i18n/zhTW'
import { RuntimeToolCard } from '@/ui/developer-mode/tools/cards/RuntimeToolCard'
import type {
  ToolAction,
  ToolRuntimeState,
} from '@/ui/developer-mode/tools/types'
import { customToolComponents } from './customToolRegistry'
import '@/ui/developer-mode/developer-mode.css'

interface ToolboxEntryProps {
  tools: ToolRuntimeState[]
  syncing: boolean
  syncedAt: number | null
  onToolAction: (toolId: string, action: ToolAction) => void
}

function formatSyncTime(timestamp: number | null): string {
  if (!timestamp) return zhTW.toolbox.syncNotReady
  return new Date(timestamp).toLocaleTimeString('zh-TW', { hour12: false })
}

export function ToolboxEntry({
  tools,
  syncing,
  syncedAt,
  onToolAction,
}: ToolboxEntryProps) {
  const [selectedTool, setSelectedTool] = useState<ToolRuntimeState | null>(null)
  const SelectedCustomTool = useMemo(() => {
    if (!selectedTool) return null
    return customToolComponents[selectedTool.id] ?? null
  }, [selectedTool])

  if (selectedTool && SelectedCustomTool) {
    return (
      <section className="devm-tool-panel">
        <button
          type="button"
          className="devm-tool-settings-action"
          onClick={() => setSelectedTool(null)}
        >
          返回工具列表
        </button>
        <SelectedCustomTool />
      </section>
    )
  }

  return (
    <section className="devm-tool-panel">
      <header className="devm-tool-header">
        <div className="devm-tool-header-top">
          <h3>{zhTW.toolbox.title}</h3>
          <span className="devm-tool-settings-feedback">
            {syncing ? zhTW.toolbox.syncing : zhTW.toolbox.synced}
          </span>
        </div>
        <p>
          {`${zhTW.toolbox.description} ${zhTW.toolbox.lastSync}: ${formatSyncTime(
            syncedAt
          )}`}
        </p>
      </header>

      {tools.length === 0 ? (
        <div className="devm-tool-card">{zhTW.toolbox.empty}</div>
      ) : (
        <div className="devm-tool-grid">
          {tools.map((tool) => {
            const CustomTool = customToolComponents[tool.id]
            if (tool.hasCustomUi && CustomTool) {
              return (
                <article key={tool.id} className="devm-tool-card">
                  <button
                    type="button"
                    className="devm-tool-custom-launch"
                    onClick={() => setSelectedTool(tool)}
                  >
                    <span className="devm-tool-name">{tool.name}</span>
                    <span className="devm-tool-desc">
                      {tool.description || tool.summary}
                    </span>
                  </button>
                </article>
              )
            }

            return (
              <RuntimeToolCard
                key={tool.id}
                tool={tool}
                onToolAction={onToolAction}
                startLabel={
                  tool.status === 'running'
                    ? '已啟動'
                    : tool.status === 'starting'
                      ? '啟動中'
                      : '啟動應用程式'
                }
                stopLabel="停止應用程式"
              />
            )
          })}
        </div>
      )}
    </section>
  )
}
