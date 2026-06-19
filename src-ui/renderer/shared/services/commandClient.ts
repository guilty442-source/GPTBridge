/**
 * 真實的命令發送客戶端，負責與後端 127.0.0.1:8765 通訊
 */
export const commandClient = {
  send: async (command: string, payload: any = {}): Promise<any> => {
    return new Promise((resolve, reject) => {
      // 直接建立 WebSocket 連線執行高優先級命令
      const ws = new WebSocket('ws://127.0.0.1:8765')

      ws.onopen = () => {
        ws.send(JSON.stringify({ command, payload }))
      }

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          // 簡單匹配回應
          if (data.event?.includes(command) || data.event === 'error') {
            resolve(data.payload)
            ws.close()
          }
        } catch (e) {
          reject(e)
        }
      }

      ws.onerror = (err) => reject(err)
      // 設置超時
      setTimeout(() => {
        ws.close()
        reject(new Error('IPC Command Timeout'))
      }, 10000)
    })
  },
}
