//! Packaged-backend spawn + health probe + single-instance mutex.


use std::time::Duration;

use gptbridge_core::ipc::http as http_util;

use super::{
    BACKEND_READY_TIMEOUT, ToolConfig,
    tool_log,
};

fn backend_healthy(port: u16) -> bool {
    http_util::get(
        "127.0.0.1",
        port,
        "/health",
        &[],
        Duration::from_millis(500),
    )
    .map(|r| r.status == 200)
    .unwrap_or(false)
}

/// Spawn the packaged Python backend (template ``ensureBackendStarted``
/// parity — the lean path: reuse a healthy listener, otherwise spawn the
/// bundled interpreter with the governed IPC env and wait for /health).
pub(super) fn spawn_packaged_backend(config: &ToolConfig) -> Result<u32, String> {
    let Some(backend) = &config.packaged_backend else {
        return Ok(0);
    };
    if backend_healthy(backend.port) {
        tool_log("backend.reuse", &config.tool_id);
        return Ok(0);
    }

    let mut command = std::process::Command::new(&backend.python);
    command
        .args(["-B", "-s", "-E", "-X", "utf8"])
        .arg(&backend.entry)
        .current_dir(&config.tool_root)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONNOUSERSITE", "1")
        .env(
            "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT",
            std::env::var("GPTBRIDGE_PROJECT_ROOT")
                .unwrap_or_else(|_| config.workspace_root.to_string_lossy().into_owned()),
        )
        .env("GPTBRIDGE_TOOL_DIR", &config.tool_root)
        .env(
            "GPTBRIDGE_TOOL_DATA_ROOT",
            std::env::var("GPTBRIDGE_TOOL_DATA_ROOT").unwrap_or_else(|_| {
                config
                    .tool_root
                    .join("runtime")
                    .to_string_lossy()
                    .into_owned()
            }),
        )
        .env(
            "GPTBRIDGE_IPC_STATE_ROOT",
            config.tool_root.join("runtime").join("ipc"),
        )
        .env("GPTBRIDGE_IPC_SESSION_TOKEN", &config.backend_token)
        .env("GPTBRIDGE_SHUTDOWN_TOKEN", &config.shutdown_token)
        .env("GPTBRIDGE_IPC_PORT", backend.port.to_string())
        .env("GPTBRIDGE_TOOL_ID", &config.tool_id)
        .env_remove("GPTBRIDGE_PYTHON")
        .env_remove("GPTBRIDGE_ALLOW_SYSTEM_PYTHON")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let child = command
        .spawn()
        .map_err(|e| format!("BACKEND_PROCESS_SPAWN_FAILED:{e}"))?;
    let pid = child.id();
    drop(child);

    let deadline = std::time::Instant::now() + BACKEND_READY_TIMEOUT;
    while std::time::Instant::now() < deadline {
        if backend_healthy(backend.port) {
            tool_log("backend.ready", &config.tool_id);
            return Ok(pid);
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err("BACKEND_STARTUP_TIMEOUT".to_string())
}

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
