// Preload patch for electron module
console.log('[electron-patch] Patching electron module...');
console.log('[electron-patch] require.cache before:', Object.keys(require.cache).filter(k => k.includes('electron')));

// Try to get the real electron module
try {
  // Delete the npm package from cache if present
  const electronPkgPath = require.resolve('electron');
  console.log('[electron-patch] electron package path:', electronPkgPath);
  delete require.cache[electronPkgPath];

  // Now require should get the built-in module
  const electron = require('electron');
  console.log('[electron-patch] electron after patch:', typeof electron, electron?.app ? 'has app' : 'no app');

  // Cache it
  require.cache[electronPkgPath] = { exports: electron, id: electronPkgPath };
  console.log('[electron-patch] Patched cache');
} catch (err) {
  console.error('[electron-patch] Error:', err.message);
}