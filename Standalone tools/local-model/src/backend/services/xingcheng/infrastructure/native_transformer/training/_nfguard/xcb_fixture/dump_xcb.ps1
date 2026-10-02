param([string]$Path, [int]$Max = 3)
$d = [IO.File]::ReadAllBytes($Path)
$magic = [Text.Encoding]::ASCII.GetString($d, 0, 4)
$ver = [BitConverter]::ToUInt32($d, 4)
$mlen = [BitConverter]::ToUInt32($d, 8)
$meta = [Text.Encoding]::UTF8.GetString($d, 12, $mlen)
$cnt = [BitConverter]::ToUInt64($d, 12 + $mlen)
Write-Host "magic=$magic ver=$ver count=$cnt size=$($d.Length)"
Write-Host "meta=$meta"
$p = 12 + $mlen + 8
for ($i = 0; $i -lt $cnt -and $i -lt $Max; $i++) {
    $kind = $d[$p]; $flags = $d[$p + 1]
    $n = [BitConverter]::ToUInt32($d, $p + 4); $p += 8
    $ids = @(); for ($t = 0; $t -lt [Math]::Min($n, 8); $t++) { $ids += [BitConverter]::ToInt32($d, $p + 4 * $t) }
    $p += 4 * $n
    $nl = [BitConverter]::ToUInt32($d, $p); $p += 4 + 4 * $nl
    $extra = ""
    if ($kind -eq 3) {
        $nr = [BitConverter]::ToUInt32($d, $p); $p += 4 + 4 * $nr
        $nrl = [BitConverter]::ToUInt32($d, $p); $p += 4 + 4 * $nrl
        $extra = " rej=$nr/$nrl"
    }
    if (($flags -band 1) -ne 0) {
        $vp = [BitConverter]::ToUInt32($d, $p); $vd = [BitConverter]::ToUInt32($d, $p + 4)
        $p += 8 + 4 * $vp * $vd
        $extra += " vision=${vp}x${vd}"
    }
    Write-Host "rec$i kind=$kind flags=$flags n_ids=$n n_labels=$nl$extra ids_head=$($ids -join ',')"
}
Write-Host "consumed=$p"
