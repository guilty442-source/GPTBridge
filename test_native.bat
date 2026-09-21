@echo off
call "E:\Progra~1\Micros~1\18\Community\VC\Auxiliary\Build\vcvarsall.bat" x64
"E:\GPTBridge\main-system\.venv\Scripts\python.exe" -c "import _sovereign_native; print('native available:', hasattr(_sovereign_native, 'transformer_matmul'))"