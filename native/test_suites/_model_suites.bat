@echo off
call "E:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
set INC=/I"E:\GPTBridge\native\include" /I"E:\GPTBridge\Standalone tools\local-model\src\backend\cpp\include"
set SRCS="E:\GPTBridge\Standalone tools\local-model\src\backend\cpp\src\engine.cpp" "E:\GPTBridge\native\core\transformer.c" "E:\GPTBridge\native\core\kv_pool.c"
cl /nologo /std:c++17 /utf-8 /O2 /EHsc %INC% /Fe:E:\GPTBridge\native\test_suites\bin\baseline_suite.exe /Fo:E:\GPTBridge\native\test_suites\bin\ "E:\GPTBridge\native\test_suites\suite_baseline.cpp" %SRCS% || exit /b 1
cl /nologo /std:c++17 /utf-8 /O2 /EHsc %INC% /Fe:E:\GPTBridge\native\test_suites\bin\eval_suite.exe /Fo:E:\GPTBridge\native\test_suites\bin\ "E:\GPTBridge\native\test_suites\suite_eval.cpp" %SRCS% || exit /b 1
cl /nologo /std:c++17 /utf-8 /O2 /EHsc %INC% /Fe:E:\GPTBridge\native\test_suites\bin\dialogue_suite.exe /Fo:E:\GPTBridge\native\test_suites\bin\ "E:\GPTBridge\native\test_suites\suite_dialogue.cpp" %SRCS% || exit /b 1
echo ALL_COMPILED
