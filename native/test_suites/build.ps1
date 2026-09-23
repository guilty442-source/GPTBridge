# Build + run the native test suites (MSVC, no Python).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File native/test_suites/build.ps1
#        [-MaxParallel N]   bounded-parallel suite execution (default 4, cap 8)
param([int]$MaxParallel = 4)
$ErrorActionPreference = "Stop"
$MaxParallel = [Math]::Max(1, [Math]::Min(8, $MaxParallel))
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
    @{
        src = "suite_kv_engine.cpp"; exe = "kv_engine_suite.exe"
        # G95：engine 級 KV 覆蓋——真實 gptbridge_kv_pool＋NativeInferenceEngine
        inc = @(
            (Join-Path $nativeRoot "..\Standalone tools\local-model\src\backend\cpp\include")
        )
        extra = @(
            (Join-Path $nativeRoot "..\Standalone tools\local-model\src\backend\cpp\src\engine.cpp"),
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "kv_pool.c")
        )
    },
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
        src = "suite_channel_runtime.cpp"; exe = "channel_runtime_suite.exe"
        # M2：A263 channel 非同步執行面（send/receive loop、state 機、
        #    outbox 持有＋回放）——transport 注入（模式 B）
        extra = @(
            (Join-Path $nativeRoot "tool_runtime\channel_runtime.cpp"),
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
        src = "suite_simd_kernels.cpp"; exe = "simd_kernels_suite.exe"
        # ACC-1：純 C SIMD 核心等價／回退驗收（連結真實 transformer.c／vector.c）
        inc = @($coreDir)
        extra = @(
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "vector.c")
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

# Per-suite build batches + bounded-parallel compile: each suite gets a
# private obj dir (/Fo:obj\<stem>\) so parallel cl invocations cannot
# collide on shared sources (transformer.c, engine.cpp etc. each produce
# identically-named .obj outputs).  Every batch calls vcvars once then its
# own cl line; the whole fleet is scheduled under $MaxParallel with a
# 600s per-batch cap.
$buildJobs = @()
function Add-BuildJob($name, $clLine) {
    $objDir = Join-Path $out ("obj\" + $name)
    New-Item -ItemType Directory -Force -Path $objDir | Out-Null
    $batPath = Join-Path $out ("_build_" + $name + ".bat")
    $lines = @("@echo off", "call `"$vcvars`" >nul || exit /b 1", $clLine)
    Set-Content -Path $batPath -Value $lines -Encoding ASCII
    $script:buildJobs += @{ name = $name; bat = $batPath }
}
foreach ($suite in $suites) {
    $srcPath = Join-Path $PSScriptRoot $suite.src
    $exePath = Join-Path $out $suite.exe
    $suiteName = [System.IO.Path]::GetFileNameWithoutExtension($suite.exe)
    $objDir = Join-Path $out ("obj\" + $suiteName)
    $extraSrcs = ""
    if ($suite.ContainsKey("extra")) {
        foreach ($e in $suite.extra) { $extraSrcs += " `"$e`"" }
    }
    $extraInc = ""
    if ($suite.ContainsKey("inc")) {
        foreach ($i in $suite.inc) { $extraInc += " /I`"$i`"" }
    }
    Add-BuildJob $suiteName ("cl /nologo /std:c++17 /utf-8 /O2 /EHsc /I`"$includeDir`"$extraInc /Fe:$exePath /Fo:$objDir\ `"$srcPath`"$extraSrcs >nul || exit /b 1")
}
# 獨立審計引擎 CLI（pre-commit 閘門嵌入式）
$auditExe = Join-Path $out "audit-engine.exe"
$auditSrc = Join-Path $auditDir "audit_engine.cpp"
$auditObj = Join-Path $out "obj\audit-engine"
Add-BuildJob "audit-engine" ("cl /nologo /std:c++17 /utf-8 /O2 /EHsc /DGPTBRIDGE_AUDIT_ENGINE_CLI /I`"$includeDir`" /Fe:$auditExe /Fo:$auditObj\ `"$auditSrc`" >nul || exit /b 1")
# M1 模式 B：proxy codec CLI driver（Python interop 測試用，非套件）
$driverExe = Join-Path $out "proxy_client_driver.exe"
$driverSrc = Join-Path $PSScriptRoot "driver_proxy_client.cpp"
$tpxSrc = Join-Path $nativeRoot "tool_runtime\transport_proxy_client.cpp"
$sidecarSrc = Join-Path $nativeRoot "tool_runtime\sidecar_transport.cpp"
$driverObj = Join-Path $out "obj\proxy_client_driver"
Add-BuildJob "proxy_client_driver" ("cl /nologo /std:c++17 /utf-8 /O2 /EHsc /I`"$includeDir`" /Fe:$driverExe /Fo:$driverObj\ `"$driverSrc`" `"$tpxSrc`" `"$sidecarSrc`" >nul || exit /b 1")

$bq = [System.Collections.Generic.Queue[object]]::new()
foreach ($j in $buildJobs) { $bq.Enqueue($j) }
$brunning = @{}
$buildFailed = $false
while ($bq.Count -gt 0 -or $brunning.Count -gt 0) {
    while ($bq.Count -gt 0 -and $brunning.Count -lt $MaxParallel) {
        $j = $bq.Dequeue()
        # .NET Process (not Start-Process): ExitCode is reliably readable
        # after exit; Start-Process -PassThru returns empty ExitCode here.
        $psi = [System.Diagnostics.ProcessStartInfo]::new(
            "cmd.exe", "/c `"$($j.bat)`"")
        $psi.WorkingDirectory = $out
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $brunning[$j.name] = @{ proc = $p; deadline = (Get-Date).AddSeconds(600) }
    }
    $bdone = @()
    foreach ($name in @($brunning.Keys)) {
        $h = $brunning[$name]
        if ($h.proc.HasExited) {
            if ($h.proc.ExitCode -ne 0) {
                Write-Output ("BUILD FAILED: {0} (rc={1})" -f $name, $h.proc.ExitCode)
                $buildFailed = $true
            }
            $bdone += $name
        } elseif ((Get-Date) -gt $h.deadline) {
            $h.proc.Kill()
            Write-Output ("BUILD TIMEOUT: {0}" -f $name)
            $buildFailed = $true
            $bdone += $name
        }
    }
    foreach ($name in $bdone) { $brunning.Remove($name) }
    if ($bdone.Count -eq 0 -and $brunning.Count -gt 0) {
        Start-Sleep -Milliseconds 250
    }
}
if ($buildFailed) { Write-Output "BUILD FAILED"; exit 1 }

# Suite manifest for the C# TestSuiteOrchestrator (§10.60.1): the suite
# list is discovered from THIS build manifest, never hardcoded.  Records
# the source revision the binaries were built from for revision checks.
$revision = ""
try {
    $revision = (git -C (Join-Path $nativeRoot "..") rev-parse HEAD 2>$null)
    if ($revision) { $revision = $revision.Trim() }
} catch { $revision = "" }
$manifestSuites = @($suites | ForEach-Object {
    @{ name = [System.IO.Path]::GetFileNameWithoutExtension($_.exe)
       exe = $_.exe; src = $_.src }
})
@{
    schema = "native-suite-manifest/v1"
    built_at_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    revision = $revision
    suites = $manifestSuites
} | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $out "suite-manifest.json") -Encoding UTF8

# Bounded-parallel suite execution: each suite writes a uniquely-named
# <stem>.json report and binds only ephemeral ports, so concurrent runs
# cannot interleave reports or collide on ports.  Per-suite 300s hard cap
# stays: a hung suite must never stall the gate.
$allCases = @()
$pending = [System.Collections.Generic.Queue[object]]::new()
foreach ($suite in $suites) { $pending.Enqueue($suite) }
$running = @{}
$results = @{}
while ($pending.Count -gt 0 -or $running.Count -gt 0) {
    while ($pending.Count -gt 0 -and $running.Count -lt $MaxParallel) {
        $suite = $pending.Dequeue()
        $suiteName = [System.IO.Path]::GetFileNameWithoutExtension($suite.exe)
        $exePath = Join-Path $out $suite.exe
        $jsonPath = Join-Path $out ("$suiteName.json")
        Remove-Item $jsonPath -Force -ErrorAction SilentlyContinue
        $p = Start-Process -FilePath $exePath -ArgumentList $suiteName `
             -WorkingDirectory $out -NoNewWindow -PassThru
        $running[$suiteName] = @{
            proc = $p; deadline = (Get-Date).AddSeconds(300)
            json = $jsonPath; exe = $suite.exe
        }
    }
    $completed = @()
    foreach ($name in @($running.Keys)) {
        $h = $running[$name]
        $timedOut = $false
        if (-not $h.proc.HasExited) {
            if ((Get-Date) -gt $h.deadline) {
                $h.proc.Kill()
                $timedOut = $true
                '{ "suite": "' + $name + '", "cases": [ { "name": "suite_timeout", "status": "FAIL", "detail": "exceeded 300s" } ], "pass": 0, "fail": 1, "blocked": 0 }' |
                    Set-Content -Path $h.json -Encoding UTF8
                Write-Output ("{0}: TIMEOUT -> FAIL" -f $h.exe)
            } else { continue }
        }
        $parsed = Get-Content -Path $h.json -Raw | ConvertFrom-Json
        $results[$name] = $parsed.cases
        if (-not $timedOut) {
            Write-Output ("{0}: {1} cases" -f $h.exe, $parsed.cases.Count)
        }
        $completed += $name
    }
    foreach ($name in $completed) { $running.Remove($name) }
    if ($completed.Count -eq 0 -and $running.Count -gt 0) {
        Start-Sleep -Milliseconds 200
    }
}
foreach ($suite in $suites) {
    $suiteName = [System.IO.Path]::GetFileNameWithoutExtension($suite.exe)
    $allCases += $results[$suiteName]
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
