# build.ps1 — MODEL_TRAINING native engine (C++23, MSVC, no external deps)
$ErrorActionPreference = 'Stop'
$vsvars = 'E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
# repo-relative include (portable): walk up until native/include/gptbridge_native.h is found.
$repoRoot = $root
while ($repoRoot -and -not (Test-Path -LiteralPath (Join-Path $repoRoot 'native\include\gptbridge_native.h'))) { $repoRoot = Split-Path -Parent $repoRoot }
if (-not $repoRoot) { throw 'REPO_ROOT_NOT_FOUND:native\include\gptbridge_native.h' }
$inc = Join-Path $repoRoot 'native\include'
$src = Join-Path $root 'xingcheng_trainer.cpp'
$exe = Join-Path $root 'xingcheng_trainer.exe'

# NativeCudaTrainingPlane: the fused-AdamW device path is compiled in
# whenever the kernel TU is present — it needs no CUDA toolkit at all.
# nvcuda.dll binds dynamically at run time and the in-tree PTX is JIT'd
# by the installed driver; a host without an NVIDIA driver still runs
# the identical CPU lane fail-closed (B132). Toolkit headers/libs/dlls
# are never referenced.
$backend = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $root))))
$cppSrc = Join-Path $backend 'cpp\src'
$knl = Join-Path $cppSrc 'cuda_kernels.cpp'
$useCuda = Test-Path -LiteralPath $knl
if ($useCuda) {
    cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc /DXINGCHENG_CUDA /DXINGCHENG_CUDA_KERNELS /I`"$inc`" /I`"$cppSrc`" /Fe`"$exe`" `"$src`" `"$knl`" /Fo`"$root\obj\\`""
} else {
    cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc /I`"$inc`" /Fe`"$exe`" `"$src`" /Fo`"$root\obj\\`""
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exe"

$exDir = Join-Path $root 'executor'
dotnet build $exDir -c Release --nologo | Out-Null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exDir\bin\Release\net10.0\xct-executor.exe"
