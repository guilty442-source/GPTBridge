import { BackupToolCard } from './cards/BackupToolCard'
import { LogToolCard } from './cards/LogToolCard'
import { RuntimeToolCard } from './cards/RuntimeToolCard'
import { SandboxToolCard } from './cards/SandboxToolCard'
import { SystemSettingsCard } from './cards/SystemSettingsCard'
import { UpdateToolCard } from './cards/UpdateToolCard'
import type { ToolControlCenterState } from './controllers/useToolControlCenter'
import { zhTW } from '@/i18n/zhTW'
import type { ToolAction, ToolRuntimeState } from './types'

interface ToolRuntimePanelProps {
  tools: ToolRuntimeState[]
  control: ToolControlCenterState
  onToolAction: (toolId: string, action: ToolAction) => void
}

export function ToolRuntimePanel({
  tools,
  control,
  onToolAction,
}: ToolRuntimePanelProps) {
  return (
    <section className="devm-tool-panel">
      <header className="devm-tool-header">
        <div className="devm-tool-header-top">
          <h3>{`${zhTW.developer.title}工具執行層`}</h3>
        </div>
        <p>{`進階工具僅在${zhTW.developer.title}管理與啟停，${zhTW.toolbox.title}僅做入口展示。`}</p>
      </header>

      <div className="devm-tool-grid">
        <SystemSettingsCard
          busyActions={control.busyActions}
          urlDraft={control.urlDraft}
          onIncreaseFont={control.increaseTextSize}
          onDecreaseFont={control.decreaseTextSize}
          onStop={control.stopCurrentAction}
          feedback={control.settingsFeedback}
        />

        <SandboxToolCard
          busyActions={control.busyActions}
          intervalMinutes={control.sandboxIntervalMinutes}
          onIntervalChange={control.setSandboxIntervalMinutes}
          onStart={control.startSandboxTool}
          onStop={control.stopSandboxTool}
          feedback={control.sandboxFeedback}
        />

        <UpdateToolCard
          busyActions={control.busyActions}
          intervalMinutes={control.updateIntervalMinutes}
          onIntervalChange={control.setUpdateIntervalMinutes}
          onStart={control.startUpdateTool}
          onStop={control.stopUpdateTool}
          feedback={control.updateFeedback}
          nonHotChangeCount={control.updateNonHotChangeCount}
          nonHotChanges={control.updateNonHotChanges}
          globalUpdatePlan={control.globalUpdatePlan}
          onApplyDetectedUpdates={control.applyDetectedUpdates}
        />

        <BackupToolCard
          busyActions={control.busyActions}
          intervalMinutes={control.backupIntervalMinutes}
          onIntervalChange={control.setBackupIntervalMinutes}
          onStart={control.startBackupTool}
          onDeleteBackupRecord={control.deleteBackupRecord}
          onStop={control.stopBackupTool}
          feedback={control.backupFeedback}
        />

        <LogToolCard
          busyActions={control.busyActions}
          intervalMinutes={control.logIntervalMinutes}
          onIntervalChange={control.setLogIntervalMinutes}
          onStart={control.startLogTool}
          onStop={control.stopLogTool}
          feedback={control.logFeedback}
          records={control.operationRecords}
        />

        {tools.map((tool) => (
          <RuntimeToolCard key={tool.id} tool={tool} onToolAction={onToolAction} />
        ))}
      </div>
    </section>
  )
}
