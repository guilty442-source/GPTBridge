@echo off
rem Build the native trading-engine components (MSVC).
rem Usage: cmd /c native\build.cmd   (from the tool root)
setlocal
set "VS=E:\Program Files\Microsoft Visual Studio\18\Community"
call "%VS%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 (echo vcvars64 failed & exit /b 1)
cd /d "%~dp0"
cl /nologo /c /W4 risk\risk_core.c /Fo:risk\risk_core.obj || exit /b 1
cl /nologo /c /W4 /EHsc /std:c++17 strategy\strategy_engine.cpp /Fo:strategy\strategy_engine.obj || exit /b 1
rem Link the strategy engine as a DLL for the ctypes facade.
link /nologo /DLL strategy\strategy_engine.obj /OUT:strategy\strategy_engine.dll || exit /b 1
rem Link the risk core as a DLL for the ctypes facade.
link /nologo /DLL risk\risk_core.obj /OUT:risk\risk_core.dll || exit /b 1
echo NATIVE_BUILD_OK
