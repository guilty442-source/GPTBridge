param(
    [string]$ProjectRoot = "",
    [switch]$PrepareOnly,
    [switch]$ForceBuild
)

$ErrorActionPreference = "Stop"
$AppDisplayName = -join ([char[]](0x5C08, 0x6848, 0x7A0B, 0x5F0F, 0x5EAB))
$EXIT_CRITICAL_FAILED = 2

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$WorkspaceRoot = (Resolve-Path (Join-Path $ProjectRoot "..")).Path

# User-scoped database settings may be created by automatic provisioning after
# the desktop session began. Load them explicitly so a reboot is not required.
foreach ($name in @(
    "GPTBRIDGE_POSTGRES_DSN",
    "GPTBRIDGE_POSTGRES_ADMIN_DSN",
    "GPTBRIDGE_MODULE_DSNS",
    "GPTBRIDGE_XINGCHENG_IDENTITY_DSN",
    "GPTBRIDGE_XINGCHENG_COGNITION_DSN"
)) {
    if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if ($value) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

$LauncherRoot = Join-Path $ProjectRoot "launcher"
$StateRoot = Join-Path $LauncherRoot "state"

New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null

function Write-LauncherStatus {
    param([string]$Message)
    $line = "[{0}] {1}" -f ([DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss.fff")), $Message
    Write-Host $line
}

function Show-LauncherError {
    param([string]$Message)
    $logRoot = Join-Path $env:LOCALAPPDATA "GPTBridgeLauncher\logs"
    $logPath = Join-Path $logRoot "launcher.log"
    try {
        New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
        $line = "[{0}] {1} ERROR: {2}" -f ([DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss.fff")), $AppDisplayName, $Message
        Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch {
        Write-Warning "Unable to write launcher log: $($_.Exception.Message)"
    }
}

function ConvertTo-ProcessArguments {
    param([string[]]$Arguments)
    return (($Arguments | ForEach-Object {
        if ($_ -match '[\s"&|<>^]') {
            '"' + ($_ -replace '(\\*)"', '$1$1\"') + '"'
        } else {
            $_
        }
    }) -join ' ')
}

function New-HiddenProcessStartInfo {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    # CreateNoWindow (CREATE_NO_WINDOW) prevents conhost from allocating a
    # console window at all.  Start-Process -WindowStyle Hidden only passes
    # SW_HIDE, which still lets a black console window flash briefly for
    # console-subsystem targets such as python.exe or .cmd shims (cmd.exe).
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

    if ($FilePath -match '\.(cmd|bat)$') {
        # CreateProcess cannot execute batch files directly; run them through
        # cmd.exe with the quoting form required by /c.
        $inner = ((ConvertTo-ProcessArguments @($FilePath)) + ' ' +
            (ConvertTo-ProcessArguments $Arguments)).Trim()
        $startInfo.FileName = Join-Path $env:SystemRoot 'System32\cmd.exe'
        $startInfo.Arguments = '/d /s /c "' + $inner + '"'
    } else {
        $startInfo.FileName = $FilePath
        $startInfo.Arguments = ConvertTo-ProcessArguments $Arguments
    }
    return $startInfo
}

function Invoke-LauncherCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $ProjectRoot
    )

    Write-LauncherStatus "Run: $FilePath $($Arguments -join ' ')"
    $startInfo = New-HiddenProcessStartInfo `
        -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $WorkingDirectory
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw "Command failed with exit code $($process.ExitCode): $FilePath"
    }
}

function Invoke-DependencyOrchestrator {
    param(
        [string]$PythonExecutable,
        [string]$OrchestratorPath,
        [string]$WorkingDirectory
    )

    # Returns the startup lifecycle state: READY, DEGRADED, FAILED, or RECOVERING.
    Write-LauncherStatus "Running local dependency orchestrator: $OrchestratorPath"
    $tempOut = Join-Path $ProjectRoot "launcher\state\orchestrator-report.json"
    $tempErr = Join-Path $ProjectRoot "launcher\state\orchestrator-report.error.log"
    Remove-Item -LiteralPath $tempOut -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tempErr -Force -ErrorAction SilentlyContinue

    $startInfo = New-HiddenProcessStartInfo `
        -FilePath $PythonExecutable `
        -Arguments @($OrchestratorPath) `
        -WorkingDirectory $WorkingDirectory
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    Set-Content -LiteralPath $tempOut -Value $stdoutTask.Result -Encoding UTF8
    Set-Content -LiteralPath $tempErr -Value $stderrTask.Result -Encoding UTF8

    $report = $null
    if (Test-Path -LiteralPath $tempOut) {
        try {
            $report = Get-Content -LiteralPath $tempOut -Raw | ConvertFrom-Json
        } catch {
            $report = $null
        }
    }

    $state = ""
    $criticalUp = $false
    if ($report -and $report.state) {
        $state = [string]$report.state
        $criticalNames = @()
        if ($report.critical_services) {
            $criticalNames = @($report.critical_services)
        } elseif ($report.postgresql) {
            # Backward compatibility with PostgreSQL-era orchestrator reports.
            $criticalNames = @("postgresql")
        }
        $criticalUp = $criticalNames.Count -gt 0
        foreach ($name in $criticalNames) {
            $criticalEntry = $report.$name
            if (-not ($criticalEntry -and [bool]$criticalEntry.ready)) {
                $criticalUp = $false
            }
        }
        Write-LauncherStatus "Dependency state: $state"
        $reportedNames = @(
            @($report.critical_services) + @($report.degradable_services) +
                @("postgresql", "qdrant", "ollama", "warm_model")
        ) | Select-Object -Unique
        foreach ($name in $reportedNames) {
            $entry = $report.$name
            if ($entry) {
                $level = if ([bool]$entry.critical) { "CRITICAL" } else { "degradable" }
                Write-LauncherStatus ("  - {0} [{1}] {2}: {3} ({4})" -f `
                    $name, $level, $entry.status, $entry.fault_code, $entry.message)
            }
        }
    } elseif ($process.ExitCode -eq $EXIT_CRITICAL_FAILED) {
        $state = "FAILED"
        $criticalUp = $false
        Write-LauncherStatus "Dependency orchestrator exited $($process.ExitCode) without readable report."
    } else {
        $state = "FAILED"
        $criticalUp = $false
        Write-LauncherStatus "Dependency orchestrator produced no readable report."
    }

    if ($criticalUp) {
        # All critical dependencies are up; launch may proceed even in a
        # DEGRADED state caused by non-critical service unavailability.
        return $state
    }

    # A critical dependency is not available. Do not pretend READY.
    $reason = ""
    if ($report) {
        $failedCritical = @()
        foreach ($name in @($report.critical_services)) {
            $entry = $report.$name
            if ($entry -and -not [bool]$entry.ready) {
                $failedCritical += (
                    "{0}: fault_code={1}; message={2}" -f `
                        $name, $entry.fault_code, $entry.message
                )
            }
        }
        if ($failedCritical.Count -gt 0) {
            $reason = $failedCritical -join " | "
        }
    }
    if (-not $reason) {
        $reason = "orchestrator exit code $($process.ExitCode)"
    }
    throw "Critical dependency unavailable; startup cannot reach READY. $reason"
}

function Ensure-NodeRuntime {
    $electronExe = Join-Path $ProjectRoot "node_modules\electron\dist\electron.exe"
    $nodeLock = Join-Path $ProjectRoot "node_modules\.package-lock.json"
    $projectLock = Join-Path $ProjectRoot "package-lock.json"
    $needsInstall =
        -not (Test-Path -LiteralPath $electronExe) -or
        -not (Test-Path -LiteralPath $nodeLock) -or
        ((Test-Path -LiteralPath $projectLock) -and
            ((Get-Item -LiteralPath $projectLock).LastWriteTimeUtc -gt
                (Get-Item -LiteralPath $nodeLock).LastWriteTimeUtc))

    if ($needsInstall) {
        $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
        Invoke-LauncherCommand $npm @("install")
    }

    if (-not (Test-Path -LiteralPath $electronExe)) {
        throw "Electron runtime is missing after npm install."
    }

    return $electronExe
}

function Ensure-PythonRuntime {
    $pythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        $bootstrap = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($bootstrap) {
            Invoke-LauncherCommand $bootstrap.Source @("-3", "-m", "venv", (Join-Path $ProjectRoot ".venv"))
        } else {
            $bootstrap = Get-Command python.exe -ErrorAction Stop
            Invoke-LauncherCommand $bootstrap.Source @("-m", "venv", (Join-Path $ProjectRoot ".venv"))
        }
    }

    $requirements = Join-Path $ProjectRoot "requirements.txt"
    $requirementsStamp = Join-Path $StateRoot "requirements.stamp"
    $needsRequirements =
        (Test-Path -LiteralPath $requirements) -and
        (-not (Test-Path -LiteralPath $requirementsStamp) -or
            ((Get-Item -LiteralPath $requirements).LastWriteTimeUtc -gt
                (Get-Item -LiteralPath $requirementsStamp).LastWriteTimeUtc))

    if ($needsRequirements) {
        Invoke-LauncherCommand $pythonExe @("-m", "pip", "install", "-r", $requirements)
        Set-Content -LiteralPath $requirementsStamp -Value ([DateTime]::UtcNow.ToString("O")) -Encoding ASCII
    }

    if (-not (Test-Path -LiteralPath $pythonExe)) {
        throw "Python runtime is missing."
    }

    return $pythonExe
}

function Get-GovernanceSourceSignature {
    # Signature of the enforced governance authority sources. A stable signature
    # lets us reuse a previously-passing audit for repeat launches instead of
    # spinning up a fresh Python process that re-hashes and re-verifies the same
    # (already O/S read-only, sealed) governance files every time.
    param([string]$GovernanceWorkspaceRoot)
    $governanceRelative = @(
        "governance_rule\governance_policy.py",
        "governance_rule\code_rule_directory.py",
        "governance_rule\permission_directory\directory_authority.py",
        "governance_rule\execution\authentication\__init__.py",
        "governance_rule\execution\integrity\__init__.py",
        "governance_rule\execution\versioning\__init__.py",
        "governance_rule\permission_directory\execution\identity_registry\__init__.py",
        "governance_rule\permission_directory\execution\path_guard\__init__.py",
        "main-system\src-ui\main\governance-bootstrap.ts",
        "main-system\src-core\core_system\governance_runtime.py",
        "governance_rule\permission_directory\registries\permissions\identity_groups.py",
        "governance_rule\permission_directory\registries\permissions\identity_permissions.py",
        "governance_rule\permission_directory\registries\permissions\capability_boundaries.py",
        "governance_rule\permission_directory\registries\permissions\tool_routes.py",
        "governance_rule\permission_directory\registries\permissions\source_ownership.py"
    )
    $lines = foreach ($relative in $governanceRelative) {
        $candidate = Join-Path $GovernanceWorkspaceRoot $relative
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $digest = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash
        "$relative`0$digest"
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "")
    } finally {
        $sha.Dispose()
    }
}

function Write-StartupJournal {
    param(
        [string]$Event,
        [hashtable]$Payload = @{}
    )
    try {
        $journalPath = Join-Path $StateRoot "startup-journal.jsonl"
        $record = @{
            event = $Event
            timestamp = [DateTime]::UtcNow.ToString("O")
        } + $Payload
        Add-Content -LiteralPath $journalPath -Value (ConvertTo-Json -InputObject $record -Compress) -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch {
        # Observability only; never fail the launcher for a journal write.
    }
}

function Invoke-DefaultGovernanceAuthority {
    param(
        [string]$PythonExecutable,
        [string]$GovernanceWorkspaceRoot
    )

    # Hybrid/cached gate: only run the full governance audit when the enforced
    # authority sources changed or no prior pass is cached. Repeat unchanged
    # launches reuse the cached PASS, avoiding a redundant Python subprocess.
    $signaturePath = Join-Path $StateRoot "governance-audit.sha256"
    $currentSignature = Get-GovernanceSourceSignature -GovernanceWorkspaceRoot $GovernanceWorkspaceRoot
    $recordedSignature = if (Test-Path -LiteralPath $signaturePath) {
        (Get-Content -LiteralPath $signaturePath -Raw).Trim()
    } else {
        ""
    }

    if ($recordedSignature -eq $currentSignature) {
        Write-LauncherStatus "Governance authority verified (cached, sources unchanged)."
        Write-StartupJournal -Event "launcher.governance.cache-hit" @{ signature = $currentSignature }
        return
    }

    Write-LauncherStatus "Loading default governance authority before main system."
    Write-StartupJournal -Event "launcher.governance.start" @{}
    Invoke-LauncherCommand `
        $PythonExecutable `
        @("-m", "governance_rule.execution.audit") `
        $GovernanceWorkspaceRoot
    Set-Content -LiteralPath $signaturePath -Value $currentSignature -Encoding ASCII
    Write-StartupJournal -Event "launcher.governance.pass" @{ signature = $currentSignature }
    Write-LauncherStatus "Default governance authority verified and active."
}

function Get-LatestSourceWriteTime {
    $latest = [DateTime]::MinValue
    $sourceRoots = @(
        (Join-Path $ProjectRoot "src-ui"),
        (Join-Path $ProjectRoot "package.json"),
        (Join-Path $ProjectRoot "package-lock.json"),
        (Join-Path $ProjectRoot "vite.config.ts"),
        (Join-Path $ProjectRoot "vite.main.config.ts"),
        (Join-Path $ProjectRoot "tsconfig.json"),
        (Join-Path $ProjectRoot "tsconfig.main.json")
    )

    foreach ($sourceRoot in $sourceRoots) {
        if (-not (Test-Path -LiteralPath $sourceRoot)) {
            continue
        }

        $item = Get-Item -LiteralPath $sourceRoot
        if ($item.PSIsContainer) {
            foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -Recurse -File) {
                if ($file.LastWriteTimeUtc -gt $latest) {
                    $latest = $file.LastWriteTimeUtc
                }
            }
        } elseif ($item.LastWriteTimeUtc -gt $latest) {
            $latest = $item.LastWriteTimeUtc
        }
    }

    return $latest
}

function Get-ProductionSourceSignature {
    $sourceRoots = @(
        (Join-Path $ProjectRoot "src-ui"),
        (Join-Path $ProjectRoot "src-core"),
        (Join-Path $ProjectRoot "config\tool-runtime-contract.json"),
        (Join-Path $ProjectRoot "package.json"),
        (Join-Path $ProjectRoot "package-lock.json"),
        (Join-Path $ProjectRoot "requirements.txt"),
        (Join-Path $ProjectRoot "vite.config.ts"),
        (Join-Path $ProjectRoot "vite.main.config.ts"),
        (Join-Path $ProjectRoot "tsconfig.json"),
        (Join-Path $ProjectRoot "tsconfig.main.json")
    )
    $files = foreach ($sourceRoot in $sourceRoots) {
        if (-not (Test-Path -LiteralPath $sourceRoot)) {
            continue
        }
        $item = Get-Item -LiteralPath $sourceRoot
        if ($item.PSIsContainer) {
            Get-ChildItem -LiteralPath $sourceRoot -Recurse -File |
                Where-Object {
                    $_.FullName -notmatch '\\(?:__pycache__|runtime|dist-ui|build)\\'
                }
        } else {
            $item
        }
    }
    $lines = foreach ($file in ($files | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($ProjectRoot.Length).TrimStart('\')
        $digest = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        "$relative`0$digest"
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "")
    } finally {
        $sha.Dispose()
    }
}

function Ensure-ProductionBuild {
    $mainOutput = Join-Path $ProjectRoot "dist-ui\main\index.js"
    $rendererOutput = Join-Path $ProjectRoot "dist-ui\renderer\index.html"
    $buildSignaturePath = Join-Path $StateRoot "production-build.sha256"
    $currentSignature = Get-ProductionSourceSignature
    $recordedSignature = if (Test-Path -LiteralPath $buildSignaturePath) {
        (Get-Content -LiteralPath $buildSignaturePath -Raw).Trim()
    } else {
        ""
    }
    $outputsReady =
        (Test-Path -LiteralPath $mainOutput) -and
        (Test-Path -LiteralPath $rendererOutput)

    $needsBuild =
        $ForceBuild -or
        -not $outputsReady -or
        $recordedSignature -ne $currentSignature
    if (-not $needsBuild) {
        $latestSource = Get-LatestSourceWriteTime
        $oldestOutput = @(
            (Get-Item -LiteralPath $mainOutput).LastWriteTimeUtc,
            (Get-Item -LiteralPath $rendererOutput).LastWriteTimeUtc
        ) | Sort-Object | Select-Object -First 1
        $needsBuild = $latestSource -gt $oldestOutput
    }

    if (-not $needsBuild) {
        Write-LauncherStatus "Production build is current."
        return
    }

    Write-LauncherStatus "Production sources changed; running incremental build."
    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    try {
        Invoke-LauncherCommand $npm @("run", "build:app")
        Set-Content -LiteralPath $buildSignaturePath -Value $currentSignature -Encoding ASCII
    } catch {
        if ($outputsReady -and $recordedSignature -eq $currentSignature) {
            Write-LauncherStatus "Build failed; launching the last compatible production generation. $($_.Exception.Message)"
            return
        }
        throw "Production build failed and the previous generation is not source-compatible. Refusing a mixed-version launch. $($_.Exception.Message)"
    }
}

$mutex = New-Object System.Threading.Mutex($false, "Local\GPTBridgeLauncher")
$mutexAcquired = $false

try {
    $mutexAcquired = $mutex.WaitOne(0)
    if (-not $mutexAcquired) {
        Write-LauncherStatus "Another launcher preparation is already running."
        exit 0
    }

    Write-LauncherStatus "Launcher start. ProjectRoot=$ProjectRoot PrepareOnly=$PrepareOnly ForceBuild=$ForceBuild"
    $launchStartedAt = [DateTime]::UtcNow
    # Expose the launcher state root to the dependency orchestrator so its
    # per-phase startup journal is co-located with the launcher's own journal.
    $env:GPTBRIDGE_STATE_ROOT = $ProjectRoot
    Write-StartupJournal -Event "launcher.start" @{ projectRoot = $ProjectRoot }

    $pythonExe = Ensure-PythonRuntime
    Write-LauncherStatus "Starting local PostgreSQL, Qdrant and Ollama dependencies (hybrid parallel)."
    Write-StartupJournal -Event "launcher.phase.dependencies.start" @{}
    $startupState = Invoke-DependencyOrchestrator `
        -PythonExecutable $pythonExe `
        -OrchestratorPath (Join-Path $WorkspaceRoot "local-model\scripts\startup_orchestrator.py") `
        -WorkingDirectory $WorkspaceRoot
    Write-StartupJournal -Event "launcher.phase.dependencies.done" @{ state = $startupState }
    Write-StartupJournal -Event "launcher.phase.governance.start" @{}
    Invoke-DefaultGovernanceAuthority $pythonExe $WorkspaceRoot
    Write-StartupJournal -Event "launcher.phase.governance.done" @{}
    $electronExe = Ensure-NodeRuntime
    Ensure-ProductionBuild
    Write-StartupJournal -Event "launcher.phase.prepare.done" @{
        total_ms = [int](([DateTime]::UtcNow - $launchStartedAt).TotalMilliseconds)
    }

    if ($PrepareOnly) {
        Write-LauncherStatus "Preparation complete (dependency state: $startupState)."
        exit 0
    }

    $mainEntry = Join-Path $ProjectRoot "dist-ui\main\index.js"
    if (-not (Test-Path -LiteralPath $mainEntry)) {
        throw "Production main entry is missing: $mainEntry"
    }

    Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    $env:GPTBRIDGE_SOURCE_PRODUCTION = "1"
    $env:GPTBRIDGE_MANAGE_BACKEND = "1"
    $env:GPTBRIDGE_WORKSPACE_ROOT = $WorkspaceRoot
    $env:GPTBRIDGE_PROJECT_ROOT = $WorkspaceRoot
    $env:GPTBRIDGE_STARTUP_STATE = $startupState
    $env:NODE_ENV = "production"

    Write-LauncherStatus "Launching source-production Electron runtime."
    Write-StartupJournal -Event "launcher.phase.electron.start" @{}
    $electronStartInfo = New-HiddenProcessStartInfo `
        -FilePath $electronExe `
        -Arguments @($mainEntry) `
        -WorkingDirectory $ProjectRoot
    $electronProcess = [System.Diagnostics.Process]::Start($electronStartInfo)
    Start-Sleep -Milliseconds 800

    if ($electronProcess.HasExited) {
        if ($electronProcess.ExitCode -ne 0) {
            Write-StartupJournal -Event "launcher.electron.exited" @{ code = $electronProcess.ExitCode }
            throw "Electron exited during startup with code $($electronProcess.ExitCode)."
        }
        Write-LauncherStatus "Electron handed off to the running instance."
        Write-StartupJournal -Event "launcher.electron.handoff" @{}
    } else {
        Write-LauncherStatus "Electron startup accepted. PID=$($electronProcess.Id)"
        Write-StartupJournal -Event "launcher.electron.accepted" @{ pid = $electronProcess.Id }
    }
} catch {
    Write-StartupJournal -Event "launcher.failed" @{ message = $_.Exception.Message }
    $message = "$AppDisplayName launch failed: $($_.Exception.Message)"
    Write-LauncherStatus $message
    Show-LauncherError $message
    exit 1
} finally {
    if ($mutexAcquired) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
