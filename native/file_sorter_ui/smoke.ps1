# smoke.ps1 — launch file-sorter-ui.exe with a REAL authenticated session.
# Reads the governed session token + workspace instance without printing them.
$ErrorActionPreference = "Stop"
$root = "E:\GPTBridge"

$tokenFile = Join-Path $env:LOCALAPPDATA "GPTBridge\ipc\session-token"
if (-not (Test-Path $tokenFile)) { throw "session token file missing: $tokenFile" }
$token = (Get-Content $tokenFile -Raw).Trim().ToLower()
if ($token -notmatch '^[0-9a-f]{64}$') { throw "session token malformed" }

# workspace_instance_id = sha256(root with / separators, lowercased)[..24]
$norm = ($root -replace '\\', '/').ToLowerInvariant()
$sha = [System.Security.Cryptography.SHA256]::Create()
$bytes = [Text.Encoding]::UTF8.GetBytes($norm)
$instance = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLower().Substring(0,24)

$wsUrl = "ws://127.0.0.1:8765/?token=$token&instance=$instance"
Write-Host "launching file-sorter-ui with governed session (token withheld)"
$env:GPTBRIDGE_SOURCE_UI_TOOL_ID = "file-sorter"
$env:GPTBRIDGE_SOURCE_UI_TITLE = "file-sorter (smoke)"
$env:GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL = $wsUrl
& "$root\native\file_sorter_ui\bin\file-sorter-ui.exe" --tool-window --tool-id=file-sorter
