$ErrorActionPreference='Stop'
$src=Get-Content governance_rule/execution/audit/convergence/formal-state-closure-evaluator.ps1 -Raw -Encoding UTF8
Invoke-Expression $src.Substring($src.IndexOf('$ErrorActionPreference'),$src.IndexOf('$head=')-$src.IndexOf('$ErrorActionPreference'))
$head=Get-CodexRows revision_history | Select-Object -Last 1
$changes=[Collections.Generic.List[object]]::new()
function Change($table,$key,$field,$value) { $changes.Add(@{table=$table;key=$key;field=$field;proposed=$value}) }
$directory=@(Get-CodexRows project_architecture_directory)
Change module_assignment_registry @{module_architecture_code='SYSTEM_RESCUE'} status 'retired-no-current-dispatch'
$suiteEvidence=@(foreach($suite in (Get-CodexRows test_suite_architecture_directory | Where-Object suite_code -like 'TS_*')) {
 $module=$directory | Where-Object architecture_code -eq $suite.module_architecture_code
 if($module.retired_version) { $suite.status='retired';$suite.retired_version=$module.retired_version; Change test_suite_architecture_directory @{suite_code=$suite.suite_code} status $suite.status; Change test_suite_architecture_directory @{suite_code=$suite.suite_code} retired_version $suite.retired_version }
 else {
  $suite.management_owner='automation-sovereign'; Change test_suite_architecture_directory @{suite_code=$suite.suite_code} management_owner $suite.management_owner
  if($suite.module_architecture_code -in @('SHARED_LAYER','INVESTMENT_MOBILE','MODEL_DIALOGUE')) { $suite.suite_root=$module.physical_root+'\tests'; Change test_suite_architecture_directory @{suite_code=$suite.suite_code} suite_root $suite.suite_root }
 }
 $ordered=[ordered]@{}; foreach($p in ($suite.PSObject.Properties | Sort-Object Name)) {$ordered[$p.Name]=$p.Value}
 $payload=ConvertTo-Json -InputObject $ordered -Compress -Depth 10
 Change a233_normalized_directory_entry @{entry_code=$suite.suite_code} domain_payload $payload
 Change a233_normalized_directory_entry @{entry_code=$suite.suite_code} content_hash (Get-Digest $ordered)
 Change a233_normalized_directory_entry @{entry_code=$suite.suite_code} revision '<successor-version>'
 Change a233_normalized_directory_entry @{entry_code=$suite.suite_code} status $(if($suite.status -eq 'retired') {'retired'} else {'active'})
 Change a233_normalized_directory_entry @{entry_code=$suite.suite_code} retired_version $suite.retired_version
 @{suite_code=$suite.suite_code;status=$suite.status;suite_root=$suite.suite_root;root_present=(Test-Path -LiteralPath $suite.suite_root.Replace('\\','\') -PathType Container)}
})
$worktreeEvidence=@(foreach($wt in (Get-CodexRows git_worktree_registry)) {
 $gitHead=(& git -C $wt.physical_path rev-parse HEAD).Trim(); if($LASTEXITCODE -ne 0){throw 'GIT_OBSERVATION_FAILED'}
 $branch=(& git -C $wt.physical_path symbolic-ref HEAD).Trim(); if($LASTEXITCODE -ne 0){throw 'GIT_BRANCH_OBSERVATION_FAILED'}
 $observed=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
 Change git_worktree_registry @{worktree_identity=$wt.worktree_identity} head_revision $gitHead
 Change git_worktree_registry @{worktree_identity=$wt.worktree_identity} branch_ref $branch
 Change git_worktree_registry @{worktree_identity=$wt.worktree_identity} observed_at $observed
 @{identity=$wt.worktree_identity;head=$gitHead;branch=$branch;observed_at=$observed;meaning='point-in-time observation; not a live HEAD pointer'}
})
$tags=Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 10
$residents=Invoke-RestMethod http://127.0.0.1:11434/api/ps -TimeoutSec 10
$observed=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$modelEvidence=@(foreach($model in (Get-CodexRows local_model_registry)) {
 $present=$model.model_identity -in @($tags.models.name)
 Change local_model_registry @{model_identity=$model.model_identity} observed_at $observed
 Change local_model_registry @{model_identity=$model.model_identity} capability_state $(if($present){'inventory-present-capability-unverified'}else{'inventory-absent-capability-unverified'})
 Change local_model_registry @{model_identity=$model.model_identity} status 'INCOMPLETE_EVIDENCE'
 $resident=$model.model_identity -in @($residents.models.name)
 Change model_residency_registry @{model_identity=$model.model_identity} residency_state $(if($resident){'observed-resident-owner-generation-unverified'}else{'observed-not-resident'})
 Change model_residency_registry @{model_identity=$model.model_identity} evidence_reference ('governance_rule/execution/audit/convergence/registry-observation-evidence-20261002.json#'+$model.model_identity+';observed_at='+$observed)
 @{model_identity=$model.model_identity;inventory_present=$present;resident=$resident;observed_at=$observed;generation_binding='unverified; previous generation field retained as historical only';capability='not tested'}
})
$request=@{artifact='codex-amendment-request';authority='request-only';schema='codex-amendment-request/v1';request_id='codex-registry-observation-reconciliation-20261002';title='Reconcile retired registry projections and refresh bounded observations';summary='Data-only registry reconciliation; preserve missing test and model evidence; no implementation or business rules added to law';requested_by='decision-sovereign';origin='direct human-governor instruction';change_class='architecture-authority';required_review='five-sovereign-audit-unanimous-pass';flow='A382/A488-non-disruptive-amendment-flow';not_executed=$true;auto_execute=$true;predecessor=@{codex_version=$head.version;version_identity=('E2:'+$head.version);version_epoch=2;history_head=$head.entry_hash;revision_sequence=$head.sequence};changes=@($changes.ToArray())}
@{request=$request;evidence=@{authority='read-only-observation-not-implementation-acceptance';source_codex_version=$head.version;suites=$suiteEvidence;worktrees=$worktreeEvidence;models=$modelEvidence;model_inventory_count=@($tags.models).Count;resident_count=@($residents.models).Count;no_test_suite_created=$true;no_model_evaluation_executed=$true}} | ConvertTo-Json -Depth 30 -Compress
