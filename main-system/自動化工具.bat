@echo off
cd /d "%~dp0"
if not exist "%~dp0launcher\bin\GPTBridge.Bootstrap.exe" goto missing
start "" /b wscript.exe //B //Nologo "%~dp0start-hidden.vbs"
exit /b %errorlevel%
:missing
echo BOOTSTRAP_UNAVAILABLE: run build_exe.bat to publish the native launcher. 1>&2
exit /b 1
