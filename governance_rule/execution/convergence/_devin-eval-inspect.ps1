# worker: devin-desktop
$r = Get-Content 'main-system/runtime/temp/_devin-eval-report.json' -Raw -Encoding UTF8 | ConvertFrom-Json
'head: ' + $r.head.version + ' seq=' + $r.head.sequence
'formal: total=' + $r.formal.total + ' participating=' + $r.formal.participating + ' open=' + $r.formal.open_rule_count
$r.formal.states | ForEach-Object { '  state=' + $_.state + ' count=' + $_.count }
'findings: ' + ((@($r.formal.findings) | ForEach-Object { $_.rule_code + ':' + ($_.reasons -join '+') }) -join ' | ')
'schema: registered=' + $r.machine_schema.registered + ' open=' + $r.machine_schema.open
'obligations: ' + ($r.obligations.counts | ConvertTo-Json -Compress) + ' open=' + $r.obligations.open
'classification: unclassified=' + $r.classification.unclassified + ' counts=' + ($r.classification.counts | ConvertTo-Json -Compress)
'surface: active=' + $r.surface.active + ' unknown=' + $r.surface.unknown + ' layers=' + ($r.surface.layers | ConvertTo-Json -Compress)
'directory: ' + $r.directory.project_canonical + '/' + $r.directory.project_normalized + ' mismatches=' + @($r.directory.project_mismatches).Count + ' catalog_open=' + $r.directory.catalog_open
'dup exact_groups=' + $r.duplicate.exact_groups + ' ' + $r.duplicate.semantic_review
'search stale=' + $r.search.stale + '/' + $r.search.total
$r.epochs | ForEach-Object { 'epoch=' + $_.epoch + ' status=' + $_.status }
$r.external | ForEach-Object { 'gate=' + $_.gate_code + ' state=' + $_.current_state }
$r.closure | ForEach-Object { 'closure=' + $_.closure_id + ' result=' + $_.result + ' open=' + $_.open_finding_count + ' status=' + $_.status }
