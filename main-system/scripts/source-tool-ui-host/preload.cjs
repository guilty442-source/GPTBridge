const { contextBridge, ipcRenderer } = require('electron')

const allowedInvokeChannels = new Set([
  'app:ensure-backend-started',
  'app:get-backend-session',
  'app:open-path',
  'dialog:select-folder',
  'dialog:validate-folder',
  'dialog:create-file',
  'dialog:open-file',
  'embedded-browser:create',
  'embedded-browser:navigate',
  'embedded-browser:execute',
  'embedded-browser:show',
  'embedded-browser:hide',
  'embedded-browser:close',
  'embedded-browser:resize',
  'embedded-browser:reload',
  'embedded-browser:go-back',
  'embedded-browser:go-forward',
  'embedded-browser:state',
  'embedded-browser:list',
  'embedded-browser:url',
  'embedded-browser:close-module',
])

const allowedEventChannels = new Set(['embedded-browser:event'])

function invoke(channel, ...args) {
  if (!allowedInvokeChannels.has(channel)) {
    return Promise.reject(new Error(`Blocked IPC channel: ${channel}`))
  }
  return ipcRenderer.invoke(channel, ...args)
}

function onEvent(channel, callback) {
  if (!allowedEventChannels.has(channel) || typeof callback !== 'function') {
    return () => {}
  }
  const listener = (_event, payload) => callback(payload)
  ipcRenderer.on(channel, listener)
  return () => ipcRenderer.removeListener(channel, listener)
}

contextBridge.exposeInMainWorld('electron', { invoke, onEvent })
contextBridge.exposeInMainWorld('gptBridge', {
  standaloneTool: false,
  selectFolder: () => invoke('dialog:select-folder'),
  validateFolder: (candidate = '') => invoke('dialog:validate-folder', candidate),
  createFile: (defaultPath = '') => invoke('dialog:create-file', defaultPath),
  openFile: (defaultPath = '') => invoke('dialog:open-file', defaultPath),
  openPath: (payload) => invoke('app:open-path', payload),
  onEmbeddedBrowserEvent: (callback) => onEvent('embedded-browser:event', callback),
})