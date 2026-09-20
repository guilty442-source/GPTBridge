Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Sort-Object WorkingSetSize -Descending |
  Select-Object -First 10 ProcessId,
    @{N='MemMB';E={[int]($_.WorkingSetSize/1MB)}},
    @{N='Cmd';E={ if ($_.CommandLine) { $_.CommandLine.Substring(0,[Math]::Min(120,$_.CommandLine.Length)) } else { '' } }} |
  Format-Table -AutoSize -Wrap
