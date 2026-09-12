// Electron shim - provides access to Electron's built-in modules
// Uses global set by electron-wrapper.js

const electron = globalThis.__ELECTRON__ || require('electron');
export default electron;