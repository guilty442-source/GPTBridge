$ErrorActionPreference = 'Stop'
$vsvars = 'E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$inc = 'E:\GPTBridge\native\include'
$src = Join-Path $root 'xingcheng_trainer.cpp'
$exe = Join-Path $root '..\xingcheng_trainer_legacy.exe'
$cppSrc = 'E:\GPTBridge\Standalone tools\local-model\src\backend\cpp\src'
$knl = Join-Path $cppSrc 'cuda_kernels.cpp'
$cudaInc = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.0\include'
$cudaLib = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.0\lib\x64\cudart_static.lib'
cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc /DXINGCHENG_CUDA /DXINGCHENG_CUDA_KERNELS /I`"$inc`" /I`"$cppSrc`" /I`"$cudaInc`" /Fe`"$exe`" `"$src`" `"$knl`" /Fo`"$root\/obj/`" /link `"$cudaLib`""
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exe"
