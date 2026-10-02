//! Governed identity resolution (sealed-registry parity).

use std::path::{Path, PathBuf};

use serde_json::Value;

use super::{tool_dir, workspace_root};

/// AI-channel participants eligible to own a nested runtime identity —
/// mirrors ``AUTHORIZED_TOOL_IDS`` in the sealed tool_routes registry.
const CHANNEL_PARTICIPANT_IDS: [&str; 4] =
    ["ai-assistant", "xingcheng", "ai-collaboration", "star-chat"];

/// Sealed identity manifests that may live inside another tool's root —
/// ``identity_group_snapshot`` manifest bindings, restricted to channel
/// participants.  (bound_tool_id, manifest path relative to workspace root)
const BOUND_IDENTITY_MANIFESTS: [(&str, &str); 4] = [
    ("ai-assistant", "Standalone tools/ai-assistant/manifest.json"),
    ("xingcheng", "xingcheng/xingcheng/manifest.json"),
    (
        "ai-collaboration",
        "Standalone tools/ai-collaboration/manifest.json",
    ),
    (
        "star-chat",
        "Standalone tools/model-dialogue/star-chat/manifest.json",
    ),
];

/// ``runtime_owner_tool_id`` — declared runtime owner, validated against
/// the manifest's own host/permission-owner/companion declarations.
pub(super) fn runtime_owner_tool_id(
    tool_id: &str,
    manifest: &Value,
) -> Result<String, String> {
    let owner = manifest["runtime_owner_tool_id"]
        .as_str()
        .unwrap_or_default()
        .trim()
        .to_string();
    if owner.is_empty() || owner == tool_id {
        return Ok(tool_id.to_string());
    }
    let mut declared: Vec<String> = vec![
        manifest["host_tool_id"].as_str().unwrap_or_default().to_string(),
        manifest["shared_permission_owner"]
            .as_str()
            .unwrap_or_default()
            .to_string(),
    ];
    if let Some(companions) = manifest["companion_tools"].as_array() {
        for c in companions {
            if let Some(id) = c["id"].as_str() {
                declared.push(id.to_string());
            }
        }
    }
    if !declared.iter().any(|d| d == &owner) {
        return Err("PERMISSION_DENIED".into());
    }
    if !tool_dir(&owner).join("manifest.json").is_file() {
        return Err("PERMISSION_DENIED".into());
    }
    Ok(owner)
}

/// ``_governed_runtime_tool_id`` — the sealed-registry identity a tool's
/// runtime authenticates as.  A nested channel participant whose bound
/// manifest lives directly inside the tool root claims the runtime
/// (model-dialogue → star-chat); deeper nestings belong to their own root.
pub(super) fn governed_runtime_tool_id(
    tool_id: &str,
    tool_root: &Path,
) -> Result<String, String> {
    let mut candidates: Vec<&str> = Vec::new();
    for (bound_id, manifest_rel) in BOUND_IDENTITY_MANIFESTS {
        if bound_id == tool_id || !CHANNEL_PARTICIPANT_IDS.contains(&bound_id) {
            continue;
        }
        let bound_manifest = workspace_root().join(manifest_rel);
        let Ok(relative) = bound_manifest
            .parent()
            .unwrap_or(tool_root)
            .strip_prefix(tool_root)
        else {
            // A bound manifest outside the tools tree (the xingcheng
            // enclave at the workspace root) still binds to the tool
            // its manifest declares as lifecycle owner
            // ("<tool_id>/<lane>").
            if bound_manifest.is_file()
                && std::fs::read_to_string(&bound_manifest)
                    .ok()
                    .and_then(|raw| {
                        serde_json::from_str::<Value>(&raw).ok()
                    })
                    .and_then(|m| {
                        m["runtime"]["lifecycle_owner"]
                            .as_str()
                            .map(|s| s.to_string())
                    })
                    .map(|owner| {
                        owner == tool_id
                            || owner
                                .starts_with(&format!("{tool_id}/"))
                    })
                    .unwrap_or(false)
            {
                candidates.push(bound_id);
            }
            continue;
        };
        let parts: Vec<_> = relative.components().collect();
        // A binding nested under a deeper tool root belongs to that
        // sub-tool — only direct participants count here.
        let deeper_tool = (1..parts.len()).any(|depth| {
            let prefix: PathBuf = parts[..depth].iter().collect();
            tool_root.join(&prefix).join("manifest.json").is_file()
        });
        if deeper_tool {
            continue;
        }
        if bound_manifest.is_file() {
            candidates.push(bound_id);
        }
    }
    if candidates.len() > 1 {
        return Err("PERMISSION_DENIED".into());
    }
    Ok(candidates
        .first()
        .map(|s| s.to_string())
        .unwrap_or_else(|| tool_id.to_string()))
}
