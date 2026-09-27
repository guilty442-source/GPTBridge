//! token.rs — port of src-ui/main/ipcSession.ts (token half).
//!
//! Per-user IPC capability token: a 256-bit hex secret stored under the IPC
//! state root, hardened to owner + SYSTEM via icacls, created through a
//! locked atomic-write protocol so concurrent shells share exactly one
//! token.

use sha2::{Digest, Sha256};
use std::fs;
use std::path::PathBuf;
use std::sync::OnceLock;
use std::time::Duration;

use super::random_hex;
use crate::native::paths;

const TOKEN_FILE_NAME: &str = "session-token";
const TOKEN_LOCK_NAME: &str = ".session-token.lock";
const LOCK_WAIT_MS: u64 = 10_000;
const STALE_LOCK_MS: u64 = 5_000;

fn is_valid_token(token: &str) -> bool {
    token.len() == 64 && token.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

pub fn ipc_state_root() -> PathBuf {
    if let Ok(configured) = std::env::var("GPTBRIDGE_IPC_STATE_ROOT") {
        let trimmed = configured.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    if cfg!(windows) {
        let base = std::env::var("LOCALAPPDATA")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .or_else(|| {
                std::env::var("USERPROFILE")
                    .ok()
                    .map(|h| format!(r"{h}\AppData\Local"))
            })
            .unwrap_or_else(|| String::from(r"C:\Windows\Temp"));
        return PathBuf::from(base).join("GPTBridge").join("ipc");
    }
    let base = std::env::var("XDG_STATE_HOME")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| {
            std::env::var("HOME")
                .ok()
                .map(|h| format!("{h}/.local/state"))
        })
        .unwrap_or_else(|| String::from("/tmp"));
    PathBuf::from(base).join("GPTBridge").join("ipc")
}

fn token_file_path() -> PathBuf {
    ipc_state_root().join(TOKEN_FILE_NAME)
}

fn read_token(file_path: &std::path::Path) -> Option<String> {
    let token = fs::read_to_string(file_path).ok()?.trim().to_lowercase();
    if is_valid_token(&token) {
        Some(token)
    } else {
        None
    }
}

fn harden_private_path(target: &std::path::Path, directory: bool) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = if directory { 0o700 } else { 0o600 };
        let _ = fs::set_permissions(target, fs::Permissions::from_mode(mode));
    }
    #[cfg(windows)]
    {
        let username = std::env::var("USERNAME").unwrap_or_default();
        let username = username.trim();
        if username.is_empty() {
            return;
        }
        let user_permission = if directory { "(OI)(CI)(F)" } else { "(R,W)" };
        let system_permission = if directory { "(OI)(CI)(F)" } else { "(F)" };
        let _ = std::process::Command::new("icacls.exe")
            .arg(target)
            .args(["/inheritance:r", "/grant:r"])
            .arg(format!("{username}:{user_permission}"))
            .args(["/grant:r"])
            .arg(format!("*S-1-5-18:{system_permission}"))
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
    }
}

fn break_stale_lock(lock_path: &std::path::Path) {
    let Ok(meta) = fs::metadata(lock_path) else {
        return;
    };
    let Ok(modified) = meta.modified() else { return };
    let age = std::time::SystemTime::now()
        .duration_since(modified)
        .unwrap_or_default();
    if age.as_millis() < STALE_LOCK_MS as u128 {
        return;
    }
    let stale_path = lock_path.with_file_name(format!(
        "{}.stale-{}-{}",
        TOKEN_LOCK_NAME,
        std::process::id(),
        random_hex(6)
    ));
    if fs::rename(lock_path, &stale_path).is_ok() {
        let _ = fs::remove_dir(&stale_path);
    }
}

fn write_token_atomically(file_path: &std::path::Path, token: &str) -> std::io::Result<()> {
    let parent = file_path.parent().unwrap_or(file_path);
    let temp_path = parent.join(format!(
        ".{TOKEN_FILE_NAME}.{}.{}.tmp",
        std::process::id(),
        random_hex(8)
    ));
    {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp_path)?;
        use std::io::Write;
        file.write_all(format!("{token}\n").as_bytes())?;
        file.sync_all()?;
    }
    let result = fs::rename(&temp_path, file_path);
    if result.is_err() {
        let _ = fs::remove_file(&temp_path);
    }
    result
}

fn repair_or_create_token(file_path: &std::path::Path) -> Result<String, String> {
    let state_root = file_path.parent().unwrap_or(file_path).to_path_buf();
    fs::create_dir_all(&state_root).map_err(|e| e.to_string())?;
    harden_private_path(&state_root, true);

    let lock_path = state_root.join(TOKEN_LOCK_NAME);
    let deadline = std::time::Instant::now() + Duration::from_millis(LOCK_WAIT_MS);
    let owns_lock = false;
    let mut owner_nonce = String::new();

    while !owns_lock {
        if let Some(token) = read_token(file_path) {
            return Ok(token);
        }
        match fs::create_dir(&lock_path) {
            Ok(()) => {
                owner_nonce = random_hex(16);
                match fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(lock_path.join("owner"))
                    .and_then(|mut f| {
                        use std::io::Write;
                        f.write_all(format!("{owner_nonce}\n").as_bytes())
                    }) {
                    Ok(()) => break,
                    Err(e) => {
                        let _ = fs::remove_file(lock_path.join("owner"));
                        let _ = fs::remove_dir(&lock_path);
                        return Err(format!("lock owner write failed: {e}"));
                    }
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(format!("lock create failed: {e}")),
        }

        break_stale_lock(&lock_path);
        if std::time::Instant::now() >= deadline {
            if let Some(token) = read_token(file_path) {
                return Ok(token);
            }
            return Err(format!("Timed out acquiring IPC token lock: {lock_path:?}"));
        }
        std::thread::sleep(Duration::from_millis(25));
    }

    let still_owns_lock = |lock_path: &std::path::Path, nonce: &str| -> bool {
        if nonce.is_empty() {
            return false;
        }
        fs::read_to_string(lock_path.join("owner"))
            .map(|s| s.trim() == nonce)
            .unwrap_or(false)
    };

    let outcome = (|| -> Result<String, String> {
        if let Some(token) = read_token(file_path) {
            return Ok(token);
        }
        if !still_owns_lock(&lock_path, &owner_nonce) {
            return Err("Lost IPC token repair lock".to_string());
        }
        // Refresh lock mtime (utimes equivalent).
        let _ = filetime_touch(&lock_path);

        if file_path.exists() {
            let quarantine = state_root.join(format!(
                "{TOKEN_FILE_NAME}.invalid-{}-{}-{}",
                now_ms(),
                std::process::id(),
                random_hex(6)
            ));
            fs::rename(file_path, &quarantine).map_err(|e| e.to_string())?;
        }

        let generated = random_hex(32);
        write_token_atomically(file_path, &generated).map_err(|e| e.to_string())?;
        harden_private_path(file_path, false);
        read_token(file_path).ok_or_else(|| "IPC session token write verification failed".to_string())
    })();

    if still_owns_lock(&lock_path, &owner_nonce) {
        let _ = fs::remove_file(lock_path.join("owner"));
        let _ = fs::remove_dir(&lock_path);
    }
    outcome
}

fn filetime_touch(path: &std::path::Path) -> std::io::Result<()> {
    // Best-effort mtime refresh; a no-op failure is acceptable because lock
    // staleness only gates the quarantine path.
    fs::OpenOptions::new().write(true).open(path).map(|_| ())
}

fn now_ms() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

/// Per-user IPC capability token shared by the main application and
/// standalone platform-tool windows.
pub fn backend_session_token() -> Result<String, String> {
    static CACHED: OnceLock<Result<String, String>> = OnceLock::new();
    CACHED
        .get_or_init(|| {
            if let Ok(configured) = std::env::var("GPTBRIDGE_IPC_SESSION_TOKEN") {
                let configured = configured.trim().to_lowercase();
                if is_valid_token(&configured) {
                    return Ok(configured);
                }
            }
            let file_path = token_file_path();
            if let Some(existing) = read_token(&file_path) {
                if let Some(dir) = file_path.parent() {
                    harden_private_path(dir, true);
                }
                harden_private_path(&file_path, false);
                return Ok(existing);
            }
            repair_or_create_token(&file_path)
        })
        .clone()
}

/// Stable per-checkout identity — SHA-256 of the normalized workspace root,
/// truncated to 24 hex chars (parity with ipcSession.ts).
pub fn workspace_instance_id() -> String {
    let normalized = paths::path_library()
        .workspace_root
        .to_string_lossy()
        .replace('\\', "/");
    let normalized = if cfg!(windows) {
        normalized.to_lowercase()
    } else {
        normalized
    };
    let digest = Sha256::digest(normalized.as_bytes());
    hex::encode(digest)[..24].to_string()
}
