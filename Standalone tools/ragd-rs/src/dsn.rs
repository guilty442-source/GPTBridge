//! Governed DSN resolution — mirrors shared_layer/security/dsn_policy.py:
//! env value `credman:GPTBridge/<target>` resolves through the Windows
//! credential store; a plain env/arg value is the literal DSN.

const CREDMAN_SCHEME: &str = "credman:";
const RUNTIME_ENV: &str = "GPTBRIDGE_POSTGRES_DSN";

pub fn resolve_dsn(explicit: Option<&str>) -> Result<String, String> {
    let raw = match explicit {
        Some(v) if !v.trim().is_empty() => v.trim().to_string(),
        _ => std::env::var(RUNTIME_ENV)
            .map_err(|_| format!("runtime DSN unavailable: {} unset", RUNTIME_ENV))?,
    };
    if let Some(target) = raw.strip_prefix(CREDMAN_SCHEME) {
        if target.trim().is_empty() {
            return Err("runtime DSN unavailable: empty credman target".to_string());
        }
        return credman_read(target.trim());
    }
    if raw.trim().is_empty() {
        return Err("runtime DSN unavailable: empty binding".to_string());
    }
    Ok(raw)
}

#[cfg(windows)]
fn credman_read(target: &str) -> Result<String, String> {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;

    #[repr(C)]
    struct FileTime {
        low: u32,
        high: u32,
    }
    #[repr(C)]
    struct CredentialW {
        flags: u32,
        cred_type: u32,
        target_name: *mut u16,
        comment: *mut u16,
        last_written: FileTime,
        blob_size: u32,
        blob: *mut u8,
        persist: u32,
        attr_count: u32,
        attributes: *mut core::ffi::c_void,
        target_alias: *mut u16,
        user_name: *mut u16,
    }
    #[link(name = "advapi32")]
    extern "system" {
        fn CredReadW(
            target: *const u16,
            cred_type: u32,
            flags: u32,
            credential: *mut *mut CredentialW,
        ) -> i32;
        fn CredFree(buffer: *mut core::ffi::c_void);
    }
    const CRED_TYPE_GENERIC: u32 = 1;

    let wide: Vec<u16> = OsStr::new(target)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let mut cred: *mut CredentialW = core::ptr::null_mut();
    let rc = unsafe { CredReadW(wide.as_ptr(), CRED_TYPE_GENERIC, 0, &mut cred) };
    if rc == 0 || cred.is_null() {
        return Err(format!("credman:{} not found", target));
    }
    let out = unsafe {
        let c = &*cred;
        let bytes = std::slice::from_raw_parts(c.blob, c.blob_size as usize);
        let text = String::from_utf8_lossy(bytes).to_string();
        CredFree(cred as *mut core::ffi::c_void);
        text
    };
    if out.trim().is_empty() {
        return Err(format!("credman:{} empty secret", target));
    }
    Ok(out)
}

#[cfg(not(windows))]
fn credman_read(target: &str) -> Result<String, String> {
    Err(format!(
        "credman:{} resolution unsupported on this platform",
        target
    ))
}
