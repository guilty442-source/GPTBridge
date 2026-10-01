@echo off
call "E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
set ROOT=E:\GPTBridge\Standalone tools\local-model\src\backend\services\xingcheng\infrastructure\native_transformer\training
set CPPSRC=E:\GPTBridge\Standalone tools\local-model\src\backend\cpp\src
set INC=E:\GPTBridge\native\include
if not exist "%ROOT%\_nfguard\obj" mkdir "%ROOT%\_nfguard\obj"
cl /nologo /std:c++latest /utf-8 /O2 /EHsc /DXINGCHENG_CUDA /DXINGCHENG_CUDA_KERNELS /I"%INC%" /I"%CPPSRC%" /I"%CUDA_PATH%\include" /Fe"%ROOT%\_nfguard\xingcheng_trainer.exe" "%ROOT%\xingcheng_trainer.cpp" "%CPPSRC%\cuda_kernels.cpp" /Fo"%ROOT%\_nfguard\obj\\" /link "%CUDA_PATH%\lib\x64\cudart_static.lib"
if errorlevel 1 (echo BUILD_FAIL && exit /b 1)
echo BUILT_OK
