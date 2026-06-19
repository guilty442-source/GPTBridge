import ReactDOM from 'react-dom/client'
import App from './ui/App'
import { AiAssistantWindowApp } from '../../platform_tools/ai-assistant/src/ui/AiAssistantWindowApp'
import { AgentCoderWindowApp } from '../../platform_tools/agent-coder/src/ui/AgentCoderWindowApp'
import { FileSorterWindowApp } from '../../platform_tools/file-sorter/src/ui/FileSorterWindowApp'
import { ProjectCleanerWindowApp } from '../../platform_tools/project-cleaner/src/ui/ProjectCleanerWindowApp'
import { DuplicateCleanerWindowApp } from '../../platform_tools/tool-mpz30cfk-hfnf/src/ui/DuplicateCleanerWindowApp'
import { InvestmentManagerWindowApp } from '../../platform_tools/tool-mqi8uv5x-fo9f/src/ui/InvestmentManagerWindowApp'
import { VaultlyWindowApp } from '../../platform_tools/vaultly/src/ui/VaultlyWindowApp'
import { HMRGuard } from './shared/components/HMRGuard'

const searchParams = new URLSearchParams(window.location.search)
const toolWindowId =
  searchParams.get('toolWindow') === '1' ? searchParams.get('tool') || '' : ''

ReactDOM.createRoot(document.getElementById('root')!).render(
  <HMRGuard>
    {toolWindowId === 'ai-assistant' ? (
      <AiAssistantWindowApp />
    ) : toolWindowId === 'agent-coder' ? (
      <AgentCoderWindowApp />
    ) : toolWindowId === 'project-cleaner' ? (
      <ProjectCleanerWindowApp />
    ) : toolWindowId === 'file-sorter' ? (
      <FileSorterWindowApp />
    ) : toolWindowId === 'tool-mpz30cfk-hfnf' ? (
      <DuplicateCleanerWindowApp />
    ) : toolWindowId === 'tool-mqi8uv5x-fo9f' ? (
      <InvestmentManagerWindowApp />
    ) : toolWindowId === 'vaultly' ? (
      <VaultlyWindowApp />
    ) : toolWindowId ? (
      <App />
    ) : (
      <App />
    )}
  </HMRGuard>
)
