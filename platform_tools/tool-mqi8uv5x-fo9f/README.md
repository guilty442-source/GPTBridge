# 投資管家

Project ID: `tool-mqi8uv5x-fo9f`

功能：
- 匯入 CSV、TSV、JSON 庫存檔建立持股清單。
- 多來源報價鏈：TWSE、Yahoo Chart、Yahoo Quote、CoinGecko、Alpha Vantage。
- 監控模式會依市場交易時間輪詢，收盤後自動停止持續更新。

CSV 欄位可用中英文：
- `symbol` / `代號`
- `name` / `名稱`
- `market` / `市場`
- `quantity` / `股數` / `庫存`
- `average_cost` / `成本` / `平均成本`
- `currency` / `幣別`

CLI 範例：

```powershell
python platform_tools/tool-mqi8uv5x-fo9f/src/main.py --portfolio holdings.csv --json
python platform_tools/tool-mqi8uv5x-fo9f/src/main.py --portfolio holdings.csv --watch --interval 60 --json --progress-jsonl
```

注意：報價僅供監控，可能延遲；本工具不提供投資建議。
