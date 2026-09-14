import { Component, type ErrorInfo, type ReactNode } from 'react'
import { mainSystemLocale } from '@/locales/main-system'

const mb = mainSystemLocale.moduleBoundary

type ModuleBoundaryProps = {
  name: string
  children: ReactNode
}

type ModuleBoundaryState = {
  failed: boolean
}

/**
 * Module fault isolation: a render error inside one module is contained to
 * that module and never blanks or stalls the rest of the UI.
 */
export class ModuleBoundary extends Component<
  ModuleBoundaryProps,
  ModuleBoundaryState
> {
  state: ModuleBoundaryState = { failed: false }

  static getDerivedStateFromError(): ModuleBoundaryState {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[module-boundary] ${this.props.name} failed`, error, info)
  }

  render(): ReactNode {
    if (this.state.failed) {
      return (
        <div className="module-fault" role="status" data-testid={`module-fault-${this.props.name}`}>
          <strong>{this.props.name}</strong>
          <span>{mb.fallbackMessage}</span>
        </div>
      )
    }
    return this.props.children
  }
}
