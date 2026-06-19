import type { SystemLight } from '@/core-system/core/systemHealth'
import { zhTW } from '@/i18n/zhTW'
import React, { useMemo, useState } from 'react'

type CriticalAlertProps = {
  lights: SystemLight[]
  onOpenRescue: () => void
}

export const CriticalAlert: React.FC<CriticalAlertProps> = ({
  lights,
  onOpenRescue,
}) => {
  const [dismissedKey, setDismissedKey] = useState('')
  const criticalLights = useMemo(
    () => lights.filter((light) => light.level === 'error'),
    [lights]
  )
  const alertKey = criticalLights
    .map((light) => `${light.name}:${light.detail}`)
    .join('|')

  if (criticalLights.length === 0 || alertKey === dismissedKey) return null

  return (
    <div
      className="critical-alert-backdrop"
      role="alertdialog"
      aria-modal="true"
    >
      <div className="critical-alert-panel">
        <h2>{zhTW.alert.title}</h2>
        <p>{zhTW.alert.body}</p>
        <ul>
          {criticalLights.map((light) => (
            <li key={light.name}>
              <strong>{light.name}</strong>
              <span>{light.detail}</span>
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="btn-base btn-primary"
          onClick={() => setDismissedKey(alertKey)}
        >
          {zhTW.alert.acknowledge}
        </button>
        <button type="button" className="btn-base" onClick={onOpenRescue}>
          {zhTW.alert.rescue}
        </button>
      </div>
    </div>
  )
}
