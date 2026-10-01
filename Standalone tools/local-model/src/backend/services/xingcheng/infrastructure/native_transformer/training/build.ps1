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

# NativeCudaTrainingPlane (optional): the trainer links the native kernel
# TU so the fused-AdamW device path is available behind
# XINGCHENG_TRAINER_CUDA_OPT. CUDA driver/NVRTC bind dynamically at
# runtime — a host without NVIDIA still runs identically. With the
# toolkit, cudart is linked statically; without it,
# XINGCHENG_CUDA_DYNRT resolves the runtime surface from nvcuda.dll, so
# the lane compiles toolkit-free and still fails closed with no driver.
$backend = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $root))))
$cppSrc = Join-Path $backend 'cpp\src'
$knl = Join-Path $cppSrc 'cuda_kernels.cpp'
$cudaInc = if ($env:CUDA_PATH) { Join-Path $env:CUDA_PATH 'include' } else { $null }
$cudaLib = if ($env:CUDA_PATH) { Join-Path $env:CUDA_PATH 'lib\x64\cudart_static.lib' } else { $null }
$useCuda = Test-Path -LiteralPath $knl
if ($useCuda -and $cudaInc -and (Test-Path -LiteralPath $cudaInc) -and (Test-Path -LiteralPath $cudaLib)) {
    cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc /DXINGCHENG_CUDA /DXINGCHENG_CUDA_KERNELS /I`"$inc`" /I`"$cppSrc`" /I`"$cudaInc`" /Fe`"$exe`" `"$src`" `"$knl`" /Fo`"$root\obj\\`" /link `"$cudaLib`""
} elseif ($useCuda) {
    cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc /DXINGCHENG_CUDA /DXINGCHENG_CUDA_KERNELS /DXINGCHENG_CUDA_DYNRT /I`"$inc`" /I`"$cppSrc`" /Fe`"$exe`" `"$src`" `"$knl`" /Fo`"$root\obj\\`""
} else {
    cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc /I`"$inc`" /Fe`"$exe`" `"$src`" /Fo`"$root\obj\\`""
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exe"

$exDir = Join-Path $root 'executor'
dotnet build $exDir -c Release --nologo | Out-Null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exDir\bin\Release\net10.0\xct-executor.exe"
