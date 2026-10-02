param([switch]$Request)
$ErrorActionPreference = 'Stop'
$codexLines = Get-Content -LiteralPath 'governance_rule/codex/data/governance_codex.sql' -Encoding UTF8
$rowCache = @{}
function Get-CodexRows([string]$Table) {
    if (-not $rowCache.ContainsKey($Table)) {
        $prefix = 'INSERT INTO "' + $Table + '" '
        $parsed = foreach ($line in $codexLines) {
            if (-not $line.StartsWith($prefix)) { continue }
            $split = $line.IndexOf(' VALUES ')
            $names = @([regex]::Matches($line.Substring(0,$split), '"([^"]+)"') | Select-Object -Skip 1 | ForEach-Object { $_.Groups[1].Value })
            $tokens = [regex]::Matches($line.Substring($split), "E'((?:\\.|[^'\\])*)'|\bNULL\b|-?\d+(?:\.\d+)?")
            if ($names.Count -ne $tokens.Count) { throw "SQL_PARSE:$Table" }
            $fields = [ordered]@{}
            for ($index=0; $index -lt $names.Count; $index++) {
                $token=$tokens[$index]
                if ($token.Groups[1].Success) {
                    $value = [regex]::Replace($token.Groups[1].Value, '\\(.)', {
                        param($match)
                        switch ($match.Groups[1].Value) { 'n' { "`n" } 'r' { "`r" } 't' { "`t" } default { $match.Groups[1].Value } }
                    })
                } elseif ($token.Value -eq 'NULL') { $value=$null } else { $value=[long]$token.Value }
                $fields[$names[$index]]=$value
            }
            [pscustomobject]$fields
        }
        $rowCache[$Table]=@($parsed)
    }
    $rowCache[$Table]
}
function Get-Digest([object]$Value) {
    $bytes=[Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 50 -Compress))
    $hasher=[Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() } finally { $hasher.Dispose() }
}
$head=Get-CodexRows 'revision_history' | Select-Object -Last 1
$lifecycle=@(Get-CodexRows 'provision_lifecycle_status')
$articles=@(Get-CodexRows 'articles')
$activeIds=@($lifecycle | Where-Object { $_.provision_type -eq 'article' -and $_.lifecycle_state -eq 'active' } | ForEach-Object provision_id)
$activeArticles=@($articles | Where-Object { $_.provision_id -in $activeIds })
$formal=@(Get-CodexRows 'formal_rule_registry')
$formalCurrent=@($formal | Where-Object { $_.status -notin @('retired','withdrawn','superseded','inactive') })
$evaluators=Get-Content 'governance_rule/execution/formal_rules/evaluators.json' -Encoding UTF8 -Raw | ConvertFrom-Json
$formalFindings=@(foreach ($rule in $formalCurrent) {
    $reasons=[Collections.Generic.List[string]]::new()
    if ($rule.rule_code -notin $evaluators.REGISTERED_RULE_CODES) { $reasons.Add('EVALUATOR_NOT_REGISTERED') }
    if ($rule.controlling_provision_id -notin $activeIds) { $reasons.Add('CONTROLLING_PROVISION_NOT_ACTIVE') }
    if ($rule.version_identity -ne $head.version) { $reasons.Add('CURRENT_GENERATION_EVIDENCE_MISSING') }
    if ($rule.status -ne 'evaluator-parity-verified' -or $rule.parity_status -ne 'VERIFIED') { $reasons.Add('PARITY_NOT_VERIFIED') }
    foreach ($field in @('semantic_hash','evaluator_hash','test_contract_hash','controlling_provision_hash')) {
        if ([string]$rule.$field -notmatch '^[0-9a-f]{64}$') { $reasons.Add("MISSING_HASH:$field") }
    }
    if ([string]::IsNullOrWhiteSpace($rule.parity_evidence_id)) { $reasons.Add('PARITY_EVIDENCE_ID_MISSING') }
    # A historical digest or registered code is not an executed current receipt.
    if ($reasons.Count -gt 0) { [pscustomobject]@{ rule_code=$rule.rule_code; reasons=@($reasons) } }
})
$schemas=@(Get-CodexRows 'machine_schema_registry')
$schemaEvidence=@(Get-CodexRows 'machine_schema_parity_evidence')
$schemaOpen=@(foreach ($schema in $schemas) {
    $evidence=$schemaEvidence | Where-Object schema_code -eq $schema.schema_code | Select-Object -Last 1
    $hashes=@($evidence.producer_semantic_hash,$evidence.validator_semantic_hash,$evidence.persistence_semantic_hash,$evidence.canonical_semantic_hash)
    if ($schema.parity_status -ne 'PASS' -or $null -eq $evidence -or $evidence.status -ne 'PASS' -or $evidence.validated_against_version -ne $head.version -or @($hashes | Where-Object { $_ -notmatch '^[0-9a-f]{64}$' }).Count -gt 0 -or @($hashes | Select-Object -Unique).Count -ne 1) { $schema.schema_code }
})
$obligations=@(Get-CodexRows 'implementation_obligations')
$obligationCounts=[ordered]@{}
foreach ($group in ($obligations | Group-Object current_state | Sort-Object Name)) { $obligationCounts[$group.Name]=$group.Count }
$openObligations=@($obligations | Where-Object current_state -notin @('complete','superseded'))
$classes=@(Get-CodexRows 'codex_article_classification' | Where-Object { $_.provision_id -in $activeIds -and $_.status -eq 'current' })
$classCounts=[ordered]@{}
foreach ($group in ($classes | Group-Object classification | Sort-Object Name)) { $classCounts[$group.Name]=$group.Count }
$unknown=@($activeIds | Where-Object { $_ -notin $classes.provision_id }).Count + @($classes | Where-Object classification -eq 'UNKNOWN').Count
$report=[ordered]@{
    authority='non-authoritative-current-registry-evaluation'; head=$head
    formal=[ordered]@{ total=$formal.Count; participating=$formalCurrent.Count; states=@($formal | Group-Object status | ForEach-Object { @{state=$_.Name;count=$_.Count} }); open_rule_count=$formalFindings.Count; findings=$formalFindings; input_digest=(Get-Digest $formal) }
    machine_schema=[ordered]@{ registered=$schemas.Count; open=$schemaOpen.Count; pending_codes=$schemaOpen; input_digest=(Get-Digest @($schemas,$schemaEvidence)) }
    obligations=[ordered]@{ total=$obligations.Count; counts=$obligationCounts; open=$openObligations.Count; input_digest=(Get-Digest $obligations) }
    classification=[ordered]@{ active_articles=$activeIds.Count; counts=$classCounts; unclassified=$unknown }
    epochs=@(Get-CodexRows 'codex_version_epochs'); external=@(Get-CodexRows 'codex_external_closure_requirements'); closure=@(Get-CodexRows 'codex_convergence_closure')
}
if (-not $Request) { $report | ConvertTo-Json -Depth 50; exit }
throw 'Request construction is not enabled until live inspection completes.'
