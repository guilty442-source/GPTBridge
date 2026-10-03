//! Stable read-only C ABI to the same engine used by Rust RAG.
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::{Mutex, OnceLock},
};
const MAX_INPUT: usize = 1024 * 1024;
const MAX_OUTPUT: usize = 8 * 1024 * 1024;
const MAX_OUTSTANDING: usize = 16;
static BUFFERS: OnceLock<Mutex<HashMap<usize, Box<[u8]>>>> = OnceLock::new();

#[repr(C)]
pub struct XstoreSqlBuffer {
    pub data: *const u8,
    pub length: usize,
}
fn empty() -> XstoreSqlBuffer {
    XstoreSqlBuffer {
        data: std::ptr::null(),
        length: 0,
    }
}
fn request(bytes: &[u8]) -> Result<Value, String> {
    let request: Value =
        serde_json::from_slice(bytes).map_err(|_| "NATIVE_SQL_ENVELOPE_INVALID")?;
    if request["format"] != "xstore-native-sql-request/v1" {
        return Err("NATIVE_SQL_ENVELOPE_VERSION".into());
    }
    let store = request["store"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or("NATIVE_SQL_STORE_REQUIRED")?;
    let statement = request["sql"]
        .as_str()
        .ok_or("NATIVE_SQL_STATEMENT_REQUIRED")?;
    let params = request["params"]
        .as_array()
        .ok_or("NATIVE_SQL_PARAMETERS_REQUIRED")?;
    let session = crate::sql::Session::open(std::path::Path::new(store))?;
    let rows = session.query(statement, params)?;
    Ok(json!({"format":"xstore-native-sql-result/v1","ok":true,"rows":rows}))
}
fn allocate(value: Value) -> XstoreSqlBuffer {
    let mut bytes = serde_json::to_vec(&value).unwrap_or_default();
    if bytes.len() > MAX_OUTPUT {
        bytes=serde_json::to_vec(&json!({"format":"xstore-native-sql-result/v1","ok":false,"error":"NATIVE_SQL_OUTPUT_LIMIT"})).unwrap();
    }
    let Ok(mut buffers) = BUFFERS.get_or_init(|| Mutex::new(HashMap::new())).lock() else {
        return empty();
    };
    if buffers.len() >= MAX_OUTSTANDING {
        return empty();
    }
    let boxed = bytes.into_boxed_slice();
    let data = boxed.as_ptr();
    let length = boxed.len();
    buffers.insert(data as usize, boxed);
    XstoreSqlBuffer { data, length }
}

/// Caller supplies a readable immutable byte span for the duration of this call.
/// Null / zero / oversized spans are rejected before dereference. Arbitrary
/// invalid non-null addresses remain a caller contract violation (standard C ABI).
/// Returned bytes are NOT NUL terminated; read exactly `length`. The pointer
/// remains valid until `xstore_sql_buffer_free(data)`; never free it elsewhere.
/// Null result means outstanding-buffer capacity or allocator/lock failure.
#[no_mangle]
pub unsafe extern "C" fn xstore_sql_query_json(data: *const u8, length: usize) -> XstoreSqlBuffer {
    let result = std::panic::catch_unwind(|| {
        let response = if data.is_null() || length == 0 || length > MAX_INPUT {
            Err("NATIVE_SQL_INPUT_SPAN_INVALID".into())
        } else {
            request(unsafe { std::slice::from_raw_parts(data, length) })
        };
        let value = match response {
            Ok(value) => value,
            Err(error) => json!({"format":"xstore-native-sql-result/v1","ok":false,"error":error}),
        };
        allocate(value)
    });
    result.unwrap_or_else(|_| empty())
}

/// Frees exactly this engine's live allocation. Unknown, null and repeated
/// pointers return 0 without dereference; successful release returns 1.
#[no_mangle]
pub extern "C" fn xstore_sql_buffer_free(data: *const u8) -> i32 {
    if data.is_null() {
        return 0;
    }
    let Some(buffers) = BUFFERS.get() else {
        return 0;
    };
    let Ok(mut buffers) = buffers.lock() else {
        return 0;
    };
    if buffers.remove(&(data as usize)).is_some() {
        1
    } else {
        0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn call(input: &[u8]) -> Value {
        let buffer = unsafe { xstore_sql_query_json(input.as_ptr(), input.len()) };
        assert!(!buffer.data.is_null());
        let result: Value = serde_json::from_slice(unsafe {
            std::slice::from_raw_parts(buffer.data, buffer.length)
        })
        .unwrap();
        assert_eq!(xstore_sql_buffer_free(buffer.data), 1);
        assert_eq!(xstore_sql_buffer_free(buffer.data), 0);
        result
    }
    #[test]
    fn invalid_envelopes_and_buffer_ownership() {
        assert_eq!(call(b"not-json")["error"], "NATIVE_SQL_ENVELOPE_INVALID");
        assert_eq!(call(b"{}")["error"], "NATIVE_SQL_ENVELOPE_VERSION");
        let buffer = unsafe { xstore_sql_query_json(std::ptr::null(), 42) };
        let result: Value = serde_json::from_slice(unsafe {
            std::slice::from_raw_parts(buffer.data, buffer.length)
        })
        .unwrap();
        assert_eq!(result["error"], "NATIVE_SQL_INPUT_SPAN_INVALID");
        assert_eq!(xstore_sql_buffer_free(buffer.data), 1);
        assert_eq!(xstore_sql_buffer_free(1usize as *const u8), 0);
    }
    #[test]
    fn canonical_abi_read_and_unsupported_statement() {
        let root = std::env::temp_dir().join(crate::meta_types::new_id("sql-abi-test"));
        let mut source = json!({});
        for table in crate::rag::TYPES {
            source[*table] = json!([]);
        }
        source["rag_generation"] =
            json!([{"record_id":"g","generation_id":"g1","alias_name":"rag","state":"ACTIVE"}]);
        crate::rag::migrate(&root, &source).unwrap();
        let request = json!({"format":"xstore-native-sql-request/v1","store":root,"sql":"SELECT generation_id FROM rag_generation WHERE alias_name = $1","params":["rag"]});
        assert_eq!(
            call(&serde_json::to_vec(&request).unwrap())["rows"],
            json!([{"generation_id":"g1"}])
        );
        let mut invalid = request;
        invalid["sql"] = json!("DELETE FROM rag_generation");
        assert_eq!(call(&serde_json::to_vec(&invalid).unwrap())["ok"], false);
        std::fs::remove_dir_all(root).unwrap();
    }
}
