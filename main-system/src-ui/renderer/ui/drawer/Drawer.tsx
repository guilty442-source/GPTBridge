import { useEffect, useRef, useState, type ReactNode } from 'react'
import './drawer.css'

interface DrawerProps {
  open: boolean
  onClose: () => void
  title: string
  eyebrow?: string
  icon?: string
  children: ReactNode
  side?: 'right' | 'bottom'
}

export function Drawer({
  open,
  onClose,
  title,
  eyebrow,
  icon,
  children,
  side = 'right',
}: DrawerProps) {
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
    <div className={`drawer-overlay ${open ? 'is-open' : 'is-closing'}`} onClick={onClose}>
      <div
        className={`drawer-panel drawer-panel--${side} ${open ? 'is-open' : 'is-closing'}`}
        ref={panelRef}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header className="drawer-header">
          <div className="drawer-header__title">
            {icon && <span className="drawer-header__icon" aria-hidden="true">{icon}</span>}
            <div>
              {eyebrow && <span className="eyebrow">{eyebrow}</span>}
              <h2>{title}</h2>
            </div>
          </div>
          <button
            type="button"
            className="drawer-close"
            onClick={onClose}
            aria-label="關閉"
          >
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <path d="M4 4L14 14M14 4L4 14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </button>
        </header>
        <div className="drawer-body">{children}</div>
      </div>
    </div>
  )
}
