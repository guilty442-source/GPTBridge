/**
 * Runtime event bus used by renderer bootstrap and diagnostics.
 */
type Listener<T = unknown> = (data: T) => void

class RuntimeEventBus {
  private listeners: Record<string, Listener[]> = {}

  on<T = unknown>(event: string, callback: Listener<T>): () => void {
    if (!this.listeners[event]) this.listeners[event] = []
    this.listeners[event].push(callback as Listener)

    return () => {
      this.off(event, callback as Listener)
    }
  }

  off(event: string, callback: Listener): void {
    if (!this.listeners[event]) return
    this.listeners[event] = this.listeners[event].filter((fn) => fn !== callback)
    if (this.listeners[event].length === 0) {
      delete this.listeners[event]
    }
  }

  emit<T = unknown>(event: string, data: T): void {
    if (!this.listeners[event]) return
    for (const listener of this.listeners[event]) {
      listener(data)
    }
  }
}

export const eventBus = new RuntimeEventBus()
