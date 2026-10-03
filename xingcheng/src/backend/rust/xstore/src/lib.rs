//! Shared native metadata engine. Consumers replay canonical events, never indexes as authority.
#![recursion_limit = "512"]
pub mod codex;
mod hash;
mod meta_domain;
mod meta_index;
mod meta_lease;
mod meta_log;
mod meta_release;
mod meta_snap;
mod meta_state;
mod meta_tx;
mod meta_types;
pub mod rag;
pub mod sql;
mod sql_ffi;
