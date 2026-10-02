param([switch]$Request,[switch]$Verify,[switch]$Summary)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
Set-Location -LiteralPath $projectRoot
$pipeline = Join-Path $projectRoot 'shared-layer/csharp/GPTBridge.CodexPipeline/publish/GPTBridge.CodexPipeline.exe'
function Invoke-CodexCheck([string]$Operation) {
    $output = & $pipeline $Operation
    if ($LASTEXITCODE -ne 0) { throw "CODEX_READ_FAILED:$Operation" }
    $output | Out-String | ConvertFrom-Json
}
$authority = Invoke-CodexCheck '--authority-state'
$mirrorCheck = Invoke-CodexCheck '--mirror-check'
if (-not $mirrorCheck.ok) { throw 'CODEX_MIRROR_INVALID' }
# Verified projections are evaluation inputs, never a local authority.
$mirrorParts = @(1..5 | ForEach-Object {
    Get-Content -LiteralPath "governance_rule/codex/governance_codex.zh-TW.part-$_.txt" -Raw -Encoding UTF8 | ConvertFrom-Json
})
if (@($mirrorParts | Where-Object codex_version -ne $authority.codex_version).Count) {
    throw 'BLOCKED_GENERATION_DRIFT'
}
$rowCache = @{}
function Get-CodexRows([string]$Table) {
    if (-not $rowCache.ContainsKey($Table)) {
        $owners = @($mirrorParts | Where-Object { $_.tables.PSObject.Properties.Name -contains $Table })
        if ($owners.Count -ne 1) { throw "CODEX_TABLE_PROJECTION_INVALID:$Table" }
        $rowCache[$Table]=@($owners[0].tables.$Table)
    }
    $rowCache[$Table]
}
function Get-Digest([object]$Value) {
    $bytes=[Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 50 -Compress))
    $hasher=[Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() } finally { $hasher.Dispose() }
}
$head=Get-CodexRows 'revision_history' | Sort-Object { [long]$_.sequence } | Select-Object -Last 1
if ($head.version -ne $authority.codex_version -or (Invoke-CodexCheck '--authority-state').codex_version -ne $head.version) {
    throw 'BLOCKED_GENERATION_DRIFT'
}
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
$classes=@(Get-CodexRows 'codex_article_classification' | Where-Object { $_.provision_id -in $activeIds -and $_.status -in @('current','active') })
$classCounts=[ordered]@{}
foreach ($group in ($classes | Group-Object classification | Sort-Object Name)) { $classCounts[$group.Name]=$group.Count }
$unknown=@($activeIds | Where-Object { $_ -notin $classes.provision_id }).Count + @($classes | Where-Object classification -eq 'UNKNOWN').Count
$allSurface=@(Get-CodexRows 'current_normative_surface')
$surface=@($allSurface | Where-Object lifecycle_state -eq 'active')
$surfaceUnknown=@($surface | Where-Object surface_layer -eq 'UNKNOWN')
$directories=@(Get-CodexRows 'directory_master_catalog' | Where-Object { -not $_.retired_version })
$directoryOpen=@($directories | Where-Object implementation_state -notin @('active','verified','complete'))
$canonical=@(Get-CodexRows 'project_architecture_directory')
$normalized=@(Get-CodexRows 'a233_normalized_directory_entry' | Where-Object source_table -eq 'project_architecture_directory')
$directoryMismatch=@(foreach ($row in $canonical) {
    $projection=$normalized | Where-Object source_key -eq $row.architecture_code | Select-Object -First 1
    if (-not $projection) { $row.architecture_code; continue }
    $payload=$projection.domain_payload | ConvertFrom-Json
    foreach ($field in $row.PSObject.Properties.Name) {
        if ($row.$field -cne $payload.$field) { $row.architecture_code; break }
    }
})
$duplicateGroups=@($activeArticles | Group-Object { Get-Digest @($_.rule,$_.prohibition,$_.exception) } | Where-Object Count -gt 1)
$search=@(Get-CodexRows 'codex_search_document')
# Compare using explicit local identities; never compare a row with itself inside a nested pipeline.
$staleSearch=@(foreach ($doc in $search) {
    $life=$lifecycle | Where-Object { $_.provision_type -eq $doc.provision_type -and $_.provision_id -eq $doc.provision_id } | Select-Object -First 1
    if ($doc.version_identity -ne $head.version -or -not $life -or $doc.lifecycle_state -ne $life.lifecycle_state) { $doc.provision_id }
})
$sync=@(Get-CodexRows 'architecture_diagram_sync_evidence' | Where-Object status -eq 'current')
$report=[ordered]@{
    authority='non-authoritative-current-registry-evaluation'; head=$head
    formal=[ordered]@{ total=$formal.Count; participating=$formalCurrent.Count; states=@($formal | Group-Object status | ForEach-Object { @{state=$_.Name;count=$_.Count} }); open_rule_count=$formalFindings.Count; findings=$formalFindings; input_digest=(Get-Digest $formal) }
    machine_schema=[ordered]@{ registered=$schemas.Count; open=$schemaOpen.Count; pending_codes=$schemaOpen; input_digest=(Get-Digest @($schemas,$schemaEvidence)) }
    obligations=[ordered]@{ total=$obligations.Count; counts=$obligationCounts; open=$openObligations.Count; input_digest=(Get-Digest $obligations) }
    classification=[ordered]@{ active_articles=$activeIds.Count; counts=$classCounts; unclassified=$unknown }
    surface=[ordered]@{ active=$surface.Count; unknown=$surfaceUnknown.Count; layers=@($surface | Group-Object surface_layer | ForEach-Object { @{layer=$_.Name;count=$_.Count} }); input_digest=(Get-Digest $surface) }
    directory=[ordered]@{ project_canonical=$canonical.Count;project_normalized=$normalized.Count;project_mismatches=$directoryMismatch;catalog_active=$directories.Count;catalog_open=$directoryOpen.Count;input_digest=(Get-Digest @($canonical,$normalized,$directories)) }
    duplicate=[ordered]@{ exact_groups=$duplicateGroups.Count; semantic_review='PENDING_CURRENT_SEMANTIC_REVIEW' }
    search=[ordered]@{ total=$search.Count;stale=$staleSearch.Count;input_digest=(Get-Digest $search) }
    diagrams=$sync
    epochs=@(Get-CodexRows 'codex_version_epochs'); external=@(Get-CodexRows 'codex_external_closure_requirements'); closure=@(Get-CodexRows 'codex_convergence_closure')
}
if ($Summary) {
    [ordered]@{head=$head;active_articles=$activeIds.Count;unknown_articles=@($surfaceUnknown | Where-Object object_type -eq 'article').Count;nonactive_default_visible=@($allSurface | Where-Object { $_.lifecycle_state -ne 'active' -and $_.default_search_visible -ne 0 }).Count;metrics=@(Get-CodexRows 'codex_convergence_metrics' | Where-Object status -eq 'current');search=$report.search;surface=$report.surface} | ConvertTo-Json -Depth 12
    exit
}
if ($Verify) {
    $checks=[Collections.Generic.List[string]]::new()
    function Assert-State([bool]$Condition,[string]$Name) {
        if (-not $Condition) { throw "FORMAL_STATE_INVARIANT:$Name" }
        $checks.Add($Name)
    }
    $epochRows=@(Get-CodexRows 'codex_version_epochs' | Where-Object epoch -eq $head.version_epoch)
    Assert-State ($epochRows.Count -eq 1 -and $epochRows[0].status -eq 'active') 'single-active-current-epoch'
    $seal=@(Get-CodexRows 'seal_manifest' | Where-Object version -eq $head.version)
    $epochSeal=@(Get-CodexRows 'epoch_seal_manifest' | Where-Object version -eq $head.version)
    $cert=@(Get-CodexRows 'certification_evidence' | Where-Object version -eq $head.version)
    Assert-State ($seal.Count -eq 1 -and $epochSeal.Count -eq 1 -and $seal[0].certification_state -eq 'sealed-governed-authorization' -and $epochSeal[0].certification_state -eq $seal[0].certification_state) 'governance-seal-dimension-parity'
    Assert-State ($cert.Count -eq 1 -and $cert[0].status -eq 'external-signatures-required') 'external-certification-not-fabricated'
    $external=Get-CodexRows 'codex_external_closure_requirements' | Where-Object gate_code -eq 'EXTERNAL_SIGNATURE'
    Assert-State ($external.current_state -eq 'externally-blocked') 'external-threshold-gate-retained'
    $closure=@(Get-CodexRows 'codex_convergence_closure' | Where-Object status -eq 'current')
    Assert-State (@($closure | Where-Object version_identity -ne $head.version).Count -eq 0) 'all-convergence-closure-rows-current'
    $formalClosure=$closure | Where-Object closure_id -eq 'FORMAL_RULE_CLOSURE'
    Assert-State ($formalClosure.open_finding_count -eq $formalFindings.Count -and $formalClosure.result -eq 'INCOMPLETE_EVIDENCE') 'formal-findings-recomputed-not-status-only-pass'
    $schemaClosure=$closure | Where-Object closure_id -eq 'MACHINE_SCHEMA_CLOSURE'
    Assert-State ($schemaClosure.open_finding_count -eq $schemaOpen.Count -and $schemaClosure.result -eq 'INCOMPLETE_EVIDENCE') 'schema-dependent-closure-fail-closed'
    $metric=@(Get-CodexRows 'codex_convergence_metrics' | Where-Object status -eq 'current')
    Assert-State ($metric.Count -eq 1 -and $metric[0].version_identity -eq $head.version -and $metric[0].machine_schema_parity -eq "$($schemas.Count-$schemaOpen.Count)/$($schemas.Count)") 'single-current-metrics-dynamic-schema-denominator'
    Assert-State ($metric[0].active_article_count_after -eq $activeIds.Count -and $metric[0].unknown_article_count -eq @($surfaceUnknown | Where-Object object_type -eq 'article').Count -and $metric[0].superseded_default_search_count -eq @($allSurface | Where-Object { $_.lifecycle_state -ne 'active' -and $_.default_search_visible -ne 0 }).Count) 'current-metrics-match-all-surface-measurements'
    $aggregate=$closure | Where-Object closure_id -eq 'CODEX_CONVERGENCE_CLOSURE'
    foreach ($state in $obligationCounts.Keys) {
        Assert-State ($aggregate.required_subresults.Contains("$state=$($obligationCounts[$state])")) "obligation-lifecycle-count:$state"
    }
    $release=Get-CodexRows 'governance_closure_state' | Where-Object component_code -eq 'VERIFIED_RELEASE'
    $pending=([string]$release.pending_codes).Split('|')
    Assert-State ($release.version_identity -eq $head.version -and $release.certification_state -eq 'VERIFIED_RELEASE_DENIED' -and 'EXTERNAL_SIGNATURE' -in $pending) 'current-verified-release-denied'
    Assert-State (@($openObligations | Where-Object { $_.obligation_code -notin $pending }).Count -eq 0) 'all-open-obligations-in-release-union'
    $format=($articles | Where-Object provision_id -eq 'B17').rule
    $contentBoundary=($articles | Where-Object provision_id -eq 'B5').rule
    Assert-State ($contentBoundary.Contains('the Codex records principles only') -and $contentBoundary.Contains('implementation rules and business rules do not enter the Codex')) 'global-principles-only-content-boundary'
    Assert-State ($format.Contains('CONTENT:B5') -and $format.Contains('MACHINE-PROJECTION:') -and $format.Contains('outside the Codex')) 'external-implementation-projection-not-law'
    $soleAuthority=($articles | Where-Object provision_id -eq 'A1').rule
    Assert-State ($soleAuthority.Contains('the current authoritative Codex is the sole normative authority') -and $soleAuthority.Contains('not parallel authorities')) 'single-codex-normative-authority'
    Assert-State ($contentBoundary.Contains('not independent normative authorities') -and $contentBoundary.Contains('derive exclusively from applicable current Codex principles')) 'owner-local-rules-subordinate-not-law-sources'
    [ordered]@{artifact='formal-state-convergence-verification';authority='non-authoritative-audit-evidence';generation=$head.version;revision=$head.sequence;result='PASS';checks=@($checks);formal_registry=$formal.Count;formal_participating=$formalCurrent.Count;formal_open=$formalFindings.Count;schema_open=$schemaOpen.Count;obligations=$obligationCounts;release='VERIFIED_RELEASE_DENIED';convergence='INCOMPLETE_EVIDENCE'} | ConvertTo-Json -Depth 20
    exit
}
if (-not $Request) { $report | ConvertTo-Json -Depth 50; exit }
if (($articles | Where-Object provision_id -eq 'B5').rule.Contains('the Codex records principles only')) {
    throw 'PRINCIPLES_ONLY_BOUNDARY:legacy law-template request generation is disabled;use an explicit governed principles-only amendment. Read-only evaluation and verification remain available.'
}
$changes=[Collections.Generic.List[object]]::new()
function Add-Change([string]$Table,[object]$Key,[string]$Field,[object]$Value) {
    $existing=Get-CodexRows $Table | Where-Object {
        $row=$_
        @($Key.Keys | Where-Object { $row.$_ -ne $Key[$_] }).Count -eq 0
    } | Select-Object -First 1
    if ($existing -and $existing.$Field -ceq $Value) { return }
    $changes.Add(@{table=$Table;key=$Key;field=$Field;proposed=$Value})
}
function Amend-Law([string]$Id,[string]$Rule,[string]$Prohibition='') {
    Add-Change 'articles' @{provision_id=$Id} 'rule' $Rule
    if ($Prohibition) { Add-Change 'articles' @{provision_id=$Id} 'prohibition' $Prohibition }
}
Amend-Law 'A120' 'EPOCH-AUTHORITY:codex_version_epochs registers epoch identity and recovery genesis;exactly one E2 row is active while E2 is the current epoch. EPOCH-STATE:active means current epoch identity only,not cryptographic certification or release approval. BASELINE:baseline version,genesis identity,predecessor and recovery history head remain immutable origin evidence;they are not overwritten by each successor. CURRENT-GENERATION:metadata,seal_manifest,epoch_seal_manifest,certification_evidence and trust requirements bind the same current generation and registered epoch. GOVERNED-SEAL:sealed-governed-authorization records governed publication and deterministic integrity only. EXTERNAL-CERTIFICATION:D107 and D113 govern real Ed25519 2-of-3 verification;pending external signatures remain a separate release-blocking requirement. RETIREMENT:explicit governed successor only. FAILURE:missing epoch identity,multiple active epoch identities or inconsistent current roots fail closed.'
$d124=($articles | Where-Object provision_id -eq 'D124').rule
$d124=$d124.Replace('CERTIFICATION:sealed-governed-authorization is the sole current certification-state value and must equal current seal,epoch seal and certification evidence.','STATE-DIMENSIONS:seal_manifest and epoch_seal_manifest certification_state are governance-sealing state and must both be sealed-governed-authorization;certification_evidence.status is the distinct external-verification state and may remain external-signatures-required until actual detached verification;codex_version_epochs.status is epoch identity lifecycle and must be active for the current epoch. CONSISTENCY:these typed dimensions share current generation and epoch but must never be compared as one enum. CERTIFIED-SEAL-AND-RELEASE:D107 real Ed25519 2-of-3 verification and every applicable independent closure gate remain mandatory;governed sealing or active epoch identity alone cannot satisfy them.')
Amend-Law 'D124' $d124 'FORBID:stale binding version+search lifecycle copied independently+stale special-law provision link+legacy backing schema claimed canonical+missing normalization plan+state-dimension-conflation+governed seal or active epoch presented cryptographically certified.'
$d107=($articles | Where-Object provision_id -eq 'D107').rule
if (-not $d107.Contains('GOVERNED-SEAL:')) {
    Amend-Law 'D107' ($d107 + ' GOVERNED-SEAL:sealed-governed-authorization is an orthogonal publication/integrity state,not CERTIFIED_SEAL;an active epoch is an identity lifecycle state,not signature verification. EXTERNAL-GATE:the EXTERNAL_SIGNATURE closure requirement remains externally-blocked until actual current-generation Ed25519 threshold verification;an administrative retired-not-required label cannot repeal this gate.')
}
Amend-Law 'B5' 'AUTHORITATIVE-FORM:current Codex authority is the canonical typed provision record with stable identity,lifecycle,lineage,declared normativity and governed generation. DECLARATIVE-NORM:the registered rule payload states the legal duty under D105;human-readable wording is not executable programming source. MACHINE-FORM:B17+D106+D114 separately control typed machine clauses,formal-rule mapping,predicate and execution evidence. CHINESE:the ordered read-only Chinese mirror is a non-authoritative comprehension projection,not an adjudication,execution,import,write or fallback source.' 'FORBID:standalone narrative replacing a registered provision+translation or mirror as adjudication authority+prose directly executed as machine predicate+style treated as semantic parity.'
Amend-Law 'B17' 'REPRESENTATION:canonical typed provision records carry legal identity,lifecycle,generation,lineage and declared normativity under D105. MACHINE-CLAUSE:an authoritative executable clause has a stable clause identifier,typed inputs,deterministic predicate,decision,severity,controlling provision and current semantic parity evidence under D106+D114;uppercase machine tokens and ASCII structural delimiters describe this machine form. DECLARATIVE-NORM:registered human-readable normative rule payloads remain legal declarations under NORMATIVE_MANUAL or their expressly registered class;they are not merely historical because of writing style and cannot become executable machine guards without the corresponding current formal mapping. EXPLANATION:translation,examples,commentary and unregistered legacy prose have no independent authority. SEPARATION:formal_rule_mapping links each machine clause to its canonical legal source;the narrative source and machine form are distinct representations,not interchangeable evaluators. UNMAPPED:preserve the legal duty and mark machine enforcement or release parity INCOMPLETE_EVIDENCE;never infer PASS from narrative or a registered code alone. AMENDMENT:semantic changes require governed successor generation and lineage;mechanical in-place style rewriting under the same identity remains forbidden.' 'FORBID:unstructured narrative as standalone machine authority+prose-only executable guard+translation replacing canonical source+declared legal norm silently reclassified historical+unmapped machine enforcement+format-only parity claim+mechanical in-place style rewrite with same identity.'
Add-Change 'codex_version_epochs' @{epoch=2} 'status' 'active'
foreach ($pair in @(@('current_state','externally-blocked'),@('required_end_state','current-generation Ed25519 2-of-3 detached signatures verified against registered trust anchors'),@('verification','D106+D107+D113:actual detached cryptographic verification;status labels and governed integrity seals are not proof'),@('on_open_gate','CERTIFIED_SEAL and affected verified release denied until actual threshold evidence'))) {
    Add-Change 'codex_external_closure_requirements' @{gate_code='EXTERNAL_SIGNATURE'} $pair[0] $pair[1]
}
$source="source=$($head.version);candidate-generation=bound-by-version_identity-field;"
$hiddenNonactive=@($allSurface | Where-Object { $_.lifecycle_state -ne 'active' -and $_.default_search_visible -ne 0 })
foreach ($row in $hiddenNonactive) {
    Add-Change 'current_normative_surface' @{surface_entry_id=$row.surface_entry_id} 'default_search_visible' 0
}
function Update-Closure([string]$Id,[string]$Required,[string]$Evidence,[long]$Open,[string]$Result) {
    $key=@{closure_id=$Id}
    foreach ($pair in @(@('version_identity','<successor-version>'),@('verified_at','<successor-version>'),@('status','current'),@('verifier','RULE_CODEX_CONVERGENCE/current-registry-evaluation'),@('required_subresults',$Required),@('evidence_roots',($source+$Evidence)),@('open_finding_count',$Open),@('result',$Result))) {
        Add-Change 'codex_convergence_closure' $key $pair[0] $pair[1]
    }
}
$formalEvidence="registry=$($formal.Count);participating=$($formalCurrent.Count);verified-labels=$(@($formalCurrent | Where-Object parity_status -eq 'VERIFIED').Count);retired=$(@($formal | Where-Object status -eq 'retired').Count);withdrawn=$(@($formal | Where-Object status -eq 'withdrawn').Count);input-sha256=$($report.formal.input_digest);open-rules=$($formalFindings.rule_code -join ',');findings-sha256=$(Get-Digest $formalFindings);NO_RUNTIME_PARITY_INFERRED"
Update-Closure 'FORMAL_RULE_CLOSURE' 'all current participating rules require active controlling source,current-generation registry/evaluator/test/prose hashes and independent execution evidence' $formalEvidence $formalFindings.Count $(if ($formalFindings.Count) {'INCOMPLETE_EVIDENCE'} else {'PASS'})
Update-Closure 'MACHINE_SCHEMA_CLOSURE' 'all registered schemas require independent current producer/validator/persistence/canonical execution evidence' "registered=$($schemas.Count);PENDING=$($schemaOpen.Count);input-sha256=$($report.machine_schema.input_digest);activation-and-verified-release=DENIED" $schemaOpen.Count 'INCOMPLETE_EVIDENCE'
Update-Closure 'NORMATIVE_SURFACE_CLOSURE' 'active lifecycle identities require classified canonical current normative surface' "active=$($surface.Count);unknown=$($surfaceUnknown.Count);input-sha256=$($report.surface.input_digest)" $surfaceUnknown.Count $(if ($surfaceUnknown.Count) {'INCOMPLETE_EVIDENCE'} else {'PASS'})
Update-Closure 'DIRECTORY_CLOSURE' 'canonical normalized project parity and registered directory governance acceptance' "project=$($canonical.Count)/$($normalized.Count);semantic-mismatches=$($directoryMismatch.Count);catalog=$($directories.Count);catalog-open=$($directoryOpen.Count);input-sha256=$($report.directory.input_digest)" ($directoryMismatch.Count+$directoryOpen.Count) $(if ($directoryMismatch.Count+$directoryOpen.Count) {'INCOMPLETE_EVIDENCE'} else {'PASS'})
Update-Closure 'DUPLICATION_CLOSURE' 'exact active-payload uniqueness and current independent semantic duplication review' "exact-duplicate-groups=$($duplicateGroups.Count);semantic-review=PENDING_CURRENT_SEMANTIC_REVIEW" ($duplicateGroups.Count+1) 'INCOMPLETE_EVIDENCE'
Update-Closure 'SEARCH_CURRENTNESS_CLOSURE' 'search lifecycle and generation joined to canonical lifecycle;nonactive default visibility denied;candidate search is rebuilt by the governed pipeline' "checked=$($search.Count);source-stale=$($staleSearch.Count);source-nonactive-default-flags=$($hiddenNonactive.Count);nonactive-default-flags-explicitly-cleared=$($hiddenNonactive.Count);input-sha256=$($report.search.input_digest);candidate-publication-requires-search-rebuild" $staleSearch.Count $(if ($staleSearch.Count) {'INCOMPLETE_EVIDENCE'} else {'PASS'})
$projectionOpen=@($sync | Where-Object { $_.result -ne 'PASS' -or $_.hash_match_count -ne $_.required_count -or $_.stale_count -ne 0 -or $_.missing_count -ne 0 }).Count
Update-Closure 'PROJECTION_PARITY_CLOSURE' 'current measured architecture file registry parity;candidate seal/mirror/SQL parity validated by publication pipeline;not implementation certification' "diagram-required=$($sync.required_count);diagram-match=$($sync.hash_match_count);source-result=$($sync.result);independent-mirror-and-SQL-publication-gate=REQUIRED" $projectionOpen $(if ($projectionOpen) {'INCOMPLETE_EVIDENCE'} else {'PASS'})
$stateParts=@($obligationCounts.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ','
$totalOpen=$formalFindings.Count+$schemaOpen.Count+$surfaceUnknown.Count+$directoryMismatch.Count+$directoryOpen.Count+$duplicateGroups.Count+1+$staleSearch.Count+$projectionOpen+$openObligations.Count+1
Update-Closure 'CODEX_CONVERGENCE_CLOSURE' "NORMATIVE_SURFACE_CLOSURE|DIRECTORY_CLOSURE|MACHINE_SCHEMA_CLOSURE|FORMAL_RULE_CLOSURE|SEARCH_CURRENTNESS_CLOSURE|PROJECTION_PARITY_CLOSURE|DUPLICATION_CLOSURE|OBLIGATION:$stateParts|EXTERNAL_SIGNATURE:externally-blocked" "formal=$($formalFindings.Count);schema=$($schemaOpen.Count);surface=$($surfaceUnknown.Count);directory=$($directoryOpen.Count);semantic-duplication-review=1;open-obligations=$($openObligations.Count);external-signature=1;obligation-input-sha256=$($report.obligations.input_digest);counts=gate-instances-not-unique-incidents" $totalOpen 'INCOMPLETE_EVIDENCE'
$oldMetric=Get-CodexRows 'codex_convergence_metrics' | Where-Object status -eq 'current' | Select-Object -Last 1
$metric=[ordered]@{}
foreach ($field in $oldMetric.PSObject.Properties) { $metric[$field.Name]=$field.Value }
$metric.report_id='CONVERGENCE_CURRENT'
$metric.active_article_count_before=$oldMetric.active_article_count_after
$metric.active_article_count_after=$activeIds.Count
$metric.core_article_count=@($surface | Where-Object { $_.object_type -eq 'article' -and $_.surface_layer -eq 'CORE_CONSTITUTIONAL' }).Count
$metric.special_law_count=@($surface | Where-Object { $_.object_type -eq 'article' -and $_.surface_layer -like 'SPECIAL_LAW*' }).Count
$metric.ordinance_count=@($surface | Where-Object { $_.object_type -eq 'article' -and $_.surface_layer -eq 'ORDINANCE' }).Count
foreach ($kind in @('registry_fact','machine_shape','formal_logic','duplicate','mixed')) {
    $metric[$kind+'_article_count_before']=$oldMetric.($kind+'_article_count_after')
    $layer=@{registry_fact='REGISTRY_FACT';machine_shape='MACHINE_SCHEMA';formal_logic='FORMAL-RULE';duplicate='DUPLICATE';mixed='MIXED'}[$kind]
    $metric[$kind+'_article_count_after']=@($surface | Where-Object { $_.object_type -eq 'article' -and $_.surface_layer -eq $layer }).Count
}
$metric.unknown_article_count=@($surfaceUnknown | Where-Object object_type -eq 'article').Count
$metric.stale_effective_count=@(Get-CodexRows 'effective_provisions' | Where-Object { $_.current_binding_version -ne $head.version }).Count
$metric.superseded_default_search_count=0 # candidate explicitly clears every measured nonactive default flag above
$metric.directory_coverage="project:$($canonical.Count)/$($normalized.Count);catalog:$($directories.Count);open:$($directoryOpen.Count)"
$metric.machine_schema_parity="$($schemas.Count-$schemaOpen.Count)/$($schemas.Count)"
$owners=@(Get-CodexRows 'formal_rule_ownership_map' | Where-Object status -in @('current','active'))
$ownerGroups=@($owners | Group-Object invariant_code)
$metric.formal_rule_single_owner_rate="$(@($ownerGroups | Where-Object Count -eq 1).Count)/$($ownerGroups.Count)"
$metric.closure_duplication_count=@(Get-CodexRows 'codex_convergence_closure' | Where-Object status -eq 'current' | Group-Object closure_id | Where-Object Count -gt 1).Count
$metric.result='INCOMPLETE_EVIDENCE';$metric.version_identity='<successor-version>';$metric.status='current'
$metricSuccessors=@()
if ($oldMetric.report_id -eq $metric.report_id) {
    foreach ($field in $metric.Keys) { if ($field -ne 'report_id') { Add-Change 'codex_convergence_metrics' @{report_id=$metric.report_id} $field $metric[$field] } }
} else {
    Add-Change 'codex_convergence_metrics' @{report_id=$oldMetric.report_id} 'status' 'superseded'
    $metricSuccessors=@(@{registry='codex_convergence_metrics';action='insert';rows=@($metric)})
}
$graph=@(Get-CodexRows 'governance_closure_state' | Where-Object status -in @('current','active'))
$releasePending=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($component in $graph) {
    if ($component.component_code -eq 'VERIFIED_RELEASE') { continue }
    if ($component.governance_state -ne 'PASS') {
        [void]$releasePending.Add($component.component_code)
        foreach ($code in ([string]$component.pending_codes).Split('|')) { if ($code) { [void]$releasePending.Add($code) } }
    }
}
foreach ($obligation in $openObligations) { [void]$releasePending.Add($obligation.obligation_code) }
foreach ($code in @('EXTERNAL_SIGNATURE','MACHINE_SCHEMA_PARITY','FORMAL_EVALUATOR_PARITY','CODEX_CONVERGENCE_CLOSURE')) { [void]$releasePending.Add($code) }
function Update-Graph([string]$Code,[string]$Summary,[string]$Pending,[string]$Certification='CODEX_CERTIFICATION_INCOMPLETE') {
    if ($Code -notin $graph.component_code) { throw "CLOSURE_COMPONENT_MISSING:$Code" }
    foreach ($pair in @(@('evidence_summary',$Summary),@('pending_codes',$Pending),@('governance_state','INCOMPLETE_EVIDENCE'),@('certification_state',$Certification),@('version_identity','<successor-version>'),@('status','current'))) {
        Add-Change 'governance_closure_state' @{component_code=$Code} $pair[0] $pair[1]
    }
}
Update-Graph 'FORMAL_EVALUATOR_PARITY' "re-evaluated registry=$($formal.Count);participating=$($formalCurrent.Count);historical-VERIFIED-labels=$($formalCurrent.Count);current-evidence-open=$($formalFindings.Count);registry/evaluator/test/prose proof required;source=$($head.version)" ($formalFindings.rule_code -join '|')
Update-Graph 'IMPLEMENTATION_OBLIGATIONS' "lifecycle-counts:$stateParts;total=$($obligations.Count);open=$($openObligations.Count);evidence-submitted is not accepted;superseded is not open" ($openObligations.obligation_code -join '|')
Update-Graph 'CODEX_CONVERGENCE_CLOSURE' "current registry evaluation;formal-open=$($formalFindings.Count);schema-open=$($schemaOpen.Count);surface-open=$($surfaceUnknown.Count);directory-open=$($directoryOpen.Count);semantic-review-pending=1;open-obligations=$($openObligations.Count);external-signature-blocked=1" 'UNKNOWN_ARTICLE_CLASSIFICATION|MACHINE_SCHEMA_PARITY|FORMAL_RULE_PARITY|DIRECTORY_GOVERNANCE_DATA|SEMANTIC_DUPLICATION_REVIEW|IMPLEMENTATION_OBLIGATIONS|EXTERNAL_SIGNATURE'
Update-Graph 'VERIFIED_RELEASE' "DENIED:union of all registered open components plus all $($openObligations.Count) open obligations and actual external signature gate;no verification inferred from technical audit or status labels" (($releasePending | Sort-Object) -join '|') 'VERIFIED_RELEASE_DENIED'
$requestPayload=[ordered]@{
    artifact='codex-amendment-request';authority='request-only';schema='codex-amendment-request/v1';request_id='certification-epoch-convergence-current-closure-20261002'
    title='Separate formal state dimensions and rebuild current convergence closure'
    summary='Preserve external Ed25519 release gate;activate epoch identity;separate narrative legal declarations from executable machine predicates;re-evaluate 89 formal rules and current closure debt without fabricating PASS.'
    requested_by='decision-sovereign';origin='direct human-governor formal-state and closure audit;Codex/data only,no application runtime implementation'
    change_class='architecture-authority';required_review='five-sovereign-audit-unanimous-pass';flow='A382/A488-non-disruptive-amendment-flow';not_executed=$true;auto_execute=$true
    predecessor=@{codex_version=$head.version;version_identity="E$($head.version_epoch):$($head.version)";version_epoch=$head.version_epoch;history_head=$head.entry_hash;revision_sequence=$head.sequence}
    changes=@($changes);proposed_successors=$metricSuccessors
}
$requestPayload | ConvertTo-Json -Depth 50
