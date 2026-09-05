@echo off
REM dev-toolchain.cmd -- load the local C/C++ toolchain into this shell.
REM
REM Managed by the System Third-Party Sub-Sovereign
REM (SYSTEM_THIRD_PARTY_MANAGER, authority=third-party-software-management).
REM Codex basis: P25 / A51 / E37; FORMAL-TOOLS include python/typescript/cpp/c.
REM
REM Usage:
REM   call dev-toolchain.cmd            opens the MSVC x64 developer shell
REM   dev-toolchain.cmd --which         prints detected compiler paths only
REM   dev-toolchain.cmd --build         runs vcvars64 then compiles sovereign-native
REM
REM Non-interactive fallback: set GPTBRIDGE_VSVARS_DISABLE=1 to skip.

setlocal EnableExtensions

set "VSROOT=E:\Program Files\Microsoft Visual Studio\18\Community"
set "VCVARS=%VSROOT%\VC\Auxiliary\Build\vcvars64.bat"
set "NATIVE_BUILD=E:\GPTBridge\main-system\src-core\core_system\native\build_native.py"

if /I "%~1"== "--which" (
    goto :which
)

if /I "%~1"== "--build" (
    if not exist "%VCVARS%" (
        echo [dev-toolchain] ERROR: vcvars64.bat not found at "%VCVARS%".
        exit /b 1
    )
    call "%VCVARS%" >nul
    if errorlevel 1 exit /b %ERRORLEVEL%
    python "%NATIVE_BUILD%"
    exit /b %ERRORLEVEL%
)

if defined GPTBRIDGE_VSVARS_DISABLE (
    echo [dev-toolchain] GPTBRIDGE_VSVARS_DISABLE is set; leaving PATH unchanged.
    exit /b 0
)

if not exist "%VCVARS%" (
    echo [dev-toolchain] ERROR: vcvars64.bat not found at "%VCVARS%".
    echo [dev-toolchain] Install the Desktop development with C++ workload.
    exit /b 1
)

call "%VCVARS%" >nul
if errorlevel 1 exit /b %ERRORLEVEL%
echo [dev-toolchain] MSVC x64 developer environment active:
for /f "delims=" %%p in ('where cl') do echo   cl   = %%p
for /f "delims=" %%p in ('where clang-cl') do echo   clang= %%p
for /f "delims=" %%p in ('where link') do echo   link = %%p
exit /b 0

:which
if not exist "%VCVARS%" (
    echo [dev-toolchain] ERROR: vcvars64.bat not found at "%VCVARS%".
    exit /b 1
)
call "%VCVARS%" >nul
if errorlevel 1 exit /b %ERRORLEVEL%
for /f "delims=" %%p in ('where cl') do echo cl=%%p
for /f "delims=" %%p in ('where clang-cl') do echo clang-cl=%%p
for /f "delims=" %%p in ('where link') do echo link=%%p
exit /b 0