# searchd — 星澄 Go 原生 metasearch 服務

取代 SearXNG 的 Go 原生 metasearch 引擎（go-service 層）：併發 fan-out
到內建免金鑰上游 adapter（Wikipedia opensearch / Bing RSS / DuckDuckGo
Lite），去重＋reciprocal-rank fusion 重排，經 `xingcheng-searchd/v1`
loopback 契約回傳 bounded metadata。

治理邊界不變：入口仍是受管 `xingcheng_web_search` 命令與
`web_search_log` 稽核；本服務只是 provider 後面的傳輸/聚合層。出站
目的地為編譯期 allowlist，服務不可用作任意 proxy。詳見 `CONTRACT.md`。

## Build

```powershell
$env:GOROOT='E:\GPTBridge\.tools\go\go'
$env:GOCACHE='E:\GPTBridge\.tools\gocache'
$env:GOPATH='E:\GPTBridge\.tools\gopath'
$env:Path="$env:GOROOT\bin;$env:Path"
cd 'Standalone tools\searchd-go'
go build -o bin\searchd.exe .\cmd\searchd
go test ./...
```

## Run

```powershell
.\bin\searchd.exe                     # 預設 127.0.0.1:8091
.\bin\searchd.exe -listen 127.0.0.1:8091 -upstream-timeout 8s
```

## Python 整合

`xingcheng/runtime/settings/web-search.json`：

```json
{
  "provider": "searchd",
  "searchd_url": "http://127.0.0.1:8091",
  "auto_start": true
}
```

- `provider`: `searchd`（本 repo 預設——SearXNG 已停用）或 `searxng`
  或 `auto`（searchd 優先，空/錯降級 searxng）。未提供 settings 檔時
  程式預設為 `auto`。
- `auto_start`: searchd 未運行時由 governed 命令惰性啟動
  `bin\searchd.exe`。
- env 覆寫：`XINGCHENG_SEARCH_PROVIDER`、`XINGCHENG_SEARCHD_URL`。
