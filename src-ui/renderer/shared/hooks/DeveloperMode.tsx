import { zhTW } from '@/i18n/zhTW'
import React, { useState } from 'react'
import { useBackendSocket } from './useBackendSocket'

type TabKey =
  | 'workspace'
  | 'designLab'
  | 'rescueCenter'
  | 'systemSettings'
  | 'deployment'

const DevButton: React.FC<{
  label: string
  onClick: () => void
  highlight?: boolean
}> = ({ label, onClick, highlight }) => (
  <button
    onClick={onClick}
    className={`p-3 rounded border text-left transition-colors ${
      highlight
        ? 'bg-blue-900/30 hover:bg-blue-900/50 text-blue-400 border-blue-900/50'
        : 'bg-gray-800 hover:bg-gray-700 text-gray-200 border-gray-700'
    }`}
  >
    {label}
  </button>
)

const TabPlaceholder: React.FC<{
  title: string
  desc: string
  placeholder: string
}> = ({ title, desc, placeholder }) => (
  <div className="space-y-6">
    <div>
      <h2 className="text-lg font-bold text-gray-200 mb-2">{title}</h2>
      <p className="text-sm text-gray-400 mb-4">{desc}</p>
      <div className="p-8 border border-dashed border-gray-700 rounded text-center text-gray-500">
        {placeholder}
      </div>
    </div>
  </div>
)

export const DeveloperMode: React.FC = () => {
  const [activeTab, setActiveTab] = useState<TabKey>('workspace')
  const { sendCommand, lastError } = useBackendSocket()

  const tabs: { key: TabKey; label: string }[] = [
    { key: 'workspace', label: zhTW.devTabs.workspace },
    { key: 'designLab', label: zhTW.devTabs.designLab },
    { key: 'rescueCenter', label: zhTW.devTabs.rescueCenter },
    { key: 'systemSettings', label: zhTW.devTabs.systemSettings },
    { key: 'deployment', label: zhTW.devTabs.deployment },
  ]

  const handleCommand = (cmd: string) => {
    sendCommand(cmd, {})
  }

  const handleDeploy = () => {
    // 高風險操作保留是/否彈窗
    if (window.confirm(zhTW.developer.deployConfirm)) {
      sendCommand('developer_apply_sandbox', { confirmed: true })
    }
  }

  return (
    <div className="flex flex-col h-full bg-gray-900 text-gray-100">
      {/* 標題區 */}
      <div className="px-6 py-4 border-b border-gray-800 flex flex-col gap-4">
        <button
          onClick={() =>
            window.dispatchEvent(
              new CustomEvent('navigate', { detail: 'dashboard' })
            )
          }
          className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded border border-gray-600 transition-colors flex items-center w-fit text-sm"
        >
          ← {zhTW.app.backHome}
        </button>
        <div>
          <h1 className="text-2xl font-bold">{zhTW.developer.title}</h1>
          <p className="text-sm text-gray-400 mt-1">
            {zhTW.developer.description}
          </p>
        </div>
      </div>

      {/* 分頁導覽列 */}
      <div className="flex border-b border-gray-800 px-4 bg-gray-900">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-3 text-sm font-medium transition-colors border-b-2 ${
              activeTab === tab.key
                ? 'border-blue-500 text-blue-400'
                : 'border-transparent text-gray-400 hover:text-gray-200'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* 內容區 */}
      <div className="flex-1 overflow-y-auto p-6">
        {/* 錯誤顯示區不外洩底層 Stack trace */}
        {lastError && (
          <div className="mb-6 p-4 bg-red-500/10 border border-red-500/50 rounded text-red-400 text-sm">
            {lastError}
          </div>
        )}

        {activeTab === 'workspace' && (
          <div className="space-y-6">
            <div>
              <h2 className="text-lg font-bold text-gray-200 mb-2">
                母工具開發與驗證
              </h2>
              <p className="text-sm text-gray-400 mb-4">
                {zhTW.developer.guardrail}
              </p>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                <DevButton
                  label={zhTW.developer.prepareSandbox}
                  onClick={() => handleCommand('developer_prepare_sandbox')}
                />
                <DevButton
                  label={zhTW.developer.autoOptimize}
                  onClick={() => handleCommand('developer_auto_optimize')}
                  highlight
                />
                <DevButton
                  label={zhTW.developer.phase1}
                  onClick={() => handleCommand('developer_phase1_integrity')}
                />
                <DevButton
                  label={zhTW.developer.phase2}
                  onClick={() => handleCommand('developer_phase2_static')}
                />
                <DevButton
                  label={zhTW.developer.phase3}
                  onClick={() => handleCommand('developer_phase3_startup')}
                />
                <DevButton
                  label={zhTW.developer.phase4}
                  onClick={() => handleCommand('developer_phase4_health')}
                />
                <DevButton
                  label={zhTW.developer.phase5}
                  onClick={() => handleCommand('developer_phase5_ai_review')}
                />
              </div>
            </div>
          </div>
        )}

        {activeTab === 'designLab' && (
          <TabPlaceholder
            title="子工具開發 (Design Lab)"
            desc="專案式子工具開發、測試與封裝，此區域禁止修改母工具。"
            placeholder="子工具管理介面載入區"
          />
        )}

        {activeTab === 'rescueCenter' && (
          <TabPlaceholder
            title="救援模式 (Rescue Center)"
            desc="針對母工具的錯誤診斷與日誌分析，不處理子工具業務。"
            placeholder="救援診斷窗口與 AI 協助區"
          />
        )}

        {activeTab === 'systemSettings' && (
          <TabPlaceholder
            title="系統設定 (System Settings)"
            desc="進階的 AI provider 設定、超時設定、與開發者標記管理。"
            placeholder="高級設定面板區"
          />
        )}

        {activeTab === 'deployment' && (
          <div className="space-y-6">
            <div>
              <h2 className="text-lg font-bold text-gray-200 mb-2">
                部署與發行 (Deployment)
              </h2>
              <p className="text-sm text-gray-400 mb-4">
                負責母工具建置檢查與沙盒覆蓋，操作前必須確認。
              </p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <DevButton
                  label={zhTW.developer.phase6}
                  onClick={() => handleCommand('developer_phase6_build')}
                />
                <DevButton
                  label={zhTW.developer.deploySummary}
                  onClick={() => handleCommand('developer_deploy_summary')}
                />
              </div>
              <div className="mt-6 p-4 border border-red-900/50 bg-red-900/10 rounded">
                <h3 className="text-red-400 font-bold mb-2">高風險操作</h3>
                <button
                  onClick={handleDeploy}
                  className="px-4 py-2 bg-red-600 hover:bg-red-500 text-white rounded font-medium transition-colors"
                >
                  {zhTW.developer.applySandbox}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
