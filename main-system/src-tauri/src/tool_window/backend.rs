//! Single-instance mutex + preload shim.
//!
//! The former packaged Python-backend spawn lane is retired (B166/B38):
//! zero Python interpreter, entry or fallback path may exist.

/// Per-tool single instance (Electron ``requestSingleInstanceLock``
/// parity): a second launch for the same tool exits instead of stacking a
/// duplicate window.
#[cfg(windows)]
pub(super) fn acquire_tool_mutex(tool_id: &str) -> bool {
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
    use windows_sys::Win32::System::Threading::CreateMutexW;
    let name: Vec<u16> = format!("Local\\gptbridge-tool-window-{tool_id}")
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    unsafe {
        let handle = CreateMutexW(std::ptr::null(), 0, name.as_ptr());
        if handle.is_null() {
            return true;
        }
        if GetLastError() == ERROR_ALREADY_EXISTS {
            return false;
        }
        // HANDLE is a raw pointer — holding the local binding keeps the
        // kernel mutex open for the process lifetime.
        let _ = handle;
        true
    }
}

#[cfg(not(windows))]
pub(super) fn acquire_tool_mutex(_tool_id: &str) -> bool {
    true
}

/// Preload parity with ``preload.cjs``: ``window.electron.invoke`` +
/// ``onEvent`` (the single whitelisted event channel is
/// ``embedded-browser:event``) and the ``window.gptBridge`` helper surface.
pub(super) const TOOL_PRELOAD_SHIM: &str = r#"
(function () {
  'use strict';
  var invoke = function (channel) {
    var args = Array.prototype.slice.call(arguments, 1);
    return window.__TAURI__.core.invoke('gptbridge_invoke', {
      channel: channel,
      args: args
    }).then(function (r) { return r; });
  };
  var onEvent = function (channel, callback) {
    if (channel !== 'embedded-browser:event' || typeof callback !== 'function') {
      return function () {};
    }
    var pending = window.__TAURI__.event.listen(channel, function (event) {
      callback(event.payload);
    });
    return function () { pending.then(function (unlisten) { unlisten(); }); };
  };
  window.electron = { invoke: invoke, onEvent: onEvent };
  window.gptBridge = {
    standaloneTool: false,
    selectFolder: function () { return invoke('dialog:select-folder'); },
    validateFolder: function (candidate) {
      return invoke('dialog:validate-folder', candidate || '');
    },
    createFile: function (defaultPath) {
      return invoke('dialog:create-file', defaultPath || '');
    },
    openFile: function (defaultPath) {
      return invoke('dialog:open-file', defaultPath || '');
    },
    openPath: function (payload) { return invoke('app:open-path', payload); },
    onEmbeddedBrowserEvent: function (callback) {
      return onEvent('embedded-browser:event', callback);
    }
  };
})();
"#;
