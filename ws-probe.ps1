param(
    [Parameter(Mandatory=$true)][int]$Port,
    [Parameter(Mandatory=$true)][string]$Token,
    [Parameter(Mandatory=$true)][string]$Instance,
    [Parameter(Mandatory=$true)][string[]]$Commands
)
$ws = [System.Net.WebSockets.ClientWebSocket]::new()
$uri = "ws://127.0.0.1:$Port/cmd?token=$Token&instance=$Instance"
$cts = [System.Threading.CancellationTokenSource]::new(8000)
$ws.ConnectAsync($uri, $cts.Token).Wait()
$buf = New-Object byte[] 65536
foreach ($cmd in $Commands) {
    $msg = @{ command = $cmd; payload = @{}; request_id = "probe-$cmd" } | ConvertTo-Json -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($msg)
    $ws.SendAsync($bytes, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $cts.Token).Wait()
    $ms = [System.IO.MemoryStream]::new()
    do {
        $res = $ws.ReceiveAsync($buf, $cts.Token).Result
        $ms.Write($buf, 0, $res.Count)
    } while (-not $res.EndOfMessage)
    $text = [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
    $ms.Dispose()
    Write-Output "=== $cmd ==="
    Write-Output $text
}
$ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "done", $cts.Token).Wait()
$ws.Dispose()
