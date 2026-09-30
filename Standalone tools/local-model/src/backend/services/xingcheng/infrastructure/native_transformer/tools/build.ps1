# build.ps1 — xc_modeltool native build (C++23, MSVC, no external deps)
$ErrorActionPreference = 'Stop'
$vsvars = 'E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $root '..\..\..\..\..\..\..\..\..')).Path
$incNat = Join-Path $repo 'native\include'
$incCpp = Join-Path $repo 'Standalone tools\local-model\src\backend\cpp\include'
$train = Join-Path $root '..\training'
$engine = Join-Path $repo 'Standalone tools\local-model\src\backend\cpp\src\engine.cpp'
$cppSrc = Join-Path $repo 'Standalone tools\local-model\src\backend\cpp\src'
$nat = Join-Path $repo 'native\core'
$exe = Join-Path $root 'xc_modeltool.exe'
# CUDA lane: capability compiled in (XINGCHENG_CUDA{_KERNELS}); every CUDA
# dependency is LoadLibrary/NVRTC-resolved at run time except cudart, which
# is linked statically so the exe keeps zero CUDA dll imports. Falls back
# to the CPU path fail-closed on hosts without driver/toolkit (B132).
$cudaInc = ''
$cudaLib = ''
$cudaDefs = '/DXINGCHENG_CUDA /DXINGCHENG_CUDA_KERNELS'
if ($env:CUDA_PATH) {
    $cudaInc = "/I`"$env:CUDA_PATH\include`""
    $cudaLib = "`"$env:CUDA_PATH\lib\x64\cudart_static.lib`""
} else {
    throw 'CUDA_PATH not set — toolkit required for the XINGCHENG_CUDA lane'
}
if (!(Test-Path (Join-Path $root 'obj'))) { New-Item -ItemType Directory (Join-Path $root 'obj') | Out-Null }
cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc $cudaDefs /I`"$incNat`" /I`"$incCpp`" /I`"$train`" $cudaInc `"$root\xc_modeltool.cpp`" `"$engine`" `"$cppSrc\cuda_bridge.cpp`" `"$cppSrc\cuda_kernels.cpp`" `"$nat\transformer.c`" `"$nat\kv_pool.c`" /Fe`"$exe`" /Fo`"$root\obj\\`" /link $cudaLib"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exe"
