@echo off
call "E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
cl /nologo /O2 /arch:AVX2 /EHsc gemm_bench.cpp /Fe:gemm_bench.exe >nul 2>&1
if errorlevel 1 (echo BUILD_FAIL && exit /b 1)
gemm_bench.exe
