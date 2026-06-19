import { zhTW } from '@/i18n/zhTW'
import type { TaskProgress } from '@/types/ui'
import React from 'react'

type ModeProgressProps = {
  progress?: TaskProgress
}

const stageLabel = (stage?: string) => {
  const stages = zhTW.workflow.stages as Record<string, string>
  if (!stage) return zhTW.workflow.idle
  return stages[stage] ?? stage
}

const phaseLabel = (phase?: string) => {
  const phases = zhTW.workflow.phases as Record<string, string>
  if (!phase) return ''
  return phases[phase] ?? ''
}

const statusLabel = (status?: string) => {
  if (status === 'completed') return zhTW.workflow.completed
  if (status === 'cancelled') return zhTW.workflow.cancelled
  if (status === 'failed') return zhTW.workflow.failed
  if (
    status === 'waiting_approval' ||
    status === 'waiting_recovery' ||
    status === 'recovery_requested'
  )
    return zhTW.workflow.waitingApproval
  if (status === 'blocked') return zhTW.workflow.blocked
  if (status === 'running') return zhTW.workflow.running
  return zhTW.workflow.idle
}

const progressTone = (status?: string) => {
  if (status === 'failed') return 'progress-danger progress-flashing'
  if (status === 'cancelled' || status === 'blocked') return 'progress-danger'
  if (
    status === 'waiting_approval' ||
    status === 'waiting_recovery' ||
    status === 'recovery_requested'
  )
    return 'progress-warning'
  return 'progress-normal'
}

export const ModeProgress: React.FC<ModeProgressProps> = ({ progress }) => {
  const rawPercent = Number(progress?.percent)
  const percent = Number.isNaN(rawPercent)
    ? 0
    : Math.max(0, Math.min(100, rawPercent))
  const stageText = stageLabel(progress?.stage)
  const phaseText = phaseLabel(progress?.phase)
  return (
    <section className="section progress-panel">
      <div className="progress-header">
        <h2>{zhTW.workflow.title}</h2>
        <strong>{percent}%</strong>
      </div>
      <div className="progress-stage">
        <span>{phaseText ? `${stageText}｜${phaseText}` : stageText}</span>
        <b>{statusLabel(progress?.status)}</b>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={phaseText ? `${stageText} ${phaseText}` : stageText}
      >
        <div
          className={`progress-fill ${progressTone(progress?.status)}`}
          style={{ width: `${percent}%` }}
        />
      </div>
    </section>
  )
}
