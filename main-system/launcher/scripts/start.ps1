param(
    [string]$ProjectRoot = "",
    [switch]$PrepareOnly,
    [switch]$ForceBuild
)

$ErrorActionPreference = "Stop"
$AppDisplayName = -join ([char[]](0x7A0B, 0x5F0F, 0x5EAB))

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
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            $Message,
            $AppDisplayName,
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    } catch {
        Write-Warning "Unable to show error dialog: $($_.Exception.Message)"
    }
}

function Invoke-LauncherCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $ProjectRoot
    )

    Write-LauncherStatus "Run: $FilePath $($Arguments -join ' ')"
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Command failed with exit code $($process.ExitCode): $FilePath"
    }
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

function Invoke-DefaultGovernanceAuthority {
    param(
        [string]$PythonExecutable,
        [string]$GovernanceWorkspaceRoot
    )

    Write-LauncherStatus "Loading default governance authority before main system."
    Invoke-LauncherCommand `
        $PythonExecutable `
        @("-m", "governance_rule.execution.audit") `
        $GovernanceWorkspaceRoot
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

    $pythonExe = Ensure-PythonRuntime
    Write-LauncherStatus "Starting local PostgreSQL, Qdrant and Ollama dependencies."
    Invoke-LauncherCommand `
        $pythonExe `
        @((Join-Path $WorkspaceRoot "local-model\scripts\startup_orchestrator.py")) `
        $WorkspaceRoot
    Invoke-DefaultGovernanceAuthority $pythonExe $WorkspaceRoot
    $electronExe = Ensure-NodeRuntime
    Ensure-ProductionBuild

    if ($PrepareOnly) {
        Write-LauncherStatus "Preparation complete."
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
    $env:NODE_ENV = "production"

    Write-LauncherStatus "Launching source-production Electron runtime."
    $electronProcess = Start-Process `
        -FilePath $electronExe `
        -ArgumentList @($mainEntry) `
        -WorkingDirectory $ProjectRoot `
        -PassThru
    Start-Sleep -Milliseconds 800

    if ($electronProcess.HasExited) {
        if ($electronProcess.ExitCode -ne 0) {
            throw "Electron exited during startup with code $($electronProcess.ExitCode)."
        }
        Write-LauncherStatus "Electron handed off to the running instance."
    } else {
        Write-LauncherStatus "Electron startup accepted. PID=$($electronProcess.Id)"
    }
} catch {
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
