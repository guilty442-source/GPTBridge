import type { ComponentType } from 'react'
import ReactDOM from 'react-dom/client'
import './platform-tool.css'

export function renderPlatformTool(
  ToolComponent: ComponentType,
  toolId: string
): void {
  document.title = toolId ? `GPTBridge - ${toolId}` : 'GPTBridge Application'
  ReactDOM.createRoot(document.getElementById('root')!).render(<ToolComponent />)
}
