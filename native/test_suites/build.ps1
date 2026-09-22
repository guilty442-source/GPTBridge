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
        src = "suite_audit_engine.cpp"; exe = "audit_engine_suite.exe"
        # P0-9 審計引擎：連結真實原生實作
        extra = @(
            (Join-Path $auditDir "audit_engine.cpp")
        )
    },
    @{ src = "suite_blocked.cpp"; exe = "baseline_suite.exe" },
    @{ src = "suite_blocked.cpp"; exe = "eval_suite.exe" },
    @{ src = "suite_blocked.cpp"; exe = "dialogue_suite.exe" }
)

$bat = Join-Path $out "_build.bat"
$lines = @("@echo off", "call `"$vcvars`" >nul || exit /b 1")
foreach ($suite in $suites) {
    $srcPath = Join-Path $PSScriptRoot $suite.src
    $exePath = Join-Path $out $suite.exe
    $extraSrcs = ""
    if ($suite.ContainsKey("extra")) {
        foreach ($e in $suite.extra) { $extraSrcs += " `"$e`"" }
    }
    $lines += "cl /nologo /std:c++17 /utf-8 /O2 /EHsc /I`"$includeDir`" /Fe:$exePath /Fo:$out\ `"$srcPath`"$extraSrcs >nul || exit /b 1"
}
# 獨立審計引擎 CLI（pre-commit 閘門嵌入式）
$auditExe = Join-Path $out "audit-engine.exe"
$auditSrc = Join-Path $auditDir "audit_engine.cpp"
$lines += "cl /nologo /std:c++17 /utf-8 /O2 /EHsc /DGPTBRIDGE_AUDIT_ENGINE_CLI /I`"$includeDir`" /Fe:$auditExe /Fo:$out\ `"$auditSrc`" >nul || exit /b 1"
Set-Content -Path $bat -Value $lines -Encoding ASCII
cmd /c $bat
if ($LASTEXITCODE -ne 0) { Write-Output "BUILD FAILED"; exit 1 }

$allCases = @()
foreach ($suite in $suites) {
    $exePath = Join-Path $out $suite.exe
    $suiteName = [System.IO.Path]::GetFileNameWithoutExtension($suite.exe)
    Push-Location $out
    & $exePath $suiteName | Out-Null
    Pop-Location
    $jsonPath = Join-Path $out ("$suiteName.json")
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
