import { useCallback, useEffect, useState } from 'react'
import { BasePanel } from './BasePanel'
import { mainSystemLocale } from '@/locales/main-system'
import './SagaVisualizerPanel.css'

const s = mainSystemLocale.sagaVisualizer
const tb = mainSystemLocale.toolbox

interface SagaOperation {
  operation_id: string
  operation_type: string
  module_id: string
  status: string
  nodes: Array<{
    id: string
    label: string
    type: string
    status: string
    engine: string
  }>
  edges: Array<{
    from: string
    to: string
    type: string
    label: string
  }>
  timeline: Array<{
    timestamp: number
    event_type: string
    step_id: string
    detail: Record<string, unknown>
    status: string
  }>
}

interface SagaVisualizerPanelProps {
  open: boolean
  onClose: () => void
  sendCommand: (command: string, payload?: unknown) => { ok: boolean; queued: boolean; message?: string }
  waitForIpcEvent: (
    eventName: string,
    timeoutMs: number,
    predicate?: (payload: Record<string, unknown>) => boolean
  ) => Promise<Record<string, unknown>>
  backendSocket: { status: string }
}

type VisualizationFormat = 'mermaid' | 'graphviz' | 'json' | 'ascii' | 'timeline'

const STATUS_COLORS: Record<string, string> = {
  PENDING: '#94a3b8',
  RUNNING: '#3b82f6',
  COMPENSATING: '#f59e0b',
  COMPLETED: '#22c55e',
  FAILED: '#ef4444',
  REQUIRES_RECONCILE: '#a855f7',
  QUARANTINED: '#6b7280',
}

const ENGINE_COLORS: Record<string, string> = {
  postgresql: '#336791',
  sqlite: '#003b57',
  qdrant: '#f59e0b',
  filesystem: '#6b7280',
  model: '#a855f7',
}

function buildNodes(op: SagaOperation) {
  const nodes = [{
    id: `op:${op.operation_id}`,
    label: `${op.operation_type}<br/>(${op.module_id})`,
    type: 'operation',
    status: op.status,
    engine: '',
  }]
  for (const step of op.nodes.filter(n => n.type === 'step')) {
    nodes.push({
      id: step.id,
      label: `${step.id}<br/>(${step.label})`,
      type: 'step',
      status: step.status,
      engine: step.engine,
    })
  }
  return nodes
}

function buildEdges(op: SagaOperation) {
  const edges: Array<{ from: string; to: string; type: string; label: string }> = []
  const steps = op.nodes.filter(n => n.type === 'step')
  for (const step of steps) {
    edges.push({ from: `op:${op.operation_id}`, to: step.id, type: 'depends_on', label: '' })
  }
  for (let i = 1; i < steps.length; i++) {
    edges.push({ from: steps[i - 1].id, to: steps[i].id, type: 'depends_on', label: '' })
  }
  return edges
}

function renderMermaid(op: SagaOperation): string {
  const lines = ['```mermaid', 'flowchart TD']

  for (const [status, color] of Object.entries(STATUS_COLORS)) {
    lines.push(`    classDef ${status.toLowerCase()} fill:${color},stroke:#333,stroke-width:2px`)
  }
  for (const [engine, color] of Object.entries(ENGINE_COLORS)) {
    lines.push(`    classDef ${engine.toLowerCase()} fill:${color},stroke:#333,stroke-width:1px,color:#fff`)
  }

  for (const node of buildNodes(op)) {
    const statusClass = node.status.toLowerCase().replace('_', '-')
    const engineClass = node.engine?.toLowerCase().replace('-', '-') || ''
    const classes = [statusClass, engineClass, node.type].filter(Boolean).join(' ')
    const label = node.label.replace('\n', '<br/>')
    lines.push(`    ${node.id}["${label}"]:::${classes}`)
  }

  for (const edge of buildEdges(op)) {
    const style = edge.type === 'compensation' ? '-.->' : '-->'
    const label = edge.label ? `|"${edge.label}"|` : ''
    lines.push(`    ${edge.from} ${style}${label} ${edge.to}`)
  }

  lines.push('```')
  return lines.join('\n')
}

function renderAscii(op: SagaOperation): string {
  const lines = [
    `Saga Operation: ${op.operation_id}`,
    `Type: ${op.operation_type} | Module: ${op.module_id} | Status: ${op.status}`,
    '',
    'Steps:',
  ]
  const steps = op.nodes.filter(n => n.type === 'step')
  for (let i = 0; i < steps.length; i++) {
    const prefix = i === steps.length - 1 ? '└──' : '├──'
    lines.push(`  ${prefix} ${steps[i].label} [${steps[i].status}]`)
  }
  return lines.join('\n')
}

function renderTimeline(op: SagaOperation): string {
  const lines = [
    `Timeline for ${op.operation_id}`,
    `Generated: ${new Date().toISOString()}`,
    '',
  ]
  if (op.timeline.length === 0) {
    lines.push('  (no events)')
    return lines.join('\n')
  }
  for (const event of op.timeline) {
    const time = new Date(event.timestamp * 1000).toISOString().slice(11, 23)
    const step = event.step_id ? ` [${event.step_id}]` : ''
    lines.push(`  ${time} ${event.event_type}${step} ${event.status}`)
  }
  return lines.join('\n')
}

function renderJson(op: SagaOperation): string {
  return JSON.stringify(op, null, 2)
}

function renderGraphviz(op: SagaOperation): string {
  const lines = ['digraph Saga {', '    rankdir=LR;']
  for (const node of buildNodes(op)) {
    const color = STATUS_COLORS[node.status] ?? '#94a3b8'
    const shape = node.type === 'operation' ? 'box' : node.type === 'compensation' ? 'diamond' : 'ellipse'
    const label = node.label.replace('<br/>', '\\n')
    lines.push(`    "${node.id}" [label="${label}", fillcolor="${color}", style="filled,rounded", shape="${shape}"];`)
  }
  for (const edge of buildEdges(op)) {
    const style = edge.type === 'compensation' ? 'dashed' : 'solid'
    lines.push(`    "${edge.from}" -> "${edge.to}" [style="${style}"];`)
  }
  lines.push('}')
  return lines.join('\n')
}

export function SagaVisualizerPanel({
  open,
  onClose,
  sendCommand,
  waitForIpcEvent,
  backendSocket,
}: SagaVisualizerPanelProps) {
  const [operations, setOperations] = useState<SagaOperation[]>([])
  const [selectedOpId, setSelectedOpId] = useState<string | null>(null)
  const [format, setFormat] = useState<VisualizationFormat>('mermaid')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>('')
  const connected = backendSocket.status === 'Connected'

  const loadOperations = useCallback(async () => {
    if (!connected) return
    setLoading(true)
    setError('')

    try {
      const result = sendCommand('app:get-saga-operations', {})
      if (!result.ok && !result.queued) {
        throw new Error(result.message || 'Failed to load operations')
      }
      const payload = await waitForIpcEvent(
        'app:get-saga-operations_result',
        10000,
      ) as { ok: boolean; operations?: SagaOperation[]; message?: string }
      if (payload.ok) {
        setOperations(payload.operations ?? [])
      } else {
        setError(payload.message || 'Failed to load operations')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [connected, sendCommand, waitForIpcEvent])

  const loadOperationDetails = useCallback(async (operationId: string) => {
    if (!connected) return
    setLoading(true)
    setError('')

    try {
      const result = sendCommand('app:get-saga-operation', { operation_id: operationId })
      if (!result.ok && !result.queued) {
        throw new Error(result.message || 'Failed to load operation')
      }
      const payload = await waitForIpcEvent(
        'app:get-saga-operation_result',
        10000,
        (p) => (p.operation as SagaOperation | undefined)?.operation_id === operationId
          || p.ok === false,
      ) as { ok: boolean; operation?: SagaOperation; message?: string }
      if (payload.ok && payload.operation) {
        const operation = payload.operation
        setOperations(prev => {
          const existing = prev.find(o => o.operation_id === operationId)
          if (existing) {
            return prev.map(o => o.operation_id === operationId ? operation : o)
          }
          return [...prev, operation]
        })
      } else {
        setError(payload.message || 'Failed to load operation')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [connected, sendCommand, waitForIpcEvent])

  const handleSelectOperation = useCallback((opId: string) => {
    setSelectedOpId(opId)
    loadOperationDetails(opId)
  }, [loadOperationDetails])

  useEffect(() => {
    if (open) {
      loadOperations()
    }
  }, [open, loadOperations])

  return (
    <BasePanel
      open={open}
      onClose={onClose}
      sendCommand={sendCommand}
      waitForIpcEvent={waitForIpcEvent}
      backendSocket={backendSocket}
      title={s.title}
      eyebrow={s.subtitle}
      icon={s.icon}
      headerActions={
        <div className="base-panel-actions">
          <select
            value={format}
            onChange={e => setFormat(e.target.value as VisualizationFormat)}
            className="base-panel-btn base-panel-btn--secondary"
            disabled={loading}
            aria-label={s.formatLabel}
          >
            <option value="mermaid">{s.mermaid}</option>
            <option value="graphviz">{s.graphviz}</option>
            <option value="json">{s.json}</option>
            <option value="ascii">{s.ascii}</option>
            <option value="timeline">{s.timeline}</option>
          </select>
          <button
            className="base-panel-btn base-panel-btn--secondary"
            onClick={loadOperations}
            disabled={loading}
          >
            {loading ? s.loading : s.refresh}
          </button>
          <button
            className="base-panel-btn base-panel-btn--secondary"
            onClick={() => setOperations([])}
            disabled={operations.length === 0}
          >
            {s.clear}
          </button>
        </div>
      }
    >
      {({ connected: panelConnected }) => (
        <>
          {!panelConnected && (
            <div className="base-panel__disconnected">
              {tb.disconnected}
            </div>
          )}

          {error && (
            <div className="base-panel__error">{error}</div>
          )}

          <div className="saga-visualizer__toolbar">
            <div className="saga-visualizer__operations-list">
              <h4>{s.selectOperation}</h4>
              {operations.length === 0 ? (
                <p className="saga-visualizer__empty">{s.noData}</p>
              ) : (
                <ul className="saga-visualizer__operations">
                  {operations.map(op => (
                    <li
                      key={op.operation_id}
                      className={`saga-visualizer__op-item ${selectedOpId === op.operation_id ? 'selected' : ''}`}
                      onClick={() => handleSelectOperation(op.operation_id)}
                    >
                      <div className="saga-visualizer__op-header">
                        <span className="saga-visualizer__op-id">{op.operation_id}</span>
                        <span className={`saga-visualizer__status saga-visualizer__status--${op.status.toLowerCase()}`}>
                          {op.status}
                        </span>
                      </div>
                      <div className="saga-visualizer__op-meta">
                        <span>{op.operation_type}</span>
                        <span>{op.module_id}</span>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="saga-visualizer__visualization">
              {selectedOpId ? (
                operations.find(o => o.operation_id === selectedOpId) ? (
                  <SagaVisualization
                    operation={operations.find(o => o.operation_id === selectedOpId)!}
                    format={format}
                  />
                ) : (
                  <div className="saga-visualizer__loading">{s.loading}</div>
                )
              ) : (
                <div className="saga-visualizer__empty-state">
                  <p>{s.noSelection}</p>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </BasePanel>
  )
}

function SagaVisualization({ operation, format }: { operation: SagaOperation; format: VisualizationFormat }) {
  let rendered: string
  switch (format) {
    case 'mermaid':
      rendered = renderMermaid(operation)
      break
    case 'graphviz':
      rendered = renderGraphviz(operation)
      break
    case 'json':
      rendered = renderJson(operation)
      break
    case 'ascii':
      rendered = renderAscii(operation)
      break
    case 'timeline':
      rendered = renderTimeline(operation)
      break
  }
  return <pre className={`saga-visualizer__${format}`}>{rendered}</pre>
}

export { SagaVisualizerPanel as default }
