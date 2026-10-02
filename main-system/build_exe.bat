@echo off
cd /d "%~dp0"
set "GPTBRIDGE_BOOTSTRAP_ENTRY=%~dp0launcher\bin\GPTBridge.Bootstrap.exe"
if exist "%GPTBRIDGE_BOOTSTRAP_ENTRY%" goto install
if not exist "%~dp0launcher\src\GPTBridge.Bootstrap\GPTBridge.Bootstrap.csproj" goto missing
dotnet publish "%~dp0launcher\src\GPTBridge.Bootstrap\GPTBridge.Bootstrap.csproj" -c Release -o "%~dp0launcher\bin"
if errorlevel 1 exit /b %errorlevel%
:install
"%GPTBRIDGE_BOOTSTRAP_ENTRY%" --project-root "%~dp0." --install-desktop %*
exit /b %errorlevel%
:missing
echo BOOTSTRAP_UNAVAILABLE: native bootstrap and source project are missing. 1>&2
exit /b 1
