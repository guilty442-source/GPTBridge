@echo off
cd /d "%~dp0"
start "" /b wscript.exe //B //Nologo "%~dp0start-hidden.vbs"
exit /b 0
