// Wrapper that loads electron first and makes it available globally
// This runs as the main entry point (package.json main)

// Load electron first - this should get the real Electron API
const electron = require('electron');
console.log('[wrapper] electron loaded:', typeof electron, electron?.app ? 'has app' : 'no app');

// Make it available globally
globalThis.__ELECTRON__ = electron;

// Now load the actual main process
require('./main.js');