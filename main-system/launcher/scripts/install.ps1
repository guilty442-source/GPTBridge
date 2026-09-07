param(
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"
$AppDisplayName = -join ([char[]](0x7A0B, 0x5F0F, 0x5EAB))

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
$ProjectRoot = (Resolve-Path $ProjectRoot).Path

$LauncherRoot = Join-Path $ProjectRoot "launcher"
$SourceFile = Join-Path $LauncherRoot "src\GPTBridgeLauncher.cpp"
$InstallRoot = Join-Path $env:LOCALAPPDATA "GPTBridgeLauncher"
$InstallBin = Join-Path $InstallRoot "bin"
$InstallConfig = Join-Path $InstallRoot "config"
$InstalledExe = Join-Path $InstallBin "$AppDisplayName.exe"
$Desktop = [Environment]::GetFolderPath("Desktop")
$DesktopExe = Join-Path $Desktop "$AppDisplayName.exe"
$LegacyInstalledExe = Join-Path $InstallBin "GPTBridge.exe"
$LegacyDesktopExe = Join-Path $Desktop "GPTBridge.exe"

New-Item -ItemType Directory -Force -Path $InstallBin, $InstallConfig | Out-Null

$vcvarsCandidates = @(
    "$env:ProgramFiles\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat",
    "$env:ProgramFiles\Microsoft Visual Studio\17\Community\VC\Auxiliary\Build\vcvars64.bat"
)
$vcvars = $vcvarsCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $vcvars) {
    throw "MSVC vcvars64.bat was not found."
}

$BuiltExe = Join-Path $InstallBin "GPTBridgeLauncher.build.exe"
$clCommand = "call `"$vcvars`" >nul && cl /nologo /O2 /EHsc /utf-8 /D UNICODE /D _UNICODE /Fe:`"$BuiltExe`" `"$SourceFile`" /link /SUBSYSTEM:WINDOWS user32.lib"
& cmd.exe /c $clCommand
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $BuiltExe)) {
    throw "Launcher EXE compilation failed."
}
Move-Item -LiteralPath $BuiltExe -Destination $InstalledExe -Force

Set-Content -LiteralPath (Join-Path $InstallConfig "root.txt") -Value $ProjectRoot -Encoding UTF8

foreach ($shortcut in Get-ChildItem -LiteralPath $Desktop -Filter "*.lnk" -File -ErrorAction SilentlyContinue) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($shortcut.FullName)
        $matchesProject =
            $link.TargetPath -like "$ProjectRoot*" -or
            $link.Arguments -like "*$ProjectRoot*"
        if ($matchesProject) {
            Remove-Item -LiteralPath $shortcut.FullName -Force
        }
    } catch {
    }
}

if (Test-Path -LiteralPath $DesktopExe) {
    Remove-Item -LiteralPath $DesktopExe -Force
}
New-Item -ItemType HardLink -Path $DesktopExe -Target $InstalledExe | Out-Null

foreach ($legacyExe in @($LegacyDesktopExe, $LegacyInstalledExe)) {
    if (Test-Path -LiteralPath $legacyExe) {
        Remove-Item -LiteralPath $legacyExe -Force
    }
}

& (Join-Path $LauncherRoot "scripts\start.ps1") -ProjectRoot $ProjectRoot -PrepareOnly
if ($LASTEXITCODE -ne 0) {
    throw "Launcher preparation failed."
}

Write-Output "Launcher installed."
Write-Output "Desktop EXE: $DesktopExe"
Write-Output "Installed EXE: $InstalledExe"
