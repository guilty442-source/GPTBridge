//! Native integration domain — OS-facing primitives: runtime path library,
//! system metrics, and workspace folder-size inventory.

pub mod metrics;
pub mod paths;
pub mod sizes;

pub use metrics::get_system_metrics;
pub use paths::{is_packaged, is_path_inside, path_library, runtime_env, RuntimePathLibrary};
pub use sizes::{main_system_size, platform_tool_sizes, shared_layer_size, workspace_size};
