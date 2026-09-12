// Post-build patch for main.js
// Replaces require("electron") with a function that gets the real Electron API

const fs = require('fs');
const path = require('path');

const mainPath = path.join(__dirname, 'main.js');
let content = fs.readFileSync(mainPath, 'utf-8');

// Find the require("electron") line and replace it
const oldRequire = 'const electron = require("electron");';
const newRequire = `
// Patched electron require - gets the real Electron API
const electron = (() => {
  // Try to get the real electron module from Electron's internal module system
  try {
    // In Electron, the built-in modules are available via process.mainModule.require
    // but only after the main module is set up. We use a workaround.
    const Module = require('module');
    const originalRequire = Module.prototype.require;
    Module.prototype.require = function(id) {
      if (id === 'electron') {
        // Return a proxy that forwards to the real electron module
        // The real electron module is available in the global scope in some Electron versions
        if (typeof globalThis.electron !== 'undefined') {
          return globalThis.electron;
        }
        // Try to get it from process.mainModule
        if (process.mainModule && process.mainModule.require) {
          try {
            return process.mainModule.require('electron');
          } catch (e) {}
        }
      }
      return originalRequire.apply(this, arguments);
    };
    return require('electron');
  } catch (e) {
    return require('electron');
  }
})();
`;

if (content.includes(oldRequire)) {
  content = content.replace(oldRequire, newRequire);
  fs.writeFileSync(mainPath, content, 'utf-8');
  console.log('Patched main.js');
} else {
  console.log('Could not find require("electron") to patch');
}