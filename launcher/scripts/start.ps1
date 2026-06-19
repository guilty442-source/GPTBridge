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

$LauncherRoot = Join-Path $ProjectRoot "launcher"
$LogsRoot = Join-Path $LauncherRoot "logs"
$StateRoot = Join-Path $LauncherRoot "state"
$LogPath = Join-Path $LogsRoot "launcher.log"

New-Item -ItemType Directory -Force -Path $LogsRoot, $StateRoot | Out-Null

function Write-LauncherLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f ([DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss.fff")), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
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
        Write-LauncherLog "Unable to show error dialog: $($_.Exception.Message)"
    }
}

function Invoke-LoggedCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-LauncherLog "Run: $FilePath $($Arguments -join ' ')"
    $stdoutPath = Join-Path $StateRoot "command.stdout.tmp"
    $stderrPath = Join-Path $StateRoot "command.stderr.tmp"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

    try {
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList $Arguments `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -Wait `
            -PassThru

        foreach ($outputPath in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $outputPath) {
                $content = Get-Content -LiteralPath $outputPath -Raw -ErrorAction SilentlyContinue
                if ($content) {
                    Add-Content -LiteralPath $LogPath -Value $content -Encoding UTF8
                }
            }
        }

        if ($process.ExitCode -ne 0) {
            throw "Command failed with exit code $($process.ExitCode): $FilePath"
        }
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
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
        Invoke-LoggedCommand $npm @("install")
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
            Invoke-LoggedCommand $bootstrap.Source @("-3", "-m", "venv", (Join-Path $ProjectRoot ".venv"))
        } else {
            $bootstrap = Get-Command python.exe -ErrorAction Stop
            Invoke-LoggedCommand $bootstrap.Source @("-m", "venv", (Join-Path $ProjectRoot ".venv"))
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
        Invoke-LoggedCommand $pythonExe @("-m", "pip", "install", "-r", $requirements)
        Set-Content -LiteralPath $requirementsStamp -Value ([DateTime]::UtcNow.ToString("O")) -Encoding ASCII
    }

    if (-not (Test-Path -LiteralPath $pythonExe)) {
        throw "Python runtime is missing."
    }

    return $pythonExe
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

function Ensure-ProductionBuild {
    $mainOutput = Join-Path $ProjectRoot "dist-ui\main\index.js"
    $rendererOutput = Join-Path $ProjectRoot "dist-ui\renderer\index.html"
    $outputsReady =
        (Test-Path -LiteralPath $mainOutput) -and
        (Test-Path -LiteralPath $rendererOutput)

    $needsBuild = $ForceBuild -or -not $outputsReady
    if (-not $needsBuild) {
        $latestSource = Get-LatestSourceWriteTime
        $oldestOutput = @(
            (Get-Item -LiteralPath $mainOutput).LastWriteTimeUtc,
            (Get-Item -LiteralPath $rendererOutput).LastWriteTimeUtc
        ) | Sort-Object | Select-Object -First 1
        $needsBuild = $latestSource -gt $oldestOutput
    }

    if (-not $needsBuild) {
        Write-LauncherLog "Production build is current."
        return
    }

    Write-LauncherLog "Production sources changed; running incremental build."
    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    try {
        Invoke-LoggedCommand $npm @("run", "build:app")
    } catch {
        if ($outputsReady) {
            Write-LauncherLog "Build failed; launching last known good production build. $($_.Exception.Message)"
            return
        }
        throw
    }
}

$mutex = New-Object System.Threading.Mutex($false, "Local\GPTBridgeLauncher")
$mutexAcquired = $false

try {
    $mutexAcquired = $mutex.WaitOne(0)
    if (-not $mutexAcquired) {
        Write-LauncherLog "Another launcher preparation is already running."
        exit 0
    }

    Write-LauncherLog "Launcher start. ProjectRoot=$ProjectRoot PrepareOnly=$PrepareOnly ForceBuild=$ForceBuild"

    $electronExe = Ensure-NodeRuntime
    $pythonExe = Ensure-PythonRuntime
    Ensure-ProductionBuild

    if ($PrepareOnly) {
        Write-LauncherLog "Preparation complete."
        exit 0
    }

    $mainEntry = Join-Path $ProjectRoot "dist-ui\main\index.js"
    if (-not (Test-Path -LiteralPath $mainEntry)) {
        throw "Production main entry is missing: $mainEntry"
    }

    Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    $env:GPTBRIDGE_SOURCE_PRODUCTION = "1"
    $env:GPTBRIDGE_MANAGE_BACKEND = "1"
    $env:GPTBRIDGE_WORKSPACE_ROOT = $ProjectRoot
    $env:GPTBRIDGE_PROJECT_ROOT = $ProjectRoot
    $env:NODE_ENV = "production"

    Write-LauncherLog "Launching source-production Electron runtime."
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
        Write-LauncherLog "Electron handed off to the running instance."
    } else {
        Write-LauncherLog "Electron startup accepted. PID=$($electronProcess.Id)"
    }
} catch {
    $message = "$AppDisplayName launch failed: $($_.Exception.Message)"
    Write-LauncherLog $message
    Show-LauncherError $message
    exit 1
} finally {
    if ($mutexAcquired) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
