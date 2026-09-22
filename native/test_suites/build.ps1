# Build + run the native test suites (MSVC, no Python).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File native/test_suites/build.ps1
$ErrorActionPreference = "Stop"
$vs = "E:\Program Files\Microsoft Visual Studio\18\Community"
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
$out = Join-Path $PSScriptRoot "bin"
New-Item -ItemType Directory -Force -Path $out | Out-Null

$nativeRoot = Split-Path $PSScriptRoot -Parent
$coreDir = Join-Path $nativeRoot "core"
$auditDir = Join-Path $nativeRoot "audit"
$includeDir = Join-Path $nativeRoot "include"

$suites = @(
    @{ src = "suite_ntp_sampling.cpp"; exe = "ntp_suite.exe" },
    @{ src = "suite_kv_cache.cpp"; exe = "kv_cache_suite.exe" },
    @{ src = "suite_moe_routing.cpp"; exe = "moe_qc_suite.exe" },
    @{ src = "suite_consistency.cpp"; exe = "consistency_suite.exe" },
    @{ src = "suite_maturity.cpp"; exe = "maturity_suite.exe" },
    @{
        src = "suite_runtime_core.cpp"; exe = "runtime_core_suite.exe"
        # E1/E2 原型：連結真實純 C 源檔（非重實作）
        extra = @(
            (Join-Path $coreDir "runtime_core.c"),
            (Join-Path $coreDir "scheduler.c"),
            (Join-Path $coreDir "ipc_registry.c"),
            (Join-Path $coreDir "watchdog.c"),
            (Join-Path $coreDir "outbox.c"),
            (Join-Path $coreDir "maintenance.c")
        )
    },
    @{
        src = "suite_runtime_state.cpp"; exe = "runtime_state_suite.exe"
        # E3 原型：連結真實純 C 源檔
        extra = @(
            (Join-Path $coreDir "runtime_state.c")
        )
    },
    @{
        src = "suite_activation_broker.cpp"; exe = "activation_broker_suite.exe"
        extra = @(
            (Join-Path $coreDir "activation_broker.c")
        )
    },
    @{
        src = "suite_system_rescue.cpp"; exe = "system_rescue_suite.exe"
        # M1 語言外移原型：連結真實純 C 源檔
        extra = @(
            (Join-Path $coreDir "system_rescue.c")
        )
    },
    @{
        src = "suite_governed_tool.cpp"; exe = "governed_tool_suite.exe"
        # M1 governed-tool ABI prototype (sha256 from system_rescue.c)
        extra = @(
            (Join-Path $coreDir "governed_tool.c"),
            (Join-Path $coreDir "system_rescue.c")
        )
    },
    @{
        src = "suite_governed_tool_ws.cpp"; exe = "governed_tool_ws_suite.exe"
        # M1 ABI §3：HTTP/WS 閘門編解碼（零 I/O，自研 SHA-1/base64/frame）
        extra = @(
            (Join-Path $nativeRoot "tool_runtime\governed_tool_ws.cpp")
        )
    },
    @{
        src = "suite_transport_proxy_client.cpp"; exe = "transport_proxy_client_suite.exe"
        # M1 模式 B：proxy 客戶端編解碼＋request_sync 等待語義（零 I/O）
        extra = @(
            (Join-Path $nativeRoot "tool_runtime\transport_proxy_client.cpp")
        )
    },
    @{
        src = "suite_tool_host.cpp"; exe = "tool_host_suite.exe"
        # M1 工具體骨架：loopback HTTP/WS＋claim/execute/respond 端到端
        extra = @(
            (Join-Path $nativeRoot "tool_runtime\tool_host.cpp"),
            (Join-Path $nativeRoot "tool_runtime\governed_tool_ws.cpp"),
            (Join-Path $nativeRoot "tool_runtime\transport_proxy_client.cpp"),
            (Join-Path $nativeRoot "tool_runtime\sidecar_transport.cpp"),
            (Join-Path $coreDir "governed_tool.c"),
            (Join-Path $coreDir "system_rescue.c")
        )
    },

    @{
        src = "suite_a263_channel_core.cpp"; exe = "a263_channel_core_suite.exe"
        # M2 前置：A263 channel 決定性語義（零 I/O，Python 為權威 shadow）
        extra = @(
            (Join-Path $coreDir "a263_channel_core.c")
        )
    },
    @{
        src = "suite_audit_engine.cpp"; exe = "audit_engine_suite.exe"
        # P0-9 審計引擎：連結真實原生實作
        extra = @(
            (Join-Path $auditDir "audit_engine.cpp")
        )
    },
    @{
        src = "suite_baseline.cpp"; exe = "baseline_suite.exe"
        # §10.60 baseline：真實 cpp bundle 載入＋確定性＋反退化（已解鎖）
        inc = @(
            (Join-Path $nativeRoot "..\Standalone tools\local-model\src\backend\cpp\include")
        )
        extra = @(
            (Join-Path $nativeRoot "..\Standalone tools\local-model\src\backend\cpp\src\engine.cpp"),
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "kv_pool.c")
        )
    },
    @{
        src = "suite_eval.cpp"; exe = "eval_suite.exe"
        # §10.60 eval：star-native-eval-v1 ppl＋sanity＋tps 閘門
        inc = @(
            (Join-Path $nativeRoot "..\Standalone tools\local-model\src\backend\cpp\include")
        )
        extra = @(
            (Join-Path $nativeRoot "..\Standalone tools\local-model\src\backend\cpp\src\engine.cpp"),
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "kv_pool.c")
        )
    },
    @{
        src = "suite_dialogue.cpp"; exe = "dialogue_suite.exe"
        # §10.60 dialogue：star-native-eval-dialogue-v1 同構閘門
        inc = @(
            (Join-Path $nativeRoot "..\Standalone tools\local-model\src\backend\cpp\include")
        )
        extra = @(
            (Join-Path $nativeRoot "..\Standalone tools\local-model\src\backend\cpp\src\engine.cpp"),
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "kv_pool.c")
        )
    }
)

# Concurrent-worker guard: two build.ps1 runs racing on the shared
# _build.bat / .obj outputs produce spurious failures; serialize on an
# atomic lock dir (wait ≤ 600 s, then fail closed).
$lockDir = Join-Path $out "_build.lock"
$lockWaited = 0
while (-not (New-Item -ItemType Directory -Path $lockDir -ErrorAction SilentlyContinue)) {
    if ($lockWaited -ge 600) {
        # Stale lock from a crashed run: owner gone → break once.
        if (-not (Get-Process -Name "cl" -ErrorAction SilentlyContinue)) {
            Remove-Item $lockDir -Recurse -Force -ErrorAction SilentlyContinue
            continue
        }
        Write-Output "BUILD LOCKED (concurrent build in progress >600s)"
        exit 1
    }
    Start-Sleep -Seconds 2
    $lockWaited += 2
}

try {

$bat = Join-Path $out "_build.bat"
$lines = @("@echo off", "call `"$vcvars`" >nul || exit /b 1")
foreach ($suite in $suites) {
    $srcPath = Join-Path $PSScriptRoot $suite.src
    $exePath = Join-Path $out $suite.exe
    $extraSrcs = ""
    if ($suite.ContainsKey("extra")) {
        foreach ($e in $suite.extra) { $extraSrcs += " `"$e`"" }
    }
    $extraInc = ""
    if ($suite.ContainsKey("inc")) {
        foreach ($i in $suite.inc) { $extraInc += " /I`"$i`"" }
    }
    $lines += "cl /nologo /std:c++17 /utf-8 /O2 /EHsc /I`"$includeDir`"$extraInc /Fe:$exePath /Fo:$out\ `"$srcPath`"$extraSrcs >nul || exit /b 1"
}
# 獨立審計引擎 CLI（pre-commit 閘門嵌入式）
$auditExe = Join-Path $out "audit-engine.exe"
$auditSrc = Join-Path $auditDir "audit_engine.cpp"
$lines += "cl /nologo /std:c++17 /utf-8 /O2 /EHsc /DGPTBRIDGE_AUDIT_ENGINE_CLI /I`"$includeDir`" /Fe:$auditExe /Fo:$out\ `"$auditSrc`" >nul || exit /b 1"
# M1 模式 B：proxy codec CLI driver（Python interop 測試用，非套件）
$driverExe = Join-Path $out "proxy_client_driver.exe"
$driverSrc = Join-Path $PSScriptRoot "driver_proxy_client.cpp"
$tpxSrc = Join-Path $nativeRoot "tool_runtime\transport_proxy_client.cpp"
$sidecarSrc = Join-Path $nativeRoot "tool_runtime\sidecar_transport.cpp"
$lines += "cl /nologo /std:c++17 /utf-8 /O2 /EHsc /I`"$includeDir`" /Fe:$driverExe /Fo:$out\ `"$driverSrc`" `"$tpxSrc`" `"$sidecarSrc`" >nul || exit /b 1"
Set-Content -Path $bat -Value $lines -Encoding ASCII
cmd /c $bat
if ($LASTEXITCODE -ne 0) { Write-Output "BUILD FAILED"; exit 1 }

$allCases = @()
foreach ($suite in $suites) {
    $exePath = Join-Path $out $suite.exe
    $suiteName = [System.IO.Path]::GetFileNameWithoutExtension($suite.exe)
    $jsonPath = Join-Path $out ("$suiteName.json")
    # 300s 硬上限：任一套件卡死不得讓整個閘門無限等待
    $p = Start-Process -FilePath $exePath -ArgumentList $suiteName `
         -WorkingDirectory $out -NoNewWindow -PassThru
    if (-not $p.WaitForExit(300000)) {
        $p.Kill()
        '{ "suite": "' + $suiteName + '", "cases": [ { "name": "suite_timeout", "status": "FAIL", "detail": "exceeded 300s" } ], "pass": 0, "fail": 1, "blocked": 0 }' |
            Set-Content -Path $jsonPath -Encoding UTF8
        Write-Output ("{0}: TIMEOUT -> FAIL" -f $suite.exe)
    }
    $parsed = Get-Content -Path $jsonPath -Raw | ConvertFrom-Json
    $allCases += $parsed.cases
    Write-Output ("{0}: {1} cases" -f $suite.exe, $parsed.cases.Count)
}
$report = Join-Path $out "native-report.json"
@{ harness = "native-test-suite/v1"; cases = $allCases } | ConvertTo-Json -Depth 5 | Set-Content -Path $report -Encoding UTF8
$failed = @($allCases | Where-Object { $_.status -eq "FAIL" }).Count
$blocked = @($allCases | Where-Object { $_.status -eq "BLOCKED" }).Count
$passed = @($allCases | Where-Object { $_.status -eq "PASS" }).Count
Write-Output ("native-report.json: PASS={0} FAIL={1} BLOCKED={2}" -f $passed, $failed, $blocked)
if ($failed -gt 0) { exit 1 }

} finally {
    Remove-Item $lockDir -Recurse -Force -ErrorAction SilentlyContinue
}
