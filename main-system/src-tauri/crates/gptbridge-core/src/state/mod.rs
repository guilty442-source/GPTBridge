//! State Core — governed runtime-state readers.
//!
//! Read-side views over the runtime state store (``runtime/state/*.json``)
//! shared by every UI surface.  Fail-closed: unmeasurable fields surface as
//! null/``available:false``, never fabricated.

pub mod perf_slo;

pub use perf_slo::{evaluate_baseline, get_perf_slo};
