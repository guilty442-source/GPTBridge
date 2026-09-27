//! gptbridge-core — the Rust core layer of the GPTBridge UI stack.
//!
//! Domain map (UI-stack refactor target):
//!   app        — Application Core: process-wide facts, diagnostics, flags
//!   state      — State Core: governed runtime-state readers (perf SLO)
//!   security   — Security: IPC capability tokens, session tickets, helpers
//!   ipc        — IPC: loopback HTTP transport and backend discovery
//!   lifecycle  — Lifecycle: managed-backend (boot_core) supervision
//!   native     — Native integration: path library, system metrics, sizes
//!
//! The crate is UI-framework-agnostic: the Tauri shell and the GPUI/egui
//! surfaces all link it so cross-surface behaviour shares one authority.

pub mod app;
pub mod ipc;
pub mod lifecycle;
pub mod native;
pub mod security;
pub mod state;
