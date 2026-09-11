const { contextBridge, ipcRenderer } = require('electron')

const allowedInvokeChannels = new Set([
  'app:ensure-backend-started',
  'app:get-backend-session',
  'app:open-path',
  'dialog:select-folder',
  'dialog:validate-folder',
  'dialog:create-file',
  'dialog:open-file',
])

function invoke(channel, ...args) {
  if (!allowedInvokeChannels.has(channel)) {
    return Promise.reject(new Error(`Blocked IPC channel: ${channel}`))
  }
  return ipcRenderer.invoke(channel, ...args)
}

contextBridge.exposeInMainWorld('electron', { invoke })
contextBridge.exposeInMainWorld('gptBridge', {
  standaloneTool: false,
  selectFolder: () => invoke('dialog:select-folder'),
  validateFolder: (candidate = '') => invoke('dialog:validate-folder', candidate),
  createFile: (defaultPath = '') => invoke('dialog:create-file', defaultPath),
  openFile: (defaultPath = '') => invoke('dialog:open-file', defaultPath),
  openPath: (payload) => invoke('app:open-path', payload),
})
