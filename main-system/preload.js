"use strict";
const electron = require("electron");
const allowedInvokeChannels = /* @__PURE__ */ new Set([
  "app:get-status",
  "app:get-backend-session",
  "app:restart",
  "app:restart-backend",
  "app:ensure-backend-started",
  "app:get-repair-status",
  "app:get-platform-tool-sizes",
  "app:reload-window",
  "app:reload-window-hard",
  "app:get-ui-zoom",
  "app:set-ui-zoom",
  "app:open-path",
  "dialog:select-folder",
  "dialog:create-file",
  "dialog:open-file",
  "embedded-browser:create",
  "embedded-browser:navigate",
  "embedded-browser:execute",
  "embedded-browser:show",
  "embedded-browser:hide",
  "embedded-browser:close",
  "embedded-browser:resize",
  "embedded-browser:list",
  "embedded-browser:url",
  "embedded-browser:close-module"
]);
electron.contextBridge.exposeInMainWorld("electron", {
  invoke: (channel, ...args) => {
    if (!allowedInvokeChannels.has(channel)) {
      return Promise.reject(new Error(`Blocked IPC channel: ${channel}`));
    }
    return electron.ipcRenderer.invoke(channel, ...args);
  }
});
electron.contextBridge.exposeInMainWorld("gptBridge", {
  selectFolder: () => electron.ipcRenderer.invoke("dialog:select-folder"),
  createFile: (defaultPath = "") => electron.ipcRenderer.invoke("dialog:create-file", defaultPath),
  openFile: (defaultPath = "") => electron.ipcRenderer.invoke("dialog:open-file", defaultPath),
  openPath: (payload) => electron.ipcRenderer.invoke("app:open-path", payload),
  restartApp: () => electron.ipcRenderer.invoke("app:restart"),
  restartBackend: () => electron.ipcRenderer.invoke("app:restart-backend"),
  ensureBackendStarted: () => electron.ipcRenderer.invoke("app:ensure-backend-started")
});
