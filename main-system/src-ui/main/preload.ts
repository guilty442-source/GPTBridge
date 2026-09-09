import { contextBridge, ipcRenderer } from 'electron'

const allowedInvokeChannels = new Set([
  'app:get-status',
  'app:get-backend-session',
  'app:ensure-backend-started',
  'app:restart-backend',
  'app:restart',
  'app:get-repair-status',
  'app:get-platform-tool-sizes',
  'app:reload-window',
  'app:reload-window-hard',
  'app:get-ui-zoom',
  'app:set-ui-zoom',
  'app:open-path',
  'dialog:select-folder',
  'dialog:create-file',
  'dialog:open-file',
  'embedded-browser:create',
  'embedded-browser:navigate',
  'embedded-browser:execute',
  'embedded-browser:show',
  'embedded-browser:hide',
  'embedded-browser:close',
  'embedded-browser:resize',
  'embedded-browser:list',
  'embedded-browser:url',
  'embedded-browser:close-module',
])

contextBridge.exposeInMainWorld('electron', {
  invoke: (channel: string, ...args: unknown[]) => {
    if (!allowedInvokeChannels.has(channel)) {
      return Promise.reject(new Error(`Blocked IPC channel: ${channel}`))
    }
    return ipcRenderer.invoke(channel, ...args)
  },
})

contextBridge.exposeInMainWorld('gptBridge', {
  selectFolder: () => ipcRenderer.invoke('dialog:select-folder'),
  createFile: (defaultPath = '') =>
    ipcRenderer.invoke('dialog:create-file', defaultPath),
  openFile: (defaultPath = '') =>
    ipcRenderer.invoke('dialog:open-file', defaultPath),
  openPath: (payload: unknown) => ipcRenderer.invoke('app:open-path', payload),
  restartApp: () => ipcRenderer.invoke('app:restart'),
})
