//! process.rs — process liveness and bounded termination helpers.

use std::fs;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::native::paths::{self, RuntimePathLibrary};

pub(crate) fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                return false;
            }
            let mut code: u32 = 0;
            let ok = GetExitCodeProcess(handle, &mut code);
            CloseHandle(handle);
            return ok != 0 && code == 259; // STILL_ACTIVE
        }
    }
    #[cfg(unix)]
    {
        std::path::Path::new(&format!("/proc/{pid}")).exists()
    }
    #[allow(unreachable_code)]
    false
}

pub(crate) fn taskkill_pid(pid: u32) {
    let _ = Command::new("taskkill.exe")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|mut c| {
            // taskkill that never reports must not block the quit path.
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                match c.try_wait() {
                    Ok(Some(_)) | Err(_) => break,
                    Ok(None) if Instant::now() > deadline => break,
                    Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                }
            }
        });
}

pub(crate) fn kill_managed_backend_processes(lib: &RuntimePathLibrary, exclude_pid: Option<u32>) {
    // Stop both sides of the managed pair; an attached backend has no child
    // handle at all, so read pids from the boot-core state file.
    let state_path = lib
        .workspace_root
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("boot-core.json");
    let Ok(raw) = fs::read_to_string(&state_path) else {
        return;
    };
    let Ok(state_json) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return;
    };
    for key in ["pid", "backend_pid"] {
        let pid = state_json[key].as_u64().unwrap_or(0) as u32;
        if pid == 0 || Some(pid) == exclude_pid {
            continue;
        }
        if !pid_alive(pid) {
            continue;
        }
        taskkill_pid(pid);
    }
}

/// Convenience wrapper resolving the shared path library.
pub(crate) fn kill_managed_backend_processes_now(exclude_pid: Option<u32>) {
    kill_managed_backend_processes(paths::path_library(), exclude_pid);
}
