@echo off
call "E:\Progra~1\Micros~1\18\Community\VC\Auxiliary\Build\vcvarsall.bat" x64
"E:\GPTBridge\main-system\.venv\Scripts\python.exe" -c "import sys; print(sys.executable)"