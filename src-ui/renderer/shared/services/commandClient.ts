/**
 * 真實的命令發送客戶端，負責與後端 127.0.0.1:8765 通訊
 */
import { getAuthenticatedBackendWebSocketUrl } from './backendSession'

export const commandClient = {
  send: async (command: string, payload: any = {}): Promise<any> => {
    const websocketUrl = await getAuthenticatedBackendWebSocketUrl()
    return new Promise((resolve, reject) => {
      // 直接建立 WebSocket 連線執行高優先級命令
      const ws = new WebSocket(websocketUrl)
      let settled = false
      const finish = (callback: () => void) => {
        if (settled) return
        settled = true
        window.clearTimeout(timer)
        callback()
      }

      ws.onopen = () => {
        ws.send(JSON.stringify({ command, payload }))
      }

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          // 簡單匹配回應
          if (data.event?.includes(command) || data.event === 'error') {
            finish(() => {
              resolve(data.payload)
              ws.close()
            })
          }
        } catch (e) {
          finish(() => reject(e))
        }
      }

      ws.onerror = (err) => finish(() => reject(err))
      // 設置超時
      const timer = window.setTimeout(() => {
        finish(() => {
          ws.close()
          reject(new Error('IPC Command Timeout'))
        })
      }, 10000)
    })
  },
}
