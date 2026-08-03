import type { GPTBridgeAPI } from './shared/components/bridge'

export {}

declare global {
  interface Window {
    gptBridge?: GPTBridgeAPI
    electron?: {
      invoke: (channel: string, ...args: unknown[]) => Promise<unknown>
    }
  }
}
