export type IPCPayload = Record<string, unknown>

export interface IPCTransportMessage {
  command: string
  payload: IPCPayload
}

export type IPCSendHandler = (message: IPCTransportMessage) => void | Promise<void>

export class IPCTransport {
  private connected = false
  private handler: IPCSendHandler | null = null

  connect(handler?: IPCSendHandler): { ok: boolean; queued: number } {
    if (handler) this.handler = handler
    this.connected = true
    return { ok: true, queued: 0 }
  }

  disconnect(): void {
    this.connected = false
  }

  isConnected(): boolean {
    return this.connected
  }

  send(command: string, payload: IPCPayload = {}): { ok: boolean; queued: boolean } {
    const message = { command, payload }
    if (!this.connected) {
      return { ok: false, queued: false }
    }
    void this.dispatch(message)
    return { ok: true, queued: false }
  }

  private async dispatch(message: IPCTransportMessage): Promise<void> {
    if (!this.handler) return
    await this.handler(message)
  }
}
