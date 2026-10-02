# 星澄工具與知識取得層 (XingCheng Tools & Knowledge Acquisition)

星澄的受管網路介面卡 — 與模型核心嚴格分離。

## 現況範圍

本套件僅保留治理稽核釘定的網路介面卡宣告（`NETWORK_DESTINATION_ALLOWLIST`
邊界證據），不內含搜尋管線：

- `search/searxng.py` — SearXNG provider（loopback-only allowlist：
  `127.0.0.1` / `localhost` / `::1`）
- `search/provider.py`、`search/types.py` — provider 介面與請求/結果型別
- `fetch/safety.py` — URL 安全檢查（SSRF / 私網阻擋）

外部故障排除研究實際走的是 `ai-collaboration` 受管通道
（`xingcheng/integration/external_research.py` → 嵌入式瀏覽器 agent），
不經本套件；本地 SearXNG 部署後若要啟用直接搜尋，需另行經治理註冊
受管指令。

歷史管線（Intent Router / Tool Router / Fetch / Parser / Rerank /
Context Builder）已依「無消費者即刪除」政策移除，可自 git 歷史還原。
