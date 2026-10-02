@echo off
call "E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
set ROOT=%~dp0..
set CPPSRC=E:\GPTBridge\.worktrees\devin\xingcheng\src\backend\cpp\src
set INC=E:\GPTBridge\.worktrees\devin\native\include
if not exist "%~dp0obj" mkdir "%~dp0obj"
cl /nologo /std:c++latest /utf-8 /O2 /EHsc /DXINGCHENG_CUDA /DXINGCHENG_CUDA_KERNELS /DXINGCHENG_CUDA_DYNRT /I"%INC%" /I"%CPPSRC%" "%ROOT%\xingcheng_trainer.cpp" "%CPPSRC%\cuda_kernels.cpp" /Fe"%~dp0xingcheng_trainer.exe" /Fo"%~dp0obj\\"
if errorlevel 1 (echo BUILD_FAIL && exit /b 1)
echo BUILT_OK
