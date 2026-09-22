@echo off
call "E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
cl /nologo /std:c++17 /utf-8 /O2 /EHsc /I"E:\GPTBridge\native\include" /Fe:E:\GPTBridge\native\test_suites\bin\tool_host_suite.exe /Fo:E:\GPTBridge\native\test_suites\bin\ "E:\GPTBridge\native\test_suites\suite_tool_host.cpp" "E:\GPTBridge\native\tool_runtime\tool_host.cpp" "E:\GPTBridge\native\tool_runtime\governed_tool_ws.cpp" "E:\GPTBridge\native\tool_runtime\transport_proxy_client.cpp" "E:\GPTBridge\native\tool_runtime\sidecar_transport.cpp" "E:\GPTBridge\native\core\governed_tool.c" "E:\GPTBridge\native\core\system_rescue.c"
exit /b %errorlevel%
