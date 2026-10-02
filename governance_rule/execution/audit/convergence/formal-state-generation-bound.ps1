param([switch]$Verify,[switch]$Summary)
$ErrorActionPreference = 'Stop'

function Assert-ConvergenceGeneration($Before, $After) {
    foreach ($field in @('codex_version','source_sha256')) {
        if ([string]::IsNullOrWhiteSpace([string]$Before.$field) -or
            [string]::IsNullOrWhiteSpace([string]$After.$field) -or
            $Before.$field -cne $After.$field) {
            throw 'BLOCKED_GENERATION_DRIFT'
        }
    }
    if ($Before.source_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $Before.mirror_digest -cnotmatch '^[0-9a-f]{64}$' -or
        $Before.mirror_digest -cne $After.mirror_digest) {
        throw 'BLOCKED_GENERATION_DRIFT'
    }
}

if ($Verify -and $Summary) {
    throw 'CONVERGENCE_MODE_CONFLICT'
}
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
$pipeline = Join-Path $projectRoot 'shared-layer/csharp/GPTBridge.CodexPipeline/publish/GPTBridge.CodexPipeline.exe'
function Read-ConvergenceGeneration {
    $raw = & $pipeline --authority-state --root $projectRoot
    if ($LASTEXITCODE -ne 0) { throw 'CODEX_AUTHORITY_READ_FAILED' }
    $authority = $raw | Out-String | ConvertFrom-Json
    $digests = foreach ($part in 1..5) {
        (Get-FileHash -LiteralPath (Join-Path $projectRoot "governance_rule/codex/governance_codex.zh-TW.part-$part.txt") -Algorithm SHA256).Hash
    }
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = ([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes(($digests -join ':'))))).Replace('-','').ToLowerInvariant()
    } finally { $hasher.Dispose() }
    [pscustomobject]@{codex_version=$authority.codex_version;source_sha256=$authority.source_sha256;mirror_digest=$digest}
}

# Buffer the child result until both authority and mirror inputs are stable.
# The evaluator owns all parity and release verdicts; this entry only fences them.
$before = Read-ConvergenceGeneration
Assert-ConvergenceGeneration $before $before
$arguments = @('-NoProfile','-File',(Join-Path $PSScriptRoot 'formal-state-closure-evaluator.ps1'))
if ($Verify) { $arguments += '-Verify' }
if ($Summary) { $arguments += '-Summary' }
$hostExe = (Get-Process -Id $PID).Path
$output = & $hostExe @arguments
$resultCode = $LASTEXITCODE
$after = Read-ConvergenceGeneration
Assert-ConvergenceGeneration $before $after
if (-not $output) { throw 'CONVERGENCE_RESULT_MISSING' }
# A failed verification remains a failed verification, with its evidence intact.
$output
exit $resultCode
