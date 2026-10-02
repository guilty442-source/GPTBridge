# migration-live-catalog-probe-20261002.ps1
#
# Read-only probe: declared migration objects vs live PostgreSQL catalog.
# Evidence for OBL_SCHEMA_DRIFT_GATE / OBL_SQL_GOVERNANCE_CLOSURE —
# sql_schema_drift_evidence has been UNVERIFIED since 2026-09-16 and the
# gptbridge_runtime role can introspect catalogs without write rights.
#
# Method (heuristic, disclosed in the artifact):
#   * Parse each shared-layer/migrations/*.sql for CREATE/DROP of
#     schema,table,index,view,function,trigger,event trigger,policy,
#     sequence,type,extension.  ALTER-created columns/constraints and
#     dynamic SQL inside DO blocks are out of scope (counted, not parsed).
#   * Query pg catalogs for the same object classes.
#   * Emit declared/present/missing per migration file plus live-only
#     objects under gptbridge_* schemas that no migration declares.
#
# Output: <this-dir>/migration-live-catalog-probe-20261002.json

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent `
    (Split-Path -Parent $here)))
$migDir = Join-Path $root 'shared-layer\migrations'
$outFile = Join-Path $here 'migration-live-catalog-probe-20261002.json'
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$dsn = [Environment]::GetEnvironmentVariable('GPTBRIDGE_POSTGRES_DSN','User')
if ([string]::IsNullOrWhiteSpace($dsn)) { throw 'GPTBRIDGE_POSTGRES_DSN_REQUIRED' }
if ($dsn -like 'credman:*') { throw 'credman indirection not supported by probe' }

function Invoke-PgQuery([string]$sql) {
    $raw = & $psql $dsn -Atc $sql 2>&1
    if ($LASTEXITCODE -ne 0) { throw "PSQL_FAIL:$raw" }
    $raw
}

# ---------- live catalog ----------
$liveTables = @{}; $liveViews = @{}; $liveIndexes = @{}
$liveFunctions = @{}; $liveTriggers = @{}; $liveEventTriggers = @{}
$livePolicies = @{}; $liveSchemas = @{}; $liveSequences = @{}

foreach ($line in (Invoke-PgQuery "SELECT nspname||'|'||relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND nspname NOT LIKE 'pg_%' AND nspname<>'information_schema'")) { $liveTables[$line] = $true }
foreach ($line in (Invoke-PgQuery "SELECT nspname||'|'||relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('v','m') AND nspname NOT LIKE 'pg_%' AND nspname<>'information_schema'")) { $liveViews[$line] = $true }
foreach ($line in (Invoke-PgQuery "SELECT schemaname||'|'||indexname FROM pg_indexes WHERE schemaname NOT LIKE 'pg_%'")) { $liveIndexes[$line] = $true }
foreach ($line in (Invoke-PgQuery "SELECT nspname||'|'||proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema'")) { $liveFunctions[$line] = $true }
foreach ($line in (Invoke-PgQuery "SELECT nspname||'|'||tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal")) { $liveTriggers[$line] = $true }
foreach ($line in (Invoke-PgQuery "SELECT evtname FROM pg_event_trigger")) { $liveEventTriggers[$line] = $true }
foreach ($line in (Invoke-PgQuery "SELECT schemaname||'|'||tablename||'|'||policyname FROM pg_policies")) { $livePolicies[$line] = $true }
foreach ($line in (Invoke-PgQuery "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema'")) { $liveSchemas[$line] = $true }
foreach ($line in (Invoke-PgQuery "SELECT nspname||'|'||relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='S' AND nspname NOT LIKE 'pg_%'")) { $liveSequences[$line] = $true }

# ---------- declared objects ----------
$patterns = @(
    @{ cls = 'schema';  rx = 'CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?"?([A-Za-z_][A-Za-z0-9_]*)"?'; q = $false },
    @{ cls = 'table';   rx = 'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?([A-Za-z_][A-Za-z0-9_]*)"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?'; q = $true },
    @{ cls = 'view';    rx = 'CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?([A-Za-z_][A-Za-z0-9_]*)"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?'; q = $true },
    @{ cls = 'index';   rx = 'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(?:"?([A-Za-z_][A-Za-z0-9_]*)"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?'; q = $true },
    @{ cls = 'function';rx = 'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:"?([A-Za-z_][A-Za-z0-9_]*)"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?'; q = $true },
    @{ cls = 'trigger'; rx = 'CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+"?([A-Za-z_][A-Za-z0-9_]*)"?'; q = $false },
    @{ cls = 'event_trigger'; rx = 'CREATE\s+EVENT\s+TRIGGER\s+"?([A-Za-z_][A-Za-z0-9_]*)"?'; q = $false },
    @{ cls = 'policy';  rx = 'CREATE\s+POLICY\s+"?([A-Za-z_][A-Za-z0-9_]*)"?\s+ON\s+(?:"?([A-Za-z_][A-Za-z0-9_]*)"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?'; q = $true },
    @{ cls = 'sequence';rx = 'CREATE\s+SEQUENCE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?([A-Za-z_][A-Za-z0-9_]*)"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?'; q = $true },
    @{ cls = 'extension'; rx = 'CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?"?([A-Za-z_][A-Za-z0-9_\-]*)"?'; q = $false }
)
$dropRx = 'DROP\s+(TABLE|INDEX|VIEW|FUNCTION|TRIGGER|EVENT\s+TRIGGER|POLICY|SEQUENCE)\s+(?:IF\s+EXISTS\s+)?(?:"?([A-Za-z_][A-Za-z0-9_]*)"?\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?'

$liveByClass = @{
    schema = $liveSchemas; table = $liveTables; view = $liveViews
    index = $liveIndexes; function = $liveFunctions
    trigger = $liveTriggers; event_trigger = $liveEventTriggers
    policy = $livePolicies; sequence = $liveSequences
}

$files = Get-ChildItem $migDir -Filter '*.sql' | Sort-Object Name

# Pass 1: collect globally dropped object names (any file) — a DROP in a
# later migration removes the object from the expected set, regardless of
# which file declared it.
$globalDropped = @{}
foreach ($file in $files) {
    $text = [IO.File]::ReadAllText($file.FullName)
    foreach ($m in [regex]::Matches($text, $dropRx, 'IgnoreCase')) {
        $globalDropped[$m.Groups[3].Value.ToLowerInvariant()] = $true
    }
}

$results = [System.Collections.Generic.List[object]]::new()
$totalMissing = 0; $totalDeclared = 0
foreach ($file in $files) {
    $text = [IO.File]::ReadAllText($file.FullName)
    # strip -- comments and dollar-quoted bodies would be ideal; DO blocks
    # counted as dynamic regions instead of parsed.
    $dynamicRegions = [regex]::Matches($text, '\bDO\s+\$').Count
    $declared = [ordered]@{}
    foreach ($p in $patterns) {
        $set = [ordered]@{}
        foreach ($m in [regex]::Matches($text, $p.rx, 'IgnoreCase')) {
            if ($p.q -and $m.Groups.Count -ge 3) {
                $schema = $m.Groups[1].Value; $name = $m.Groups[2].Value
            } else { $schema = ''; $name = $m.Groups[1].Value }
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            if ($p.cls -eq 'policy') {
                # groups: 1=policy name,2=schema,3=table
                $key = "$($m.Groups[2].Value)|$($m.Groups[3].Value)|$($m.Groups[1].Value)"
                $set[$key] = $true; continue
            }
            $key = if ($schema) { "$schema|$name" } else { "|$name" }
            $set[$key] = $true
        }
        $declared[$p.cls] = $set
    }

    $missing = @(); $droppedLater = @(); $present = 0; $declaredCount = 0
    foreach ($cls in $declared.Keys) {
        foreach ($key in $declared[$cls].Keys) {
            $declaredCount++
            $name = ($key -split '\|')[-1]
            if ($globalDropped.ContainsKey($name.ToLowerInvariant())) {
                $droppedLater += "$cls`:$key"; continue
            }
            $found = $false
            if ($key.StartsWith('|')) {
                # unqualified: match bare name in any schema
                $found = @($liveByClass[$cls].Keys | Where-Object { ($_ -split '\|')[-1] -eq $name }).Count -gt 0
            } else {
                $found = $liveByClass[$cls].ContainsKey($key)
            }
            if ($found) { $present++ } else {
                $missing += "$cls`:$key"; $totalMissing++
            }
        }
        $totalDeclared += $declared[$cls].Count
    }
    $results.Add([ordered]@{
        file = $file.Name; declared = $declaredCount
        present_live = $present; missing = $missing
        dropped_elsewhere = $droppedLater
        dynamic_regions = $dynamicRegions
    })
}

# ---------- live-only objects under gptbridge_* ----------
$declaredNames = @{}
foreach ($r in $results) { }
$allDeclared = @{}
foreach ($file in $files) {
    $text = [IO.File]::ReadAllText($file.FullName)
    foreach ($p in $patterns) {
        foreach ($m in [regex]::Matches($text, $p.rx, 'IgnoreCase')) {
            if ($p.q -and $m.Groups.Count -ge 3) { $allDeclared[$m.Groups[2].Value] = $true }
            else { $allDeclared[$m.Groups[1].Value] = $true }
        }
    }
}
$liveOnly = @()
$excludedScratch = 0
foreach ($key in $liveTables.Keys) {
    $schema = ($key -split '\|')[0]
    if ($schema -like 'gptbridge_codex_codex_stage_*' -or
        $schema -like 'vect_test_*') { $excludedScratch++; continue }
    $name = ($key -split '\|')[-1]
    if (-not $allDeclared.ContainsKey($name)) { $liveOnly += "table:$key" }
}
foreach ($key in $liveFunctions.Keys) {
    $schema = ($key -split '\|')[0]
    if ($schema -like 'gptbridge_codex_codex_stage_*' -or
        $schema -like 'vect_test_*') { $excludedScratch++; continue }
    $name = ($key -split '\|')[-1]
    if (-not $allDeclared.ContainsKey($name)) { $liveOnly += "function:$key" }
}
foreach ($key in $liveEventTriggers.Keys) {
    if (-not $allDeclared.ContainsKey($key)) { $liveOnly += "event_trigger:$key" }
}
# gptbridge_codex.* objects are managed by the CodexPipeline amendment
# host, not by shared-layer migrations — expected divergence.
$liveOnlyPipeline = @($liveOnly | Where-Object { $_ -match ':(gptbridge_codex)\|' })

$artifact = [ordered]@{
    id = 'migration-live-catalog-probe-20261002'
    date = '2026-10-02'
    worker = 'devin-desktop'
    subject = 'migration declared-object vs live catalog parity — drift measurement for OBL_SCHEMA_DRIFT_GATE / OBL_SQL_GOVERNANCE_CLOSURE'
    scope = 'shared-layer/migrations/*.sql vs database gptbridge (role gptbridge_runtime, read-only catalog introspection)'
    method_limits = 'heuristic CREATE/DROP parse applied globally in file order (drops anywhere remove the object from expected set); ALTER-created columns/constraints and DO-block dynamic SQL not parsed (counted); unqualified names matched by bare name across schemas; codex_stage_*/vect_test_* scratch schemas excluded from live-only; gptbridge_codex.* objects are CodexPipeline-managed (not migration-owned); registry statuses ordered-source-verified/genesis-baseline-declared assert source ordering, not live application — this probe measures object existence'
    totals = [ordered]@{
        migration_files = $files.Count
        declared_objects = $totalDeclared
        missing_live = $totalMissing
        live_only_objects = $liveOnly.Count
        live_only_codex_pipeline_managed = $liveOnlyPipeline.Count
        live_scratch_excluded = $excludedScratch
        event_triggers_live = $liveEventTriggers.Count
    }
    migrations = $results
    live_only = $liveOnly
}
$artifact | ConvertTo-Json -Depth 8 | Set-Content $outFile -Encoding UTF8
"missing=$totalMissing live_only=$($liveOnly.Count) -> $outFile"
