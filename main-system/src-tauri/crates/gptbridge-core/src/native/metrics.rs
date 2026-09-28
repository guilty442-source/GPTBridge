//! metrics.rs — port of src-ui/main/systemMetrics.ts.
//!
//! CPU delta sampling + RAM + disk-free metrics for the status payload.

use std::sync::Mutex;
use std::sync::OnceLock;

use sysinfo::{Disks, System};

struct MetricsState {
    system: System,
    last_cpu_usage: Option<f32>,
}

fn metrics_state() -> &'static Mutex<MetricsState> {
    static STATE: OnceLock<Mutex<MetricsState>> = OnceLock::new();
    STATE.get_or_init(|| {
        let mut system = System::new();
        system.refresh_cpu_usage();
        Mutex::new(MetricsState {
            system,
            last_cpu_usage: None,
        })
    })
}

fn system_disk_root() -> String {
    if cfg!(windows) {
        let configured = std::env::var("SystemDrive").unwrap_or_default();
        let trimmed = configured.trim();
        if trimmed.len() == 2
            && trimmed.ends_with(':')
            && trimmed.chars().next().unwrap().is_ascii_alphabetic()
        {
            return format!("{trimmed}\\");
        }
        if !trimmed.is_empty() {
            return trimmed.to_string();
        }
        return String::from("C:\\");
    }
    String::from("/")
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

pub fn get_system_metrics() -> serde_json::Value {
    let mut state = metrics_state().lock().unwrap();

    state.system.refresh_cpu_usage();
    state.system.refresh_memory();
    let cpu = state.system.global_cpu_usage();
    let cpu_usage_percent = if cpu.is_finite() {
        Some(((cpu.max(0.0).min(100.0) * 10.0).round()) / 10.0)
    } else {
        state.last_cpu_usage
    };
    state.last_cpu_usage = cpu_usage_percent;

    let total_mem = state.system.total_memory();
    let free_mem = state.system.available_memory();
    let ram_usage_percent = if total_mem > 0 {
        ((total_mem - free_mem) as f64 / total_mem as f64 * 100.0).clamp(0.0, 100.0)
    } else {
        0.0
    };

    let disk_root = system_disk_root();
    let disks = Disks::new_with_refreshed_list();
    let disk = disks
        .iter()
        .find(|d| {
            let mount = d.mount_point().to_string_lossy().replace('/', "\\");
            disk_root.starts_with(&mount) || mount.starts_with(disk_root.trim_end_matches('\\'))
        })
        .or_else(|| disks.iter().next());
    let (disk_total, disk_free, disk_usage) = match disk {
        Some(d) => {
            let total = d.total_space();
            let free = d.available_space();
            let usage = if total > 0 {
                ((total - free) as f64 / total as f64 * 100.0).clamp(0.0, 100.0)
            } else {
                0.0
            };
            (Some(total), Some(free), Some(usage))
        }
        None => (None, None, None),
    };

    serde_json::json!({
        "cpuUsagePercent": cpu_usage_percent,
        "ramUsagePercent": ram_usage_percent,
        "ramTotalBytes": total_mem,
        "ramFreeBytes": free_mem,
        "diskUsagePercent": disk_usage,
        "diskTotalBytes": disk_total,
        "diskFreeBytes": disk_free,
        "diskRoot": disk_root,
        "sampledAt": now_ms(),
    })
}
