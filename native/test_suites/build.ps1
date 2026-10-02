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
        # G95：engine �?KV 覆�??�—�?�?gptbridge_kv_pool＋NativeInferenceEngine
        inc = @(
            (Join-Path $nativeRoot "..\xingcheng\src\backend\cpp\include")
        )
        extra = @(
            (Join-Path $nativeRoot "..\xingcheng\src\backend\cpp\src\engine.cpp"),
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "kv_pool.c")
        )
    },
    @{ src = "suite_moe_routing.cpp"; exe = "moe_qc_suite.exe" },
    @{ src = "suite_consistency.cpp"; exe = "consistency_suite.exe" },
    @{ src = "suite_maturity.cpp"; exe = "maturity_suite.exe" },
    @{
        src = "suite_runtime_core.cpp"; exe = "runtime_core_suite.exe"
        # E1/E2 ?��?：�???�實�?C 源�?（�??�實作�?
        extra = @(
            (Join-Path $coreDir "runtime_core.c"),
            (Join-Path $coreDir "scheduler.c"),
            (Join-Path $coreDir "ipc_registry.c"),
            (Join-Path $coreDir "watchdog.c"),
            (Join-Path $coreDir "outbox.c"),
            (Join-Path $coreDir "maintenance.c"),
            (Join-Path $nativeRoot "bridge\gptbridge_native.c")
        )
    },
    @{
        src = "suite_runtime_state.cpp"; exe = "runtime_state_suite.exe"
        # E3 ?��?：�???�實�?C 源�?
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
        # M1 語�?外移?��?：�???�實�?C 源�?
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
        # M1 ABI §3：HTTP/WS ?��?編解碼�???I/O，自??SHA-1/base64/frame�?
        extra = @(
            (Join-Path $nativeRoot "tool_runtime\governed_tool_ws.cpp")
        )
    },
    @{
        src = "suite_transport_proxy_client.cpp"; exe = "transport_proxy_client_suite.exe"
        # M1 模�? B：proxy 客戶端編�?��＋request_sync 等�?語義（零 I/O�?
        extra = @(
            (Join-Path $nativeRoot "tool_runtime\transport_proxy_client.cpp")
        )
    },
    @{
        src = "suite_tool_host.cpp"; exe = "tool_host_suite.exe"
        # M1 工具體骨?��?loopback HTTP/WS＋claim/execute/respond 端到�?
        extra = @(
            (Join-Path $nativeRoot "tool_runtime\tool_host.cpp"),
            (Join-Path $nativeRoot "tool_runtime\tool_host_conn.cpp"),
            (Join-Path $nativeRoot "tool_runtime\tool_host_claims.cpp"),
            (Join-Path $nativeRoot "tool_runtime\tool_host_observe.cpp"),
            (Join-Path $nativeRoot "tool_runtime\governed_tool_ws.cpp"),
            (Join-Path $nativeRoot "tool_runtime\transport_proxy_client.cpp"),
            (Join-Path $nativeRoot "tool_runtime\sidecar_transport.cpp"),
            (Join-Path $coreDir "governed_tool.c"),
            (Join-Path $coreDir "system_rescue.c")
        )
    },

    @{
        src = "suite_a263_channel_core.cpp"; exe = "a263_channel_core_suite.exe"
        # M2 ?�置：A263 channel 決�??��?義�???I/O，Python ?��?�?shadow�?
        extra = @(
            (Join-Path $coreDir "a263_channel_core.c")
        )
    },
    @{
        src = "suite_channel_runtime.cpp"; exe = "channel_runtime_suite.exe"
        # M2：A263 channel ?��?步執行面（send/receive loop?�state 機�?
        #    outbox ?��?＋�??��??�—transport 注入（模�?B�?
        extra = @(
            (Join-Path $nativeRoot "tool_runtime\channel_runtime.cpp"),
            (Join-Path $nativeRoot "tool_runtime\channel_runtime_loops.cpp"),
            (Join-Path $coreDir "a263_channel_core.c")
        )
    },
    @{
        src = "suite_audit_engine.cpp"; exe = "audit_engine_suite.exe"
        # P0-9 審�?引�?：�???�實?��?實�?
        extra = @(
            (Join-Path $auditDir "audit_engine.cpp")
        )
    },
    @{
        src = "suite_simd_kernels.cpp"; exe = "simd_kernels_suite.exe"
        # ACC-1：�? C SIMD ?��?等價／�??�驗收（�???�實 transformer.c／vector.c�?
        inc = @($coreDir)
        extra = @(
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "vector.c")
        )
    },
    @{
        src = "suite_baseline.cpp"; exe = "baseline_suite.exe"
        # §10.60 baseline：�?�?cpp bundle 載入＋確定性�??�退?��?已解?��?
        inc = @(
            (Join-Path $nativeRoot "..\xingcheng\src\backend\cpp\include")
        )
        extra = @(
            (Join-Path $nativeRoot "..\xingcheng\src\backend\cpp\src\engine.cpp"),
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "kv_pool.c")
        )
    },
    @{
        src = "suite_eval.cpp"; exe = "eval_suite.exe"
        # §10.60 eval：star-native-eval-v1 ppl＋sanity＋tps ?��?
        inc = @(
            (Join-Path $nativeRoot "..\xingcheng\src\backend\cpp\include")
        )
        extra = @(
            (Join-Path $nativeRoot "..\xingcheng\src\backend\cpp\src\engine.cpp"),
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "kv_pool.c")
        )
    },
    @{
        src = "suite_dialogue.cpp"; exe = "dialogue_suite.exe"
        # §10.60 dialogue：star-native-eval-dialogue-v1 ?��??��?
        inc = @(
            (Join-Path $nativeRoot "..\xingcheng\src\backend\cpp\include")
        )
        extra = @(
            (Join-Path $nativeRoot "..\xingcheng\src\backend\cpp\src\engine.cpp"),
            (Join-Path $coreDir "transformer.c"),
            (Join-Path $coreDir "kv_pool.c")
        )
    },
    @{
        src = "suite_scheduler_parity.cpp"; exe = "scheduler_parity_suite.exe"
        # G100: scheduler parity ??C vs Python PeriodicScheduler (5-core budget, shadow?�primary gate)
        extra = @(
            (Join-Path $coreDir "scheduler.c")
        )
    },
    @{
        src = "suite_resource_governor.cpp"; exe = "resource_governor_suite.exe"
        # A608: resource-governor C++23 ?�制律�??��??��??��???OS ?��??��???
        # A185 split: control-law units only (no Win32 engine / host layer).
        extra = @(
            (Join-Path $nativeRoot "resource_governor\resource_governor.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_steps.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_budget.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_advisor.cpp")
        )
    },
    @{
        src = "suite_resource_governor_budget.cpp"; exe = "resource_governor_budget_suite.exe"
        # A590/A593/A598: ?��? concurrency ?��??�制律�?8 工�?類別）�?
        extra = @(
            (Join-Path $nativeRoot "resource_governor\resource_governor.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_steps.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_budget.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_advisor.cpp")
        )
    },
    @{
        src = "suite_resource_governor_pools.cpp"; exe = "resource_governor_pools_suite.exe"
        # Pool layer: Pool 歸�?／pools 規�?�??／per-pool ?�享 Job envelope??
        extra = @(
            (Join-Path $nativeRoot "resource_governor\resource_governor.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_steps.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_budget.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_advisor.cpp")
        )
    },
    @{
        src = "suite_resource_governor_advisor.cpp"; exe = "resource_governor_advisor_suite.exe"
        # B167/B38 ?��??�替＋B3 ?��??�適?��?ceiling 上�??�streak/cooldown??
        # 夜�??�電窗口?�fail-closed（�??��?＋�?引�?，零?��??��???
        extra = @(
            (Join-Path $nativeRoot "resource_governor\resource_governor.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_steps.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_budget.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_advisor.cpp")
        )
    },
    @{
        src = "suite_resource_governor_grants.cpp"; exe = "resource_governor_grants_suite.exe"
        # star-resource-request/grant/v1: adjudication + expiry/renew/release
        extra = @(
            (Join-Path $nativeRoot "resource_governor\governor_grants.cpp"),
            (Join-Path $nativeRoot "resource_governor\resource_governor.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_steps.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_cycle_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_rules.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_budget.cpp"),
            (Join-Path $nativeRoot "resource_governor\governor_advisor.cpp")
        )
    }
)

# Concurrent-worker guard: two build.ps1 runs racing on the shared
# _build.bat / .obj outputs produce spurious failures; serialize on an
# atomic lock dir (wait ??600 s, then fail closed). The lock records the
# owner PID: a killed build's finally never runs, so a dead owner is
# reclaimed immediately instead of stalling the next build for 600 s.
$lockDir = Join-Path $out "_build.lock"
$lockWaited = 0
while (-not (New-Item -ItemType Directory -Path $lockDir -ErrorAction SilentlyContinue)) {
    $ownerPidFile = Join-Path $lockDir "owner.pid"
    if (Test-Path $ownerPidFile) {
        $ownerPid = 0
        [void][int]::TryParse(
            ((Get-Content $ownerPidFile -Raw -ErrorAction SilentlyContinue) -as [string]).Trim(),
            [ref]$ownerPid
        )
        if ($ownerPid -le 0 -or -not (Get-Process -Id $ownerPid -ErrorAction SilentlyContinue)) {
            Remove-Item $lockDir -Recurse -Force -ErrorAction SilentlyContinue
            continue
        }
    } elseif (((Get-Date) - (Get-Item $lockDir).CreationTime).TotalSeconds -gt 30) {
        # Lock predates owner tracking or owner died before writing its pid.
        Remove-Item $lockDir -Recurse -Force -ErrorAction SilentlyContinue
        continue
    }
    if ($lockWaited -ge 600) {
        # Stale lock from a crashed run: owner gone ??break once.
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
Set-Content -Path (Join-Path $lockDir "owner.pid") -Value $PID

try {

# Two-phase bounded-parallel build:
#   Phase A ??every UNIQUE translation unit compiles once under
#     $MaxParallel (shared sources like transformer.c / engine.cpp /
#     governor_*.cpp used to recompile once per consuming suite).
#   Phase B ??each suite/driver links from the shared obj pool.
# vcvars is imported into this session once instead of being re-executed
# per job (was ~30 cmd wraps), and /GL+/LTCG is dropped: whole-program
# optimisation roughly doubles compile+link cost for zero coverage gain
# on correctness suites.
$envDump = & cmd.exe /c "`"$vcvars`" >nul 2>&1 && set"
if ($LASTEXITCODE -ne 0) { Write-Output "vcvars64 failed"; exit 1 }
foreach ($line in $envDump) {
    $eq = ([string]$line).IndexOf('=')
    if ($eq -gt 0) {
        [System.Environment]::SetEnvironmentVariable(
            ([string]$line).Substring(0, $eq),
            ([string]$line).Substring($eq + 1))
    }
}
$clExe = $null
try { $clExe = (Get-Command cl.exe -ErrorAction Stop).Source } catch { }
if (-not $clExe) { Write-Output "cl.exe not found after vcvars import"; exit 1 }

$tuObjDir = Join-Path $out "obj\tu"
New-Item -ItemType Directory -Force -Path $tuObjDir | Out-Null

# Dedup key: source path + include set + language + defines ??identical
# (src, incs, flags) pairs share one obj; any flag difference compiles
# separately (e.g. audit_engine.cpp vs its /D CLI variant).
$tuJobs = @{}
function Add-Tu($src, $incs, $std, $defines) {
    $key = $src + "|" + ($incs -join ";") + "|" + $std + "|" + ($defines -join ";")
    if (-not $script:tuJobs.ContainsKey($key)) {
        $idx = $script:tuJobs.Count
        $stem = [System.IO.Path]::GetFileNameWithoutExtension($src)
        $name = "{0:d3}_{1}" -f $idx, $stem
        $script:tuJobs[$key] = @{
            name = $name
            src = $src; incs = $incs; std = $std; defines = $defines
            obj = Join-Path $tuObjDir ($name + ".obj")
        }
    }
    return $script:tuJobs[$key].obj
}
function Add-TuForSource($src, $incs) {
    $std = if ($src -match '\.c$') { "clatest" } else { "c++latest" }
    return Add-Tu $src $incs $std @()
}

$linkJobs = @()
foreach ($suite in $suites) {
    $srcPath = Join-Path $PSScriptRoot $suite.src
    $exePath = Join-Path $out $suite.exe
    $suiteName = [System.IO.Path]::GetFileNameWithoutExtension($suite.exe)
    $srcs = @($srcPath)
    if ($suite.ContainsKey("extra")) { $srcs += @($suite.extra) }
    $incs = @($includeDir)
    if ($suite.ContainsKey("inc")) { $incs += @($suite.inc) }
    $objs = @($srcs | ForEach-Object { Add-TuForSource $_ $incs })
    $linkJobs += @{ name = $suiteName; exe = $exePath; objs = $objs }
}
# ?��?審�?引�? CLI（pre-commit ?��?嵌入式�???own /D ??own TU.
$auditExe = Join-Path $out "audit-engine.exe"
$auditSrc = Join-Path $auditDir "audit_engine.cpp"
$aeObj = Add-Tu $auditSrc @($includeDir) "c++latest" @("/DGPTBRIDGE_AUDIT_ENGINE_CLI")
$linkJobs += @{ name = "audit-engine"; exe = $auditExe; objs = @($aeObj) }
# A608 資�?管制?�主程�?（C++23）�???��?�常駐�?程�???Python ?��??�?��?�?
$govRoot = Join-Path $nativeRoot "resource_governor"
$govExe = Join-Path $govRoot "bin\resource-governor.exe"
New-Item -ItemType Directory -Force -Path (Split-Path $govExe -Parent) | Out-Null
$govSrcFiles = Get-ChildItem -Path $govRoot -Filter "*.cpp" -File | Sort-Object Name
$govObjs = @($govSrcFiles | ForEach-Object { Add-Tu $_.FullName @($includeDir) "c++latest" @() })
$linkJobs += @{ name = "resource-governor"; exe = $govExe; objs = $govObjs }
# M1 模�? B：proxy codec CLI driver（Python interop 測試?��??��?件�?
$driverExe = Join-Path $out "proxy_client_driver.exe"
$driverSrc = Join-Path $PSScriptRoot "driver_proxy_client.cpp"
$tpxSrc = Join-Path $nativeRoot "tool_runtime\transport_proxy_client.cpp"
$sidecarSrc = Join-Path $nativeRoot "tool_runtime\sidecar_transport.cpp"
$drvObjs = @(
    (Add-Tu $driverSrc @($includeDir) "c++latest" @()),
    (Add-Tu $tpxSrc @($includeDir) "c++latest" @()),
    (Add-Tu $sidecarSrc @($includeDir) "c++latest" @()))
$linkJobs += @{ name = "proxy_client_driver"; exe = $driverExe; objs = $drvObjs }
# transport-proxy/v1 線�?�?fixture（�??��?：live sidecar 案�??��?管�?端�?
# ?�代已退役�? Python fixture（D7/B171：測試�??�無 Python）�?
$wireExe = Join-Path $out "proxy_wire_agent.exe"
$wireSrc = Join-Path $PSScriptRoot "proxy_wire_agent.cpp"
$wireObj = Add-Tu $wireSrc @($includeDir) "c++latest" @()
$linkJobs += @{ name = "proxy_wire_agent"; exe = $wireExe; objs = @($wireObj) }

# Generic bounded-parallel job runner: streams are drained async so a
# chatty cl cannot deadlock on a full pipe; a failed job prints its
# captured output.  .NET Process (not Start-Process) keeps ExitCode
# reliably readable.
function Invoke-JobFleet($jobs, $makeArgs, $timeoutSec) {
    $q = [System.Collections.Generic.Queue[object]]::new()
    foreach ($j in $jobs) { $q.Enqueue($j) }
    $running = @{}
    $failed = $false
    while ($q.Count -gt 0 -or $running.Count -gt 0) {
        while ($q.Count -gt 0 -and $running.Count -lt $MaxParallel) {
            $j = $q.Dequeue()
            $psi = [System.Diagnostics.ProcessStartInfo]::new(
                $clExe, (& $makeArgs $j))
            $psi.WorkingDirectory = $out
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $p = [System.Diagnostics.Process]::Start($psi)
            $running[$j.name] = @{
                proc = $p
                deadline = (Get-Date).AddSeconds($timeoutSec)
                outTask = $p.StandardOutput.ReadToEndAsync()
                errTask = $p.StandardError.ReadToEndAsync()
            }
        }
        $done = @()
        foreach ($name in @($running.Keys)) {
            $h = $running[$name]
            if ($h.proc.HasExited) {
                if ($h.proc.ExitCode -ne 0) {
                    # Write-Host, not Write-Output: anything emitted here
                    # would enter the function's return pipeline and break
                    # the caller's -not $ok check.
                    Write-Host ("BUILD FAILED: {0} (rc={1})" -f $name, $h.proc.ExitCode)
                    Write-Host ($h.outTask.Result + $h.errTask.Result)
                    $failed = $true
                }
                $done += $name
            } elseif ((Get-Date) -gt $h.deadline) {
                $h.proc.Kill()
                Write-Host ("BUILD TIMEOUT: {0}" -f $name)
                $failed = $true
                $done += $name
            }
        }
        foreach ($name in $done) { $running.Remove($name) }
        if ($done.Count -eq 0 -and $running.Count -gt 0) {
            Start-Sleep -Milliseconds 150
        }
    }
    return -not $failed
}

$compileArgs = {
    param($j)
    $stdFlag = if ($j.std -eq "clatest") { "/std:clatest" } else { "/std:c++latest" }
    $eh = if ($j.std -eq "clatest") { "" } else { " /EHsc" }
    $defs = ($j.defines -join " ")
    $incs = (($j.incs | ForEach-Object { "/I`"$_`"" }) -join " ")
    return "/nologo $stdFlag /utf-8 /O2$eh $defs $incs /c /Fo`"$($j.obj)`" `"$($j.src)`""
}
$linkArgs = {
    param($j)
    $objArgs = (($j.objs | ForEach-Object { "`"$_`"" }) -join " ")
    return "/nologo $objArgs /Fe`"$($j.exe)`""
}

if (-not (Invoke-JobFleet @($tuJobs.Values) $compileArgs 600)) {
    Write-Output "BUILD FAILED"; exit 1
}
if (-not (Invoke-JobFleet $linkJobs $linkArgs 300)) {
    Write-Output "BUILD FAILED"; exit 1
}

# Suite manifest for the C# TestSuiteOrchestrator (§10.60.1): the suite
# list is discovered from THIS build manifest, never hardcoded.  Records
# the source revision the binaries were built from for revision checks.
$revision = ""
try {
    $revision = (git -C (Join-Path $nativeRoot "..") rev-parse HEAD 2>$null)
    if ($revision) { $revision = $revision.Trim() }
} catch { $revision = "" }
$manifestSuites = @($suites | ForEach-Object {
    $exePath = Join-Path $out $_.exe
    $exeHash = ""
    if (Test-Path $exePath) {
        $exeHash = (Get-FileHash -Algorithm SHA256 -Path $exePath).Hash.ToLowerInvariant()
    }
    @{ name = [System.IO.Path]::GetFileNameWithoutExtension($_.exe)
       exe = $_.exe; src = $_.src; sha256 = $exeHash }
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
