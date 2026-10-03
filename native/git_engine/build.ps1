# Build git-engine-m0.exe (C++23, MSVC, no Python) and run its self-contained tests.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File native/git_engine/build.ps1
$ErrorActionPreference = "Stop"
$vs = "E:\Program Files\Microsoft Visual Studio\18\Community"
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
$here = $PSScriptRoot
$includeDir = Join-Path (Split-Path $here -Parent) "include"
$out = Join-Path $here "bin"
$obj = Join-Path $out "obj"
New-Item -ItemType Directory -Force -Path $out | Out-Null
New-Item -ItemType Directory -Force -Path $obj | Out-Null

$exe = Join-Path $out "git-engine-m0.exe"
$bat = Join-Path $out "_build_git_engine.bat"
$srcEngine = Join-Path $here "git_engine.cpp"
$srcTests = Join-Path $here "tests_main.cpp"
$lines = @(
    "@echo off",
    "call `"$vcvars`" >nul || exit /b 1",
    "cl /nologo /std:c++latest /utf-8 /O2 /EHsc /W4 /WX- /I`"$includeDir`" /Fe`"$exe`" /Fo:$obj\ `"$srcEngine`" `"$srcTests`" /link >nul || exit /b 1"
)
Set-Content -Path $bat -Value $lines -Encoding ASCII
& cmd.exe /c "`"$bat`""
if ($LASTEXITCODE -ne 0) { Write-Output "BUILD FAILED: git-engine-m0"; exit 1 }
$hash = (Get-FileHash -Algorithm SHA256 -Path $exe).Hash.ToLowerInvariant()
Write-Output ("git-engine-m0.exe built sha256={0}" -f $hash)
& $exe
if ($LASTEXITCODE -ne 0) { Write-Output "TESTS FAILED: git-engine-m0"; exit 1 }
Write-Output "build+tests ok: $exe"
