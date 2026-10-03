# production-soak-battery.ps1 — governed runtime soak battery runner.
#
# Runs the ordered soak battery from the production-closure directive:
#   8h soak -> analyze -> 24h soak -> analyze -> 72h soak -> analyze.
# Each phase drives `xc-learning.exe --production-soak` against the live
# xingcheng model service (descriptor pid + /v1/status probe with the
# session token so vram/cache/error columns are populated — the analyzer
# fails evidence-incomplete without them), then `--production-soak-analyze`.
#
# Resume-safe: completed phases are recorded in battery-state.json and
# skipped on restart; a phase interrupted mid-run restarts that phase.
# Single instance via an OS file lock. Fail-closed: an aborted phase
# marks the runtime axis BLOCKED/FAIL and stops the battery.
#
# The battery waits for the model service to become reachable (the lazy
# governed activation path brings it up when the main system runs);
# it never fabricates a target.

param(
    [int]$WaitServiceMinutes = 720,
    [int]$SampleIntervalMs = 60000
)

$ErrorActionPreference = 'Stop'

$ToolRoot  = 'E:\GPTBridge\xingcheng'
$Xc        = 'E:\GPTBridge\xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe'
$IpcDir    = "$ToolRoot\xingcheng\runtime\ipc"
$LogDir    = "$ToolRoot\xingcheng\runtime\logs\production-soak"
$StateFile = "$LogDir\battery-state.json"
$LockPath  = "$LogDir\battery.lock"

$Phases = @(
    @{ name = '8h';  seconds = 28800 },
    @{ name = '24h'; seconds = 86400 },
    @{ name = '72h'; seconds = 259200 }
)

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

# Single-instance guard.
try {
    $lockStream = New-Object System.IO.FileStream(
        $LockPath, [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
}
catch {
    Write-Host "[soak-battery] another instance holds $LockPath — exit"
    exit 1
}

function Write-BatteryState($doc) {
    $doc['format'] = 'star-production-soak-battery/v1'
    $doc['updated_at'] = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $tmp = "$StateFile.tmp"
    [System.IO.File]::WriteAllText($tmp, ($doc | ConvertTo-Json -Depth 12), (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -Force $tmp $StateFile
}

function Read-BatteryState {
    if (Test-Path $StateFile) {
        try { return (Get-Content $StateFile -Raw | ConvertFrom-Json) } catch { }
    }
    return [pscustomobject]@{ phases = @(); started_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
}

# Resolve a live model-service target from the descriptor contract:
# pid alive AND /v1/status answers 2xx with the session token.
function Resolve-ServiceTarget {
    $desc = "$IpcDir\model-service.json"
    $tok  = "$IpcDir\model-service-session-token"
    if (-not (Test-Path $desc) -or -not (Test-Path $tok)) { return $null }
    try {
        $d = Get-Content $desc -Raw | ConvertFrom-Json
        $proc = Get-Process -Id ([int]$d.pid) -ErrorAction Stop
        $token = (Get-Content $tok -Raw).Trim()
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$($d.port)/v1/status" `
            -Headers @{ 'X-GPTBridge-Session-Token' = $token } `
            -TimeoutSec 10 -UseBasicParsing
        if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300) {
            return @{ pid = [int]$d.pid; port = [int]$d.port; token = $tok }
        }
    } catch { }
    return $null
}

function Invoke-Xc([string[]]$Args_) {
    $out = & $Xc --tool-root $ToolRoot @Args_ 2>&1 | Out-String
    return @{ text = $out; json = ($out | ConvertFrom-Json -ErrorAction SilentlyContinue) }
}

$state = Read-BatteryState
$completed = @{}
foreach ($p in $state.phases) { $completed[$p.name] = $p }

# Mark the runtime axis RUNNING once.
Invoke-Xc @('--production-certify','--axis','runtime','--state','RUNNING',
           '--note','soak battery started (8h/24h/72h ordered)') | Out-Null

$batteryOk = $true
foreach ($phase in $Phases) {
    if ($completed.ContainsKey($phase.name)) {
        Write-Host "[soak-battery] phase $($phase.name) already recorded — skip"
        continue
    }

    # Wait for the governed model service (lazy activation path).
    $target = $null
    $waited = 0
    while ($null -eq $target -and $waited -lt $WaitServiceMinutes * 60) {
        $target = Resolve-ServiceTarget
        if ($null -eq $target) {
            Write-BatteryState @{
                phases = @($state.phases)
                waiting_for = 'model-service'
                wait_elapsed_s = $waited
                current_phase = $phase.name
            }
            Start-Sleep -Seconds 120
            $waited += 120
        }
    }
    if ($null -eq $target) {
        Invoke-Xc @('--production-certify','--axis','runtime','--state','BLOCKED',
                   '--note',"service unreachable before $($phase.name) soak") | Out-Null
        Write-BatteryState @{
            phases = @($state.phases)
            verdict = 'BLOCKED_SERVICE_UNAVAILABLE'
            failed_phase = $phase.name
        }
        $batteryOk = $false
        break
    }

    Write-Host "[soak-battery] phase $($phase.name): pid=$($target.pid) port=$($target.port) seconds=$($phase.seconds)"
    Write-BatteryState @{
        phases = @($state.phases)
        current_phase = $phase.name
        target = @{ pid = $target.pid; port = $target.port }
    }

    $soak = Invoke-Xc @('--production-soak',
                        '--pid',"$($target.pid)",
                        '--seconds',"$($phase.seconds)",
                        '--interval-ms',"$SampleIntervalMs",
                        '--port',"$($target.port)",
                        '--token-file',$target.token)
    if ($null -eq $soak.json -or -not $soak.json.ok) {
        Invoke-Xc @('--production-certify','--axis','runtime','--state','FAIL',
                   '--note',"soak $($phase.name) run failed: $($soak.text.Substring(0,[Math]::Min(300,$soak.text.Length)))") | Out-Null
        Write-BatteryState @{ phases = @($state.phases); verdict = 'SOAK_RUN_FAILED'; failed_phase = $phase.name; detail = $soak.text }
        $batteryOk = $false
        break
    }
    $logPath = $soak.json.log
    $analysis = Invoke-Xc @('--production-soak-analyze','--file',$logPath)
    $verdict = if ($null -ne $analysis.json) { $analysis.json.verdict } else { 'ANALYZE_FAILED' }
    $analysisPath = "$LogDir\analysis-$($phase.name)-$(Get-Date -Format 'yyyyMMdd-HHmmss').json"
    [System.IO.File]::WriteAllText($analysisPath, $analysis.text, (New-Object System.Text.UTF8Encoding($false)))

    $phaseRec = [pscustomobject]@{
        name = $phase.name
        soak_log = $logPath
        analysis = $analysisPath
        verdict = $verdict
        samples = $(if ($null -ne $analysis.json) { $analysis.json.samples } else { 0 })
        completed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    $state.phases = @($state.phases) + $phaseRec
    Write-BatteryState @{ phases = @($state.phases); current_phase = $null }

    if ($verdict -ne 'BOUNDED') {
        Invoke-Xc @('--production-certify','--axis','runtime','--state','FAIL',
                   '--evidence',$analysisPath,
                   '--note',"soak $($phase.name) verdict $verdict") | Out-Null
        Write-BatteryState @{ phases = @($state.phases); verdict = $verdict; failed_phase = $phase.name }
        $batteryOk = $false
        break
    }
}

if ($batteryOk) {
    $last = $state.phases[-1]
    Invoke-Xc @('--production-certify','--axis','runtime','--state','PASS',
               '--evidence',$last.analysis,
               '--note','8h/24h/72h soak battery BOUNDED') | Out-Null
    Write-BatteryState @{ phases = @($state.phases); verdict = 'BOUNDED'; finished_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
    Write-Host '[soak-battery] battery complete: BOUNDED'
}

$lockStream.Dispose()
exit $(if ($batteryOk) { 0 } else { 1 })
