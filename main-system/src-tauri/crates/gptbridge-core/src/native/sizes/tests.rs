//! Contract port of `main-system/scripts/test_platform_tool_size_breakdown.ts`:
//! the platform-tool size contract moved with the implementation into this
//! crate.  The inventory fixture carries `main_system_independent_tool`
//! because `declares_independent_tool_card` is part of the live contract.

use std::fs;
use std::path::Path;

use super::api::platform_tool_sizes;
use super::measure::classify_tool_file;

#[test]
fn classify_tool_file_assigns_categories() {
    let cases: [(&str, &str); 22] = [
        ("src/main.rs", "program"),
        ("tests/test_main.rs", "program"),
        ("manifest.json", "program"),
        ("dist/tool.exe", "runtime"),
        ("build/app.js", "runtime"),
        ("env/tool.exe", "runtime"),
        ("node_modules/pkg/index.js", "runtime"),
        ("runtime/ipc/channel.json", "runtime"),
        ("data/default.sqlite3", "user_data"),
        ("runtime/state/model.sqlite3", "user_data"),
        ("runtime/settings/preferences.json", "user_data"),
        ("runtime/recovery/journal.json", "user_data"),
        ("runtime/test-self-training/examples.sqlite3", "user_data"),
        (".cache/main.bin", "cache"),
        ("runtime/browser-profiles/Cache/data", "cache"),
        ("runtime/edge-profile/Default/data", "cache"),
        ("runtime/electron-user-data/GPU Cache/data", "cache"),
        ("runtime/temp/file.tmp", "cache"),
        ("data/business/backups/archive.zip", "backups"),
        ("runtime/data/investment_backups/archive.ivault", "backups"),
        ("runtime/cache/database.bak", "backups"),
        ("RUNTIME\\STATE\\MODEL.SQLITE3", "user_data"),
    ];
    for (relative, expected) in cases {
        assert_eq!(classify_tool_file(relative), expected, "{relative}");
    }
}

fn write_fixture_file(tool_root: &Path, relative: &str, contents: &str) -> u64 {
    let target = tool_root.join(relative.replace('/', "\\"));
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    fs::write(&target, contents).unwrap();
    contents.len() as u64
}

#[test]
fn platform_tool_sizes_breakdown_and_cache() {
    let workspace_root =
        std::env::temp_dir().join(format!("gptbridge-size-breakdown-{}", std::process::id()));
    let _ = fs::remove_dir_all(&workspace_root);
    let tool_root = workspace_root.join("sample-tool");

    let manifest = serde_json::json!({
        "id": "sample-tool",
        "main_system_independent_tool": true,
        "runtime": { "entry": "src/main.py" },
    })
    .to_string();
    let mut expected_bytes = [("program", manifest.len() as u64)]
        .into_iter()
        .collect::<std::collections::HashMap<&str, u64>>();
    for key in ["runtime", "user_data", "cache", "backups"] {
        expected_bytes.insert(key, 0);
    }
    let mut expected_files = [
        ("program", 1u64),
        ("runtime", 0),
        ("user_data", 0),
        ("cache", 0),
        ("backups", 0),
    ]
    .into_iter()
    .collect::<std::collections::HashMap<&str, u64>>();
    for (key, relative, contents) in [
        ("program", "src/main.py", "program"),
        ("runtime", "dist/tool.exe", "runtime"),
        ("user_data", "runtime/state/model.sqlite3", "user-data"),
        ("cache", "runtime/browser-profiles/Cache/data", "cache"),
        ("backups", "data/business/backups/archive.zip", "backup"),
    ] {
        *expected_bytes.get_mut(key).unwrap() +=
            write_fixture_file(&tool_root, relative, contents);
        *expected_files.get_mut(key).unwrap() += 1;
    }
    fs::write(tool_root.join("manifest.json"), &manifest).unwrap();

    let tools = platform_tool_sizes(&workspace_root, true);
    let list = tools.as_array().unwrap();
    assert_eq!(list.len(), 1);
    let tool = &list[0];
    let breakdown = &tool["size_breakdown"];
    for (category, size_bytes) in &expected_bytes {
        assert_eq!(
            breakdown[category]["size_bytes"].as_u64().unwrap(),
            *size_bytes,
            "{category} size_bytes"
        );
        assert_eq!(
            breakdown[category]["file_count"].as_u64().unwrap(),
            expected_files[category],
            "{category} file_count"
        );
    }
    let breakdown_bytes: u64 = breakdown
        .as_object()
        .unwrap()
        .values()
        .map(|category| category["size_bytes"].as_u64().unwrap())
        .sum();
    let breakdown_files: u64 = breakdown
        .as_object()
        .unwrap()
        .values()
        .map(|category| category["file_count"].as_u64().unwrap())
        .sum();
    assert_eq!(
        breakdown_bytes,
        tool["project_size_bytes"].as_u64().unwrap()
    );
    assert_eq!(breakdown_files, tool["file_count"].as_u64().unwrap());

    let added_bytes = write_fixture_file(&tool_root, "runtime/state/new.sqlite3", "new-state");
    let cached = platform_tool_sizes(&workspace_root, false);
    assert_eq!(
        cached[0]["project_size_bytes"].as_u64().unwrap(),
        tool["project_size_bytes"].as_u64().unwrap()
    );
    let refreshed = platform_tool_sizes(&workspace_root, true);
    assert_eq!(
        refreshed[0]["project_size_bytes"].as_u64().unwrap(),
        tool["project_size_bytes"].as_u64().unwrap() + added_bytes
    );
    assert_eq!(
        refreshed[0]["size_breakdown"]["user_data"]["size_bytes"]
            .as_u64()
            .unwrap(),
        breakdown["user_data"]["size_bytes"].as_u64().unwrap() + added_bytes
    );

    let _ = fs::remove_dir_all(&workspace_root);
}
