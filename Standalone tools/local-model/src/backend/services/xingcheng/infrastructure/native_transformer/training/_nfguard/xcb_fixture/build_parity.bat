@echo off
call "E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
cl /nologo /std:c++latest /EHsc /I"E:\GPTBridge\.worktrees\devin\Standalone tools\local-model\src\backend\cpp\src" "%~dp0xcb_parity.cpp" /Fe"%~dp0parity_cpp.exe"
if errorlevel 1 (echo BUILD_FAIL && exit /b 1)
"%~dp0parity_cpp.exe" "%~dp0parity_cpp.xcb"
