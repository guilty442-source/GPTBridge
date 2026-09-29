//! paths.rs — port of src-ui/main/pathLibrary.ts.
//!
//! Source-production layout: the workspace root is the GPTBridge checkout
//! (``E:\GPTBridge`` style); a packaged layout resolves against the
//! ``resources/`` directory next to the executable.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

#[derive(Debug, Clone)]
pub struct RuntimePathLibrary {
    pub mode: &'static str,
    pub executable_dir: PathBuf,
    pub workspace_root: PathBuf,
    pub resources_root: PathBuf,
    pub app_root: PathBuf,
    pub unpacked_root: PathBuf,
    pub preload_entry: PathBuf,
    pub renderer_entry_html: PathBuf,
    pub python_executable: PathBuf,
    pub python_entry: PathBuf,
    pub boot_core_entry: PathBuf,
    /// Native backend host binary — the governed successor of the
    /// retired Python ``boot_core``/``main.py`` chain.
    pub native_backend_executable: PathBuf,
    pub python_source_repair_entry: PathBuf,
}

fn first_existing(candidates: &[PathBuf]) -> Option<PathBuf> {
    candidates.iter().find(|c| c.exists()).cloned()
}

fn has_workspace_markers(candidate: &Path) -> bool {
    let source_core = candidate.join("main-system").join("src-core").exists()
        || candidate.join("src-core").exists();
    candidate.join("governance_rule").exists()
        && candidate
            .join("governance_rule")
            .join("permission_directory")
            .exists()
        && source_core
}

fn python_executable_candidates_for(root: &Path) -> Vec<PathBuf> {
    if cfg!(windows) {
        vec![
            root.join(".venv").join("Scripts").join("pythonw.exe"),
            root.join(".venv").join("Scripts").join("python.exe"),
        ]
    } else {
        vec![root.join(".venv").join("bin").join("python")]
    }
}

fn resolve_from_path(name: &str) -> Option<PathBuf> {
    let command = if cfg!(windows) { "where.exe" } else { "which" };
    let output = std::process::Command::new(command)
        .arg(name)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .output()
        .ok()?;
    let first = output
        .stdout
        .split(|b| *b == b'\n' || *b == b'\r')
        .map(|s| String::from_utf8_lossy(s).trim().to_string())
        .find(|s| !s.is_empty())?;
    let path = PathBuf::from(first);
    if path.exists() {
        Some(path)
    } else {
        None
    }
}

/// Whether the running binary sits in a bundled resources layout.
/// Equivalent to Electron ``app.isPackaged``.
pub fn is_packaged() -> bool {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()));
    match exe_dir {
        Some(dir) => has_workspace_markers(&dir.join("resources")),
        None => false,
    }
}

pub fn path_library() -> &'static RuntimePathLibrary {
    static LIB: OnceLock<RuntimePathLibrary> = OnceLock::new();
    LIB.get_or_init(|| {
        let packaged = is_packaged();
        let executable_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.to_path_buf()))
            .unwrap_or_else(|| PathBuf::from("."));

        let packaged_resources_root = executable_dir.join("resources");
        let workspace_root = if packaged {
            // A packaged process stays inside its own resources directory.
            [packaged_resources_root.clone(), executable_dir.clone()]
                .iter()
                .find(|c| has_workspace_markers(c))
                .cloned()
                .unwrap_or_else(|| packaged_resources_root.clone())
        } else {
            // Source-production contract (matches pathLibrary.ts).
            // Fallback: walk up from the executable directory looking for
            // the governed workspace markers — a hardcoded checkout path
            // silently misroots any moved/renamed checkout.
            std::env::var("GPTBRIDGE_PROJECT_ROOT")
                .map(PathBuf::from)
                .ok()
                .filter(|p| has_workspace_markers(p))
                .or_else(|| {
                    executable_dir
                        .ancestors()
                        .find(|dir| has_workspace_markers(dir))
                        .map(|dir| dir.to_path_buf())
                })
                .unwrap_or_else(|| executable_dir.clone())
        };

        let resources_root = if packaged {
            packaged_resources_root
        } else {
            workspace_root.join("main-system").join("resources")
        };
        let app_root = if packaged {
            resources_root.join("app")
        } else {
            workspace_root.join("main-system")
        };
        let unpacked_root = if packaged {
            resources_root.join("app.asar.unpacked")
        } else {
            workspace_root.join("main-system")
        };

        let path_python = resolve_from_path(if cfg!(windows) { "python" } else { "python3" });
        let path_pythonw = if cfg!(windows) {
            resolve_from_path("pythonw")
        } else {
            None
        };
        let mut python_candidates: Vec<PathBuf> = Vec::new();
        for root in [&resources_root, &unpacked_root, &app_root, &workspace_root] {
            python_candidates.extend(python_executable_candidates_for(root));
        }
        if packaged {
            if let Some(p) = path_pythonw.clone() {
                python_candidates.push(p);
            }
            if let Some(p) = path_python.clone() {
                python_candidates.push(p);
            }
        } else {
            if let Some(p) = path_python {
                python_candidates.push(p);
            }
            if let Some(p) = path_pythonw {
                python_candidates.push(p);
            }
        }

        let mut python_executable =
            first_existing(&python_candidates).unwrap_or_else(|| python_candidates[0].clone());
        if cfg!(windows) {
            let lower = python_executable.to_string_lossy().to_lowercase();
            if lower.ends_with("\\python.exe") {
                let pythonw = python_executable.with_file_name("pythonw.exe");
                if pythonw.exists() {
                    python_executable = pythonw;
                }
            }
        }

        let python_entry = [
            resources_root.join("src-core").join("main.py"),
            unpacked_root.join("src-core").join("main.py"),
            app_root.join("src-core").join("main.py"),
            workspace_root.join("src-core").join("main.py"),
        ]
        .iter()
        .find(|p| p.exists())
        .cloned()
        .unwrap_or_else(|| resources_root.join("src-core").join("main.py"));

        let boot_core_entry = [
            resources_root.join("src-core").join("boot_core.py"),
            unpacked_root.join("src-core").join("boot_core.py"),
            app_root.join("src-core").join("boot_core.py"),
            workspace_root.join("src-core").join("boot_core.py"),
        ]
        .iter()
        .find(|p| p.exists())
        .cloned()
        .unwrap_or_else(|| python_entry.clone());

        let native_backend_executable = {
            let exe_name = if cfg!(windows) {
                "gptbridge-backend.exe"
            } else {
                "gptbridge-backend"
            };
            [
                executable_dir.join(exe_name),
                workspace_root
                    .join("main-system")
                    .join("src-tauri")
                    .join("target")
                    .join("release")
                    .join(exe_name),
                workspace_root
                    .join("main-system")
                    .join("src-tauri")
                    .join("target")
                    .join("debug")
                    .join(exe_name),
            ]
            .iter()
            .find(|p| p.exists())
            .cloned()
            .unwrap_or_else(|| executable_dir.join(exe_name))
        };

        let source_repair_entry = [
            resources_root
                .join("src-core")
                .join("tasks")
                .join("source_repair.py"),
            unpacked_root
                .join("src-core")
                .join("tasks")
                .join("source_repair.py"),
            app_root
                .join("src-core")
                .join("tasks")
                .join("source_repair.py"),
            workspace_root
                .join("src-core")
                .join("tasks")
                .join("source_repair.py"),
        ]
        .iter()
        .find(|p| p.exists())
        .cloned()
        .unwrap_or_else(|| {
            workspace_root
                .join("main-system")
                .join("src-core")
                .join("tasks")
                .join("source_repair.py")
        });

        let renderer_entry_html = workspace_root
            .join("main-system")
            .join("dist-ui")
            .join("renderer")
            .join("index.html");
        let preload_entry = workspace_root
            .join("main-system")
            .join("dist-ui")
            .join("main")
            .join("preload.js");

        RuntimePathLibrary {
            mode: if packaged {
                "packaged"
            } else {
                "source-production"
            },
            executable_dir,
            workspace_root,
            resources_root,
            app_root,
            unpacked_root,
            preload_entry,
            renderer_entry_html,
            python_executable,
            python_entry,
            boot_core_entry,
            native_backend_executable,
            python_source_repair_entry: source_repair_entry,
        }
    })
}

/// Path-inside check equivalent to the TS ``isPathInside`` helper.
pub fn is_path_inside(base: &Path, target: &Path) -> bool {
    let base = base.canonicalize().unwrap_or_else(|_| base.to_path_buf());
    let target = target
        .canonicalize()
        .unwrap_or_else(|_| target.to_path_buf());
    target.starts_with(&base)
}

pub fn runtime_env(name: &str) -> Option<String> {
    std::env::var(name).ok()
}
