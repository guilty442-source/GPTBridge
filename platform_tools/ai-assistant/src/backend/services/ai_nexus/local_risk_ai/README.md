# 本地輔助AI

此資料夾是 AI投資管家的本地模式風險引擎。它可以由應用程式服務層呼叫，也可以單獨用 Python 執行。

## 單獨執行

```powershell
$env:PYTHONPATH = "platform_tools/ai-assistant/src/backend/services"
.venv\Scripts\python.exe -m ai_nexus.local_risk_ai --portfolio path\to\portfolio.xlsx
```

離線模式不抓報價，只檢查持倉資料、成本與集中度：

```powershell
$env:PYTHONPATH = "platform_tools/ai-assistant/src/backend/services"
.venv\Scripts\python.exe -m ai_nexus.local_risk_ai --portfolio path\to\portfolio.xlsx --offline
```

也可以直接下本地命令：

```powershell
.venv\Scripts\python.exe -m ai_nexus.local_risk_ai --portfolio path\to\portfolio.xlsx --instruction "只看 AAPL 跌破成本 3% 集中度 30%"
```

此模組不呼叫外部 LLM；即時模式只使用既有報價 provider。
