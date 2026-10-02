//! xcorpus — library surface. The bin (`main.rs`) is a thin CLI over
//! these modules; `abi` exposes the xtok/v1 C ABI consumed by the
//! C++ inference engine (B81: tokenizer is Rust-owned, reached through
//! the stable C boundary only).

pub mod abi;
pub mod corpus;
pub mod kernels;
pub mod scan;
pub mod xcb;
pub mod textutil;
pub mod tokenizer;
