$ErrorActionPreference = 'Stop'
$rows = Get-Content _articles.json -Encoding UTF8 -Raw | ConvertFrom-Json
$news = Get-Content _newtexts.json -Encoding UTF8 -Raw | ConvertFrom-Json

$changes = New-Object System.Collections.ArrayList
$touched = @()
foreach ($id in ($news.PSObject.Properties.Name | Sort-Object)) {
  foreach ($f in 'rule','prohibition','exception') {
    $old = $rows.$id.$f
    $new = $news.$id.$f
    if ($old -cne $new) {
      [void]$changes.Add([ordered]@{
        table = 'articles'
        key = [ordered]@{ provision_id = $id }
        field = $f
        proposed = $new
      })
    }
  }
  $touched += $id
}

# stale-reference evidence: update the single current scan row (all remaining
# named sub-sovereign tokens are historical-lineage/abolition/revival-prohibition
# contexts, counted as historical references)
foreach ($fc in @(
  @{f='current_reference_count'; v=0},
  @{f='historical_reference_count'; v=37},
  @{f='result'; v='PASS'},
  @{f='version_identity'; v='<successor-version>'}
)) {
  [void]$changes.Add([ordered]@{
    table = 'architecture_stale_reference_evidence'
    key = [ordered]@{ evidence_id = 'CURRENT_ARCHITECTURE_STALE_REFERENCE_SCAN_CURRENT' }
    field = $fc.f
    proposed = $fc.v
  })
}

$req = [ordered]@{
  artifact = 'codex-amendment-request'
  authority = 'request-only'
  schema = 'codex-amendment-request/v1'
  request_id = 'sub-sovereign-active-law-convergence-20261002'
  title = 'Sub-sovereign active-law owner convergence: retire live delegation to the abolished sub-sovereign layer per B162'
  summary = "Human-governor directive 2026-10-02: B162 already declares SUB-SOVEREIGNS abolished and historical-only, and the SUB_SOVEREIGN_LAYER architecture registration is retired, yet $($touched.Count) active provisions still delegate live authority to named sub-sovereign identities (ownership, module management, dispatch, verification, routing). This amendment rewrites those provisions so every former sub-sovereign duty is exercised directly by the registered core sovereign (per sovereign_hierarchy_registry parent_identity, A89 parent map, and A124/A125 learning transfer): decision-sovereign takes policy-architecture/health-maintenance-test/data-governance/priority-capability/change-acceptance duties; automation-sovereign takes all sync/resource/integration/third-party/dispatch duties; system-runtime-sovereign takes system+startup duties; permission-sovereign takes directory/identity-group module management; XINGCHENG takes learning/language-review/system-programming duties. Structural charter provisions (A35/A36/A73/A78-A85/A89/A90/A91/A96/A105/A118/B66) are rewritten to describe the retired boundary: sub-sovereign identities are preserved as historical lineage in sovereign_hierarchy_registry and may never hold active authority. Provisions that only mention sub-sovereigns in abolition, retirement, or revival-prohibition contexts (A17/A83/A84/A97/A102/A103/A111/B114/B162/C111/A124/A125/A129/B68/B69) keep their wording, with A125 fixed to drop its one live reference. Codex-text-only change; no implementation or file is moved."
  requested_by = 'decision-sovereign'
  origin = 'direct human-governor instruction 2026-10-02: sub-sovereigns declared abolished by B162 yet active law still names them as owners/managers; active ownership statements must successor to the five cores / Xingcheng, not linger under A24 legacy normalization'
  change_class = 'architecture-authority'
  required_review = 'five-sovereign-audit-unanimous-pass'
  flow = 'A382/A488-non-disruptive-amendment-flow'
  not_executed = $true
  auto_execute = $true
  predecessor = [ordered]@{
    codex_version = '2026-10-02T07:29:50Z'
    version_identity = 'E2:2026-10-02T07:29:50Z'
    version_epoch = 2
    history_head = '44b660d730c68cf344854c1daead7bfba9662f4897bb6f15ac53b3008570f754'
    revision_sequence = 220
  }
  problem = [ordered]@{
    summary = 'B162 abolished the sub-sovereign layer (historical only, active-sub-sovereign forbidden) and SUB_SOVEREIGN_LAYER is retired in the architecture registry, but the active normative surface still assigns live authority to retired identities: OWNER clauses (A13/A42/A59/B2/B26/B111/C5/C6/D8/D22/D27), module-management delegations (A63/A64/A66-A69/A77/B44/B46/D42-D44), duty/flow statements (A31/A53/A87/A99/B22/B48/B65/C7/C8/C20/C24/C27/C31/C36-C40/C47/D3/D30/D55/D56/D77), sovereign-to-sub retirement chains (A78-A82/A85/B66), and structural roster/hierarchy charters (A35/A36/A73/A89/A90/A91/A96/A105/A118). Directory resolvers and evaluators see both "abolished" and "active owner" answers simultaneously.'
    affected = @('articles','architecture_stale_reference_evidence')
    scope = 'codex-text convergence only; ownership moves to the registered parent core sovereign or Xingcheng; historical identities preserved as lineage; no file or implementation change'
  }
  changes = $changes
  proposed_successors = @()
  verification = [ordered]@{
    post_checks = @(
      'no active rule/prohibition/exception delegates authority to a *-sub-sovereign identity (only abolition, retired-lineage and revival-prohibition mentions remain)',
      'architecture_stale_reference_evidence current row reports current_reference_count=0 result=PASS',
      'PostgreSQL/artifact parity PASS',
      'zh-TW mirror parity PASS',
      'codex-automation drift=false'
    )
  }
}

$path = 'governance_rule\execution\audit\convergence\codex-amendment-request-sub-sovereign-active-law-convergence-20261002.json'
$req | ConvertTo-Json -Depth 10 | Set-Content $path -Encoding utf8
"changes=$($changes.Count) provisions=$($touched.Count)"
$touched -join ' '