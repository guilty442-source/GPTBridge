# build.ps1 — build the file-sorter native C++ tool window.
#
# Produces bin\file-sorter-ui.exe.  Requires MSVC (vcvars64); the
# script locates the newest Visual Studio under Program Files.
[CmdletBinding()]
param([string]$Configuration = "release")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src  = Join-Path $root "src"
$bin  = Join-Path $root "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null

# --- locate vcvars64 -------------------------------------------------
$vcvars = $null
$vsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (Test-Path $vsWhere) {
    $vsPath = & $vsWhere -latest -property installationPath
    if ($vsPath) {
        $candidate = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat"
        if (Test-Path $candidate) { $vcvars = $candidate }
    }
}
if (-not $vcvars) {
    foreach ($base in "${env:ProgramFiles}", "${env:ProgramFiles(x86)}") {
        foreach ($year in "18", "2026", "2022") {
            $candidate = Join-Path $base "Microsoft Visual Studio\$year\Community\VC\Auxiliary\Build\vcvars64.bat"
            if (Test-Path $candidate) { $vcvars = $candidate; break }
        }
        if ($vcvars) { break }
    }
}
if (-not $vcvars) { throw "vcvars64.bat not found" }

$opt = if ($Configuration -eq "debug") { "/Od /Zi /MDd" } else { "/O2 /MD" }
$flags = @(
    "/std:c++17", "/utf-8", "/EHsc", "/W3", "/nologo",
    "/I", (Join-Path $root "src"),
    "/I", (Join-Path $root "..\include"),
    "/D", "UNICODE", "/D", "_UNICODE", "/D", "WIN32_LEAN_AND_MEAN",
    "/D", "_WINSOCK_DEPRECATED_NO_WARNINGS",
    "/Fe:" + (Join-Path $bin "file-sorter-ui.exe"),
    "/Fo:" + (Join-Path $bin "obj\") + ""
)
New-Item -ItemType Directory -Force -Path (Join-Path $bin "obj") | Out-Null
$sources = Get-ChildItem (Join-Path $src "*.cpp") | ForEach-Object { $_.FullName }
$libs = "user32.lib gdi32.lib comctl32.lib shell32.lib ws2_32.lib uxtheme.lib msimg32.lib dwmapi.lib ole32.lib"

$cmd = "`"$vcvars`" >nul && cl $opt $flags $sources /link /SUBSYSTEM:WINDOWS $libs"
Write-Host "[file-sorter-ui] cl $opt ..."
& cmd /c $cmd
if ($LASTEXITCODE -ne 0) { throw "cl exited $LASTEXITCODE" }
Write-Host "[file-sorter-ui] built: $(Join-Path $bin 'file-sorter-ui.exe')"
