param([string]$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..')))
$ErrorActionPreference = 'Stop'
$engine = Join-Path $Root 'native/test_suites/bin/audit-engine.exe'
$manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $Root 'governance_rule/execution/audit/audit_checks_manifest.json') | ConvertFrom-Json
$checks = @($manifest.checks | Where-Object { $_.id -like 'python-retirement:source:*' -or $_.id -eq 'python-minimization:bucket-budget' })
if ($checks.Count -ne 4) { throw 'PYTHON_RETIREMENT_CHECKS_MISSING' }
$baseline = Get-Content -Raw -Encoding UTF8 (Join-Path $Root 'main-system/config/python-minimization-baseline.json') | ConvertFrom-Json
foreach ($group in @($baseline.zero_targets, $baseline.allowed_zones)) {
    foreach ($entry in $group.PSObject.Properties) {
        if ($entry.Value.files -ne 0 -or $entry.Value.loc -ne 0) { throw 'PYTHON_NONZERO_BUDGET' }
    }
}
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('gptbridge-retirement-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory((Join-Path $fixture 'main-system/config')) | Out-Null
$encoding = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path $fixture 'main-system/config/python-minimization-baseline.json'), ($baseline | ConvertTo-Json -Depth 25), $encoding)
$fixtureManifest = Join-Path $fixture 'manifest.json'
[IO.File]::WriteAllText($fixtureManifest, (@{checks=$checks} | ConvertTo-Json -Depth 12), $encoding)
$cases = [Collections.Generic.List[object]]::new()
function Invoke-FixtureAudit {
    $output = & $engine --manifest $fixtureManifest --root $fixture
    $script:engineExit = $LASTEXITCODE
    $output | Out-String | ConvertFrom-Json
}
$clean = Invoke-FixtureAudit
if ($engineExit -ne 0 -or $clean.failed -ne 0 -or $clean.total -ne 4) { throw 'PYTHON_CLEAN_FIXTURE_FAILED' }
$cases.Add(@{name='clean-tree';result='PASS'})
# Inject source text only; no Python process or interpreter is executed.
foreach ($relative in @('new-owner/worker.py', 'tests/test_regression.py', 'scripts/build_tool.py', 'training/job.py', 'governance/adapter.py', 'runtime/on_demand.py', 'new-owner/launcher.pyw', 'new-owner/types.pyi')) {
    $file = Join-Path $fixture $relative
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($file)) | Out-Null
    [IO.File]::WriteAllText($file, "retired_source`n", $encoding)
    $report = Invoke-FixtureAudit
    if ($engineExit -eq 0 -or $report.failed -eq 0) { throw "PYTHON_RESURRECTION_ACCEPTED:$relative" }
    if ($relative.EndsWith('.py') -and @($report.checks | Where-Object { $_.id -eq 'python-minimization:bucket-budget' -and $_.status -eq 'FAIL' }).Count -ne 1) {
        throw "PYTHON_ZERO_BUDGET_NOT_ENFORCED:$relative"
    }
    $cases.Add(@{name=$relative;result='PASS'})
    Remove-Item -LiteralPath $file
}
@{artifact='python-retirement-regression';result='PASS';cases=@($cases);fixture=$fixture} | ConvertTo-Json -Depth 5
