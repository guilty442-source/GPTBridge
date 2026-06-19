import ReactDOM from 'react-dom/client'
import { AgentCoderWindowApp } from '../../platform_tools/agent-coder/src/ui/AgentCoderWindowApp'
import { AiAssistantWindowApp } from '../../platform_tools/ai-assistant/src/ui/AiAssistantWindowApp'
import { FileSorterWindowApp } from '../../platform_tools/file-sorter/src/ui/FileSorterWindowApp'
import { ProjectCleanerWindowApp } from '../../platform_tools/project-cleaner/src/ui/ProjectCleanerWindowApp'
import { DuplicateCleanerWindowApp } from '../../platform_tools/tool-mpz30cfk-hfnf/src/ui/DuplicateCleanerWindowApp'
import { InvestmentManagerWindowApp } from '../../platform_tools/tool-mqi8uv5x-fo9f/src/ui/InvestmentManagerWindowApp'
import { VaultlyWindowApp } from '../../platform_tools/vaultly/src/ui/VaultlyWindowApp'
import './platform-tool.css'

const TOOL_COMPONENTS = {
  'agent-coder': AgentCoderWindowApp,
  'ai-assistant': AiAssistantWindowApp,
  'file-sorter': FileSorterWindowApp,
  'project-cleaner': ProjectCleanerWindowApp,
  'tool-mpz30cfk-hfnf': DuplicateCleanerWindowApp,
  'tool-mqi8uv5x-fo9f': InvestmentManagerWindowApp,
  vaultly: VaultlyWindowApp,
} as const

function resolveToolId(): string {
  const params = new URLSearchParams(window.location.search)
  return String(params.get('tool') || '').trim()
}

function PlatformToolHost() {
  const toolId = resolveToolId()
  document.title = toolId ? `GPTBridge - ${toolId}` : 'GPTBridge Application'
  const ToolComponent =
    TOOL_COMPONENTS[toolId as keyof typeof TOOL_COMPONENTS] ?? null

  if (!ToolComponent) {
    return (
      <main className="platform-tool-fallback">
        <section className="platform-tool-fallback__panel">
          <h1>Application unavailable</h1>
          <p>
            This standalone window did not receive a known platform application
            id. Close it and start the application again from GPTBridge.
          </p>
        </section>
      </main>
    )
  }

  return <ToolComponent />
}

ReactDOM.createRoot(document.getElementById('root')!).render(<PlatformToolHost />)
