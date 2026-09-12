// Electron shim for main process
// At runtime, uses global set by electron-wrapper.js
// At build time, provides TypeScript types

import type { App, BrowserWindow, Dialog, IpcMain, Shell } from 'electron';

const electron = (globalThis as any).__ELECTRON__ || require('electron');

export const app: App = electron.app;
export const BrowserWindow: typeof BrowserWindow = electron.BrowserWindow;
export const dialog: Dialog = electron.dialog;
export const ipcMain: IpcMain = electron.ipcMain;
export const shell: Shell = electron.shell;
export default electron;