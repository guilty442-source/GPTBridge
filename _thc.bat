@echo off
call "E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
cl /nologo /std:c++latest /utf-8 /O2 /GL /EHsc /I"E:\GPTBridge\native\include" /c /Fo"%TEMP%\tool_host.obj" "E:\GPTBridge\native\tool_runtime\tool_host.cpp" || exit /b 1
cl /nologo /std:c++latest /utf-8 /O2 /GL /EHsc /I"E:\GPTBridge\native\include" /c /Fo"%TEMP%\tool_host_conn.obj" "E:\GPTBridge\native\tool_runtime\tool_host_conn.cpp" || exit /b 1
cl /nologo /std:c++latest /utf-8 /O2 /GL /EHsc /I"E:\GPTBridge\native\include" /c /Fo"%TEMP%\tool_host_claims.obj" "E:\GPTBridge\native\tool_runtime\tool_host_claims.cpp" || exit /b 1
cl /nologo /std:c++latest /utf-8 /O2 /GL /EHsc /I"E:\GPTBridge\native\include" /c /Fo"%TEMP%\tool_host_observe.obj" "E:\GPTBridge\native\tool_runtime\tool_host_observe.cpp" || exit /b 1
echo ALL-OK
