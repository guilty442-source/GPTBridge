# build_verify.ps1 — side-build of xc_modeltool to a non-locked name so
# validation can run while the canonical exe is held by a corpus job.
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
$exe = Join-Path $root 'xc_modeltool_verify.exe'
$cudaInc = "/I`"$env:CUDA_PATH\include`""
$cudaLib = "`"$env:CUDA_PATH\lib\x64\cudart_static.lib`""
if (!(Test-Path (Join-Path $root 'obj_verify'))) { New-Item -ItemType Directory (Join-Path $root 'obj_verify') | Out-Null }
cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc /DXINGCHENG_CUDA /DXINGCHENG_CUDA_KERNELS /I`"$incNat`" /I`"$incCpp`" /I`"$train`" $cudaInc `"$root\xc_modeltool.cpp`" `"$engine`" `"$cppSrc\cuda_bridge.cpp`" `"$cppSrc\cuda_kernels.cpp`" `"$nat\transformer.c`" `"$nat\kv_pool.c`" /Fe`"$exe`" /Fo`"$root\obj_verify\\`" /link $cudaLib"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exe"
