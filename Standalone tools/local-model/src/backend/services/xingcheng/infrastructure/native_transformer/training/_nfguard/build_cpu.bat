@echo off
call "E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
set CUDA_PATH=
set ROOT=E:\GPTBridge\.worktrees\devin\Standalone tools\local-model\src\backend\services\xingcheng\infrastructure\native_transformer\training
set INC=E:\GPTBridge\.worktrees\devin\native\include
if not exist "%ROOT%\_nfguard\obj_cpu" mkdir "%ROOT%\_nfguard\obj_cpu"
cl /nologo /std:c++latest /utf-8 /O2 /EHsc /I"%INC%" /Fe"%ROOT%\_nfguard\xingcheng_trainer_cpu.exe" "%ROOT%\xingcheng_trainer.cpp" /Fo"%ROOT%\_nfguard\obj_cpu\\"
if errorlevel 1 (echo BUILD_FAIL && exit /b 1)
echo BUILT_OK
