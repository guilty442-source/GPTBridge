@echo off
call "E:\Progra~1\Micros~1\18\Community\VC\Auxiliary\Build\vcvarsall.bat" x64
cd /d E:\GPTBridge\main-system\src-core\core_system\native
"E:\GPTBridge\main-system\.venv\Scripts\python.exe" build_native.py