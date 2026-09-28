# Build resource-governor.exe (C++23, MSVC, no Python).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File native/resource_governor/build.ps1
$ErrorActionPreference = "Stop"
$vs = "E:\Program Files\Microsoft Visual Studio\18\Community"
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
$here = $PSScriptRoot
$includeDir = Join-Path (Split-Path $here -Parent) "include"
$out = Join-Path $here "bin"
$obj = Join-Path $out "obj"
New-Item -ItemType Directory -Force -Path $out | Out-Null
New-Item -ItemType Directory -Force -Path $obj | Out-Null

$exe = Join-Path $out "resource-governor.exe"
$bat = Join-Path $out "_build_governor.bat"
# A185 split: all implementation units (control law + Win32 engine + host + entry).
$srcFiles = Get-ChildItem -Path $here -Filter "*.cpp" -File | Sort-Object Name
$quoted = @($srcFiles | ForEach-Object { '"' + $_.FullName + '"' })
$sources = $quoted -join " "
$lines = @(
    "@echo off",
    "call `"$vcvars`" >nul || exit /b 1",
    "cl /nologo /std:c++latest /utf-8 /O2 /GL /EHsc /W4 /WX- /I`"$includeDir`" /Fe`"$exe`" /Fo:$obj\ $sources /link /LTCG >nul || exit /b 1"
)
Set-Content -Path $bat -Value $lines -Encoding ASCII
& cmd.exe /c "`"$bat`""
if ($LASTEXITCODE -ne 0) { Write-Output "BUILD FAILED: resource-governor"; exit 1 }
$hash = (Get-FileHash -Algorithm SHA256 -Path $exe).Hash.ToLowerInvariant()
Write-Output ("resource-governor.exe built sha256={0}" -f $hash)
Write-Output "build ok: $exe"
