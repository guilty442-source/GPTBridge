//! js_bridge — Tauri JS Bridge layer.
//!
//! The renderer-visible contract: a preload shim injected into every webview
//! (``window.electron`` is a compatibility alias for the governed Tauri
//! WebView2 preload only — no Electron runtime exists; Electron is
//! forbidden by the project language policy), a server-side channel
//! whitelist (identical to the retired preload.ts ``allowedInvokeChannels``),
//! the single governed ``gptbridge_invoke`` command dispatching into
//! ``channels``, and the token-guarded loopback bridge for tool UIs.

pub(crate) mod channels;
pub(crate) mod loopback;

/// Preload whitelist parity — identical to preload.ts allowedInvokeChannels.
const ALLOWED_CHANNELS: [&str; 25] = [
    "app:get-status",
    "app:get-perf-slo",
    "app:get-backend-session",
    "app:restart",
    "app:restart-backend",
    "app:ensure-backend-started",
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
    "embedded-browser:close-module",
];

/// The renderer-visible contract injected into every webview before scripts
/// run — replaces the Electron preload bridge.  Channel dispatch goes
/// through the single governed ``gptbridge_invoke`` command which re-checks
/// the whitelist server-side.
pub(crate) const PRELOAD_SHIM: &str = r#"
(function () {
  'use strict';
  var invoke = function (channel) {
    var args = Array.prototype.slice.call(arguments, 1);
    return window.__TAURI__.core.invoke('gptbridge_invoke', {
      channel: channel,
      args: args
    }).then(function (r) { return r; });
  };
  window.electron = { invoke: invoke };
  window.gptBridge = {
    selectFolder: function () { return invoke('dialog:select-folder'); },
    createFile: function (defaultPath) {
      return invoke('dialog:create-file', defaultPath || '');
    },
    openFile: function (defaultPath) {
      return invoke('dialog:open-file', defaultPath || '');
    },
    openPath: function (payload) { return invoke('app:open-path', payload); },
    restartApp: function () { return invoke('app:restart'); },
    restartBackend: function () { return invoke('app:restart-backend'); },
    ensureBackendStarted: function () { return invoke('app:ensure-backend-started'); }
  };
  // Reload shortcuts (Electron before-input-event parity).
  window.addEventListener('keydown', function (event) {
    var key = (event.key || '').toLowerCase();
    var reload = key === 'f5' || ((event.ctrlKey || event.metaKey) && key === 'r');
    if (!reload) return;
    event.preventDefault();
    invoke(event.shiftKey ? 'app:reload-window-hard' : 'app:reload-window');
  });
})();
"#;

#[tauri::command]
pub(crate) async fn gptbridge_invoke(
    app: tauri::AppHandle,
    channel: String,
    args: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let envelope = gptbridge_core::ipc::RequestEnvelope::from_legacy(
        channel.clone(),
        args.clone(),
    );
    envelope.validate()?;
    // Tool-window mode has its own whitelist and dispatch surface
    // (source-tool-ui-host contract, tool_dispatch.rs).
    if crate::tool_window::tool_window_requested() {
        return tauri::async_runtime::spawn_blocking(move || {
            tauri::async_runtime::block_on(crate::tool_dispatch::dispatch(app, &channel, args))
        })
        .await
        .map_err(|e| format!("dispatch join failed: {e}"));
    }
    if !ALLOWED_CHANNELS.contains(&channel.as_str()) {
        return Ok(serde_json::json!({
            "ok": false,
            "message": format!("Blocked IPC channel: {channel}")
        }));
    }
    // Electron handlers receive the first positional argument as payload;
    // the shim forwards the full array.
    let payload = if args.is_array() {
        args.get(0).cloned().unwrap_or(serde_json::Value::Null)
    } else {
        args
    };
    // dispatch does sync-only work (worker spawn up to 25s, loopback HTTP
    // up to 15s) — run it on the blocking pool so a slow embedded-browser
    // op never occupies an async-runtime thread.
    tauri::async_runtime::spawn_blocking(move || {
        tauri::async_runtime::block_on(channels::dispatch(app, &channel, payload))
    })
    .await
    .map_err(|e| format!("dispatch join failed: {e}"))
}
