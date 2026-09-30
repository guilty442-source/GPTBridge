# build.ps1 — xc_modeltool native build (C++23, MSVC, no external deps)
$ErrorActionPreference = 'Stop'
$vsvars = 'E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $root '..\..\..\..\..\..\..\..\..')).Path
$incNat = Join-Path $repo 'native\include'
$incCpp = Join-Path $repo 'Standalone tools\local-model\src\backend\cpp\include'
$train = Join-Path $root '..\training'
$engine = Join-Path $repo 'Standalone tools\local-model\src\backend\cpp\src\engine.cpp'
$nat = Join-Path $repo 'native\core'
$exe = Join-Path $root 'xc_modeltool.exe'
if (!(Test-Path (Join-Path $root 'obj'))) { New-Item -ItemType Directory (Join-Path $root 'obj') | Out-Null }
cmd /c "call `"$vsvars`" >nul 2>&1 && cl /nologo /std:c++latest /utf-8 /O2 /EHsc /I`"$incNat`" /I`"$incCpp`" /I`"$train`" `"$root\xc_modeltool.cpp`" `"$engine`" `"$nat\transformer.c`" `"$nat\kv_pool.c`" /Fe`"$exe`" /Fo`"$root\obj\\`""
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built: $exe"
