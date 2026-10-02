//! abi.rs — `xtok_*` C ABI surface for the inference engine.
//!
//! B81 language ownership: the tokenizer is a Rust-owned capability;
//! the C++ engine reaches it only through this stable C ABI (the same
//! runtime-bound, fail-closed pattern as the CUDA lane — the dll is
//! resolved with LoadLibrary, never import-linked).
//!
//! Contract (xtok/v1):
//!   - Handles are opaque `void*`; `xtok_free` releases them. A freed or
//!     null handle is an argument error, never a panic.
//!   - Probe form: `out == NULL` (or `cap == 0`) returns the required
//!     element/byte count without writing — the C++ side sizes its
//!     vector first, then calls again to fill.
//!   - Return: >=0 element/byte count on success; negative rc on error:
//!       -1 = invalid argument / null handle / null data pointer
//!       -2 = out buffer capacity insufficient (required count is NOT
//!            written; use probe form)
//!       -3 = internal failure (panic caught, no state leaked)
//!   - The library never panics across the FFI boundary; every entry is
//!     wrapped in catch_unwind.
//!   - Zero allocation trust: lengths are checked before any deref.

use std::ffi::c_void;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;
use std::slice;

use crate::tokenizer::ByteLevelBpeTokenizer;

pub const XTOK_ABI_VERSION: u32 = 1;

// rc codes — negative values are stable ABI, matched by the C++ header.
const RC_ARG: i64 = -1;
const RC_CAP: i64 = -2;
const RC_INTERNAL: i64 = -3;

/// Opaque tokenizer handle. The tokenizer is immutable after load, so a
/// single handle may be shared across threads; the C++ engine calls it
/// from one thread today, but Send/Sync keeps the contract honest.
struct Xtok {
    inner: ByteLevelBpeTokenizer,
}
unsafe impl Send for Xtok {}
unsafe impl Sync for Xtok {}

#[no_mangle]
pub extern "C" fn xtok_abi_version() -> u32 {
    XTOK_ABI_VERSION
}

#[no_mangle]
pub extern "C" fn xtok_load(path_ptr: *const u8, path_len: usize) -> *mut c_void {
    let r = catch_unwind(AssertUnwindSafe(|| -> Result<*mut c_void, ()> {
        if path_ptr.is_null() || path_len == 0 || path_len > 1 << 20 {
            return Err(());
        }
        let bytes = unsafe { slice::from_raw_parts(path_ptr, path_len) };
        // Paths are raw OS bytes on the C side; UTF-8 lossy conversion
        // keeps the loader total.
        let path = PathBuf::from(String::from_utf8_lossy(bytes).into_owned());
        let tk = ByteLevelBpeTokenizer::load(&path).map_err(|_| ())?;
        Ok(Box::into_raw(Box::new(Xtok { inner: tk })) as *mut c_void)
    }));
    r.ok().and_then(|x| x.ok()).unwrap_or(std::ptr::null_mut())
}

#[no_mangle]
pub extern "C" fn xtok_free(h: *mut c_void) {
    if h.is_null() {
        return;
    }
    let _ = catch_unwind(AssertUnwindSafe(|| unsafe {
        drop(Box::from_raw(h as *mut Xtok));
    }));
}

#[no_mangle]
pub extern "C" fn xtok_vocab_size(h: *const c_void) -> i64 {
    if h.is_null() {
        return RC_ARG;
    }
    catch_unwind(AssertUnwindSafe(|| unsafe {
        (*(h as *const Xtok)).inner.vocab_size()
    }))
    .unwrap_or(RC_INTERNAL)
}

/// Encode `text` (raw bytes, UTF-8 lossy semantics identical to the C++
/// std::string input — byte-level BPE consumes bytes) into `out`.
/// Probe form (`out` null or `cap` 0) returns the token count.
#[no_mangle]
pub extern "C" fn xtok_encode(
    h: *const c_void,
    text_ptr: *const u8,
    text_len: usize,
    add_bos: i32,
    add_eos: i32,
    max_length: i64,
    out: *mut i64,
    cap: usize,
) -> i64 {
    if h.is_null() || (text_ptr.is_null() && text_len > 0) {
        return RC_ARG;
    }
    catch_unwind(AssertUnwindSafe(|| {
        let text = if text_len == 0 {
            &[][..]
        } else {
            unsafe { slice::from_raw_parts(text_ptr, text_len) }
        };
        let ids = unsafe { &*(h as *const Xtok) }.inner.encode_limited(
            text,
            add_bos != 0,
            add_eos != 0,
            max_length,
        );
        if out.is_null() || cap == 0 {
            return ids.len() as i64;
        }
        if ids.len() > cap {
            return RC_CAP;
        }
        unsafe {
            std::ptr::copy_nonoverlapping(ids.as_ptr(), out, ids.len());
        }
        ids.len() as i64
    }))
    .unwrap_or(RC_INTERNAL)
}

/// Decode `ids` into `out` as UTF-8 bytes. Probe form (`out` null or
/// `cap` 0) returns the byte count.
#[no_mangle]
pub extern "C" fn xtok_decode(
    h: *const c_void,
    ids_ptr: *const i64,
    ids_len: usize,
    skip_special: i32,
    out: *mut u8,
    cap: usize,
) -> i64 {
    if h.is_null() || (ids_ptr.is_null() && ids_len > 0) {
        return RC_ARG;
    }
    catch_unwind(AssertUnwindSafe(|| {
        let ids = if ids_len == 0 {
            &[][..]
        } else {
            unsafe { slice::from_raw_parts(ids_ptr, ids_len) }
        };
        let bytes =
            unsafe { &*(h as *const Xtok) }.inner.decode(ids, skip_special != 0);
        if out.is_null() || cap == 0 {
            return bytes.len() as i64;
        }
        if bytes.len() > cap {
            return RC_CAP;
        }
        unsafe {
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len());
        }
        bytes.len() as i64
    }))
    .unwrap_or(RC_INTERNAL)
}
