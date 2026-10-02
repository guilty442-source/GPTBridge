# build.ps1 — xc_modeltool native build (C++23, MSVC, no external deps)
$ErrorActionPreference = 'Stop'
$vsvars = 'E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $root '..\..\..\..\..\..\..\..\..')).Path
$incNat = Join-Path $repo 'native\include'
$incCpp = Join-Path $repo 'xingcheng\src\backend\cpp\include'
$train = Join-Path $root '..\training'
$engine = Join-Path $repo 'xingcheng\src\backend\cpp\src\engine.cpp'
$cppSrc = Join-Path $repo 'xingcheng\src\backend\cpp\src'
$nat = Join-Path $repo 'native\core'
$exe = Join-Path $root 'xc_modeltool.exe'
# CUDA lane: capability compiled in (XINGCHENG_CUDA{_KERNELS}) with zero
# toolkit dependency — nvcuda.dll is LoadLibrary-bound at run time and the
# device code is self-authored PTX JIT'd by the installed driver. No CUDA
# headers, libs or dlls are needed at build or run time; hosts without an
# NVIDIA driver fail closed to the governed CPU path exactly as before
# (B132). The toolkit is never required.
$cudaDefs = '/DXINGCHENG_CUDA /DXINGCHENG_CUDA_KERNELS'
if (!(Test-Path (Join-Path $root 'obj'))) { New-Item -ItemType Directory (Join-Path $root 'obj') | Out-Null }
cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc $cudaDefs /I`"$incNat`" /I`"$incCpp`" /I`"$cppSrc`" /I`"$train`" `"$root\xc_modeltool.cpp`" `"$engine`" `"$cppSrc\cuda_bridge.cpp`" `"$cppSrc\cuda_kernels.cpp`" `"$nat\transformer.c`" `"$nat\kv_pool.c`" /Fe`"$exe`" /Fo`"$root\obj\\`""
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exe"
