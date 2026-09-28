# build.ps1 — MODEL_TRAINING native engine (C++23, MSVC, no external deps)
$ErrorActionPreference = 'Stop'
$vsvars = 'E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$inc = 'E:\GPTBridge\native\include'
$src = Join-Path $root 'xingcheng_trainer.cpp'
$exe = Join-Path $root 'xingcheng_trainer.exe'
cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc /I`"$inc`" /Fe`"$exe`" `"$src`" /Fo`"$root\obj\\`""
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exe"

$exDir = Join-Path $root 'executor'
dotnet build $exDir -c Release --nologo | Out-Null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exDir\bin\Release\net10.0\xct-executor.exe"
