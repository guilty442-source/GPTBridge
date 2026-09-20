# 恢復所有被 resource governor / 手動降權的 python 訓練相關進程
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'xingcheng|opencode|pretrain|self_learning' } |
  ForEach-Object {
    $proc = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
    if ($proc) {
      try {
        $proc.PriorityClass = 'Normal'
        $proc.ProcessorAffinity = 0xFFFF
        "restored pid=$($proc.Id) pri=$($proc.PriorityClass) aff=$($proc.ProcessorAffinity)"
      } catch {
        "failed pid=$($proc.Id): $($_.Exception.Message)"
      }
    }
  }
