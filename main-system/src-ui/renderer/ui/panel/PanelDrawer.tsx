import { useEffect, useRef, useState, type ReactNode } from 'react'
import { mainSystemLocale } from '@/locales/main-system'
import './PanelDrawer.css'

export interface PanelDrawerProps {
  open: boolean
  onClose: () => void
  title: string
  eyebrow?: string
  icon?: string
  children: ReactNode
  side?: 'right' | 'bottom'
  className?: string
  headerActions?: ReactNode
}

export function PanelDrawer({
  open,
  onClose,
  title,
  eyebrow,
  icon,
  children,
  side = 'right',
  className = '',
  headerActions,
}: PanelDrawerProps) {
  const [rendered, setRendered] = useState(open)
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (open) {
      setRendered(true)
    } else {
      const timer = window.setTimeout(() => setRendered(false), 280)
      return () => window.clearTimeout(timer)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose])

  if (!rendered) return null

  return (
    <div className={`panel-drawer-overlay ${open ? 'is-open' : 'is-closing'} ${className}`} onClick={onClose}>
      <div
        className={`panel-drawer-panel panel-drawer-panel--${side} ${open ? 'is-open' : 'is-closing'}`}
        ref={panelRef}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header className="panel-drawer-header">
          <div className="panel-drawer-header__title">
            {icon && <span className="panel-drawer-header__icon" aria-hidden="true">{icon}</span>}
            <div>
              {eyebrow && <span className="eyebrow">{eyebrow}</span>}
              <h2>{title}</h2>
            </div>
          </div>
          <div className="panel-drawer-header__actions">
            {headerActions}
            <button
              type="button"
              className="panel-drawer-close"
              onClick={onClose}
              aria-label={mainSystemLocale.common.close}
            >
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
                <path d="M4 4L14 14M14 4L4 14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        </header>
        <div className="panel-drawer-body">{children}</div>
      </div>
    </div>
  )
}