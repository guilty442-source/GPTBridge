$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'formal-state-generation-bound.ps1'),[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'GENERATION_FENCE_PARSE_FAILED' }
$function = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-ConvergenceGeneration'},$true)
. ([scriptblock]::Create($function.Extent.Text))
$base = @{codex_version='2026-10-02T11:52:44Z';source_sha256=('a'*64);mirror_digest=('b'*64)}
Assert-ConvergenceGeneration $base $base
$passed = 1
foreach ($field in @('codex_version','source_sha256','mirror_digest')) {
    foreach ($value in @('changed','')) {
        $changed = $base.Clone()
        $changed[$field] = $value
        $denied = $false
        try { Assert-ConvergenceGeneration $base $changed }
        catch { if ($_.Exception.Message -ne 'BLOCKED_GENERATION_DRIFT') { throw }; $denied = $true }
        if (-not $denied) { throw "DRIFT_ACCEPTED:$field" }
        $passed++
    }
}
$invalid = $base.Clone()
$invalid.source_sha256 = 'invalid'
try { Assert-ConvergenceGeneration $invalid $invalid; throw 'INVALID_HASH_ACCEPTED' }
catch { if ($_.Exception.Message -ne 'BLOCKED_GENERATION_DRIFT') { throw } }
$passed++
"PASS: $passed generation-fence cases"
