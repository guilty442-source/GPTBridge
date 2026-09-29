fn main() {
    // Register app-defined commands so tauri-build emits their
    // allow-/deny- permission entries; without this every invoke from the
    // webview fails with "Command gptbridge_invoke not allowed by ACL".
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["gptbridge_invoke"]),
        ),
    )
    .expect("tauri build failed");
}
