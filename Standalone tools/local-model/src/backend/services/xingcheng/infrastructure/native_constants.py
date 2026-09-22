from __future__ import annotations

from typing import Any, Callable

InvestmentAnalyzer = Callable[[dict[str, Any]], dict[str, Any]]
MarketSearcher = Callable[[dict[str, Any]], dict[str, Any]]


class StarNativeConstantsMixin:
    """Class-level constants shared across all native-model mixins."""

    MODEL_ID = "star-native-language-model"
    VERSION = "1.0"
    ARCHITECTURE = (
        "star-tokenizer+intent-encoder+local-retrieval+tool-router+"
        "first-party-lexical-prototype-intent-classifier+"
        "source-attributed-long-context-reading+"
        "weighted-backoff-autoregressive-language-model+self-training"
    )

    _INTENTS = (
        (
            "capabilities",
            (
                "有哪些能力",
                "能力清單",
                "你會什麼",
                "可以做什麼",
                "能做什麼",
                "what can you do",
                "capabilities",
            ),
        ),
        (
            "self_upgrade",
            (
                "自我升級",
                "升級自己",
                "更新自己",
                "擴充自己",
                "升級星澄",
                "改善星澄",
                "修正星澄",
                "提升星澄",
                "檢討星澄",
                "self-upgrade",
                "improve yourself",
                "upgrade star",
            ),
        ),
        (
            "coding",
            (
                "程式碼",
                "寫程式",
                "程式設計",
                "編程",
                "編碼",
                "Python",
                "TypeScript",
                "JavaScript",
                "SQL",
                "重構",
                "函式",
                "修正問題",
                "修正錯誤",
                "修改程式",
                "建立功能",
                "新增功能",
                "實作功能",
                "檢查程式",
                "write code",
                "coding",
                "programming",
                "refactor",
                "function",
            ),
        ),
        (
            "visual",
            (
                "視覺辨識",
                "圖片辨識",
                "照片辨識",
                "影片辨識",
                "文件影像",
                "看圖",
                "圖像分類",
                "image recognition",
                "visual recognition",
                "video recognition",
                "document image",
            ),
        ),
        (
            "file_management",
            (
                "檔案管理",
                "整理檔案",
                "整理圖片",
                "整理影片",
                "檔案分類",
                "圖片分類",
                "產生標籤",
                "自動標籤",
                "file management",
                "file classification",
                "image tagging",
            ),
        ),
        (
            "reading",
            (
                "閱讀",
                "讀取",
                "讀完",
                "摘要",
                "總結",
                "重點",
                "大綱",
                "文件",
                "文章",
                "原文",
                "reading",
                "summarize",
                "summary",
                "document",
            ),
        ),
        (
            "statistics",
            ("統計", "平均", "中位數", "標準差", "變異", "相關性", "共變異", "statistics", "average", "median", "standard deviation"),
        ),
        (
            "data_organization",
            ("整理資料", "彙整資料", "資料清理", "分類資料", "排序資料", "organize data", "clean data", "sort data"),
        ),
        (
            "calculation",
            ("計算", "算出", "等於多少", "公式", "數學", "XIRR", "再平衡", "夏普", "Sortino", "calculate", "calculation", "formula"),
        ),
        ("reasoning", ("推理", "邏輯", "證明", "推導", "因果", "reasoning", "reason", "logic", "prove")),
        ("search", ("搜尋", "查詢", "找資料", "查資料", "search", "searching", "look up", "research")),
        ("distribution", ("配息", "股息", "收益分配", "除息", "dividend", "distribution")),
        ("quote", ("報價", "價格", "淨值", "行情", "quote", "price", "net asset value")),
        ("risk", ("風險", "波動", "回撤", "集中", "壓力", "情境", "risk", "volatility", "drawdown", "stress")),
        ("analysis", ("分析", "評估", "投資", "持股", "資產", "失敗", "錯誤訊息", "analyze", "analyse", "analysis", "portfolio", "holdings", "investment")),
        ("status", ("狀態", "健康", "資料庫", "版本", "模型", "status", "health", "version", "version info", "health check")),
        (
            "prediction",
            (
                "預測",
                "預估",
                "預期",
                "推估",
                "會漲",
                "會跌",
                "走勢研判",
                "後市",
                "forecast",
                "forecasting",
                "predict",
                "prediction",
                "outlook",
                "will rise",
                "will fall",
            ),
        ),
        (
            "recommendation",
            (
                "推薦",
                "建議買",
                "建議賣",
                "建議持有",
                "該買",
                "該賣",
                "值得買",
                "投資建議",
                "recommend",
                "recommendation",
                "suggest buying",
                "suggest selling",
                "which to buy",
                "what to buy",
            ),
        ),
        (
            "monitoring",
            (
                "盯盤",
                "盯股",
                "盤中留意",
                "持續觀察",
                "追蹤行情",
                "提醒新低",
                "提醒新高",
                "watchlist",
                "watch",
                "monitor price",
                "alert",
                "notify",
                "track",
            ),
        ),
        (
            "backtest",
            (
                "回測",
                "回测",
                "歷史測試",
                "策略回測",
                "backtest",
                "back-test",
                "historical simulation",
                "strategy simulation",
            ),
        ),
        (
            "news",
            (
                "新聞",
                "最新消息",
                "即時新聞",
                "財經新聞",
                "消息面",
                "最新動態",
                "headlines",
                "latest news",
                "breaking news",
                "business news",
                "news",
            ),
        ),

        ("prediction",
            ("预测",
            "预变",
            "预报",
            "预测结果",
            "特定时间点",
            "预测时间",
            "演算",
            "推演未来走势",
            "预测未来走势"),
            ("prediction",
            "forecast",
            "future trend",
            "projection"),
        ),
        ("recommendation",
            ("推荐后续走势",
            "推蓅后续",
            "推荐新走势"),
            ("recommendation",
            "reco",
            "next move"),
        ),
        ("monitoring",
            ("监控数据异常",
            "鼎監查异常",
            "监控编码"),
            ("monitoring",
            "monitor",
            "watch",
            "track"),
        ),
        ("backtest",
            ("回测结果",
            "歷史回溬",
            "回测动作"),
            ("backtest",
            "back-testing",
            "historical test"),
        ),
        ("news",
            ("新闻消息",
            "最新消息",
            "新闻摘要",
            "财经新闻",
            "新闻整点",
            "快讯",
            "财经快讯",
            "近日新闻",
            "新闻日报"),
            ("news",
            "latest news",
            "news digest",
            "financial news"),
        ),
        ("srl_role",
            ("语义角色",
            "语义角色标注",
            "施事称",
            "受事称",
            "工具角色",
            "语义角色分析",
            "多语义角色",
            "语义角色标识"),
            ("semantic role",
            "semantic role labeling",
            "SRL",
            "theta role"),
        ),
        ("dependency_parse",
            ("依存解析",
            "依存关系",
            "句法解析",
            "语义角色",
            "主谓宾",
            "句法依存",
            "依存树"),
            ("dependency parse",
            "dependency parsing",
            "dependency tree"),
        ),
        ("conversation_context",
            ("对话上下文",
            "上下文跟踪",
            "对话记忆",
            "上下文试图",
            "前後跟踪",
            "话霉跟踪",
            "方上下文",
            "话课跟踪",
            "多轮对话"),
            ("conversation context",
            "context tracking",
            "previous context"),
        ),
        ("sentiment_analysis",
            ("情感分析",
            "情绪分析",
            "情感值",
            "情绪值",
            "面向情感",
            "吸引力评估"),
            ("sentiment analysis",
            "sentiment",
            "tone"),
        ),
        ("inference_pipeline",
            ("推理管线",
            "推理流程",
            "推理链",
            "两步推理",
            "振挡推理",
            "口语推理"),
            ("inference pipeline",
            "reasoning pipeline",
            "inference chain"),
        ),
        ("lateral_bestseller",
            ("横向热销",
            "侧向热销",
            "旁路热销",
            "旁赗热销",
            "横向推荐",
            "導模热销",
            "爬行榜侧路",
            "横向拐磗",
            "導向热销"),
            ("lateral bestseller",
            "lateral pick",
            "cross-sell pick"),
        ),
    )
    _MARKET_ALIASES = {
        "台股": "TW",
        "臺股": "TW",
        "美股": "US",
        "港股": "HK",
        "日股": "JP",
        "陸股": "CN",
        "滬股": "CN",
        "深股": "CN",
        "A股": "CN",
        "滬深": "CN",
        "韓股": "KR",
        "新加坡股": "SG",
        "星股": "SG",
        "澳股": "AU",
        "印度股": "IN",
        "基金": "FUND",
    }
    _ACTION_ALIASES: dict[str, tuple[str, ...]] = {
        "query": ("查詢", "查一下", "看一下", "幫我查", "搜尋", "查找", "look up", "search"),
        "create": ("建立", "新增", "創建", "create", "add"),
        "generate": ("產生", "生成", "撰寫", "編寫", "寫一", "寫支", "寫段", "寫篇", "寫個", "寫出", "寫成", "generate"),
        "modify": ("修改", "變更", "調整", "更新", "modify", "update", "edit"),
        "delete": ("刪除", "刪掉", "移除", "砍掉", "清掉", "delete", "remove"),
        "move": ("搬移", "移動", "搬到", "移到", "move"),
        "copy": ("複製", "拷貝", "copy"),
        "rename": ("重新命名", "改名", "rename"),
        "classify": ("分類", "歸類", "整理", "classify", "organize"),
        "analyze": ("分析", "解析", "評估", "analyze", "analyse"),
        "compare": ("比較", "比對", "對照", "compare"),
        "execute": ("執行", "運行", "跑一下", "啟動", "execute", "run", "start"),
        "stop": ("停止", "終止", "關閉", "stop", "terminate", "shutdown"),
        "monitor": ("監控", "監看", "持續觀察", "monitor", "watch"),
        "read": ("讀取", "閱讀", "讀一下", "查看", "read", "view"),
        "write": ("寫入", "寫進", "存檔", "write", "save"),
        "export": ("匯出", "輸出", "導出", "export"),
        "import": ("匯入", "導入", "import"),
        "backup": ("備份", "backup"),
        "restore": ("還原", "復原", "restore"),
        "schedule": ("排程", "定期執行", "定時執行", "schedule", "cron"),
        "summarize": ("摘要", "總結", "summarize", "summary"),
        "translate": ("翻譯", "translate"),
        "open": ("開啟", "打開", "open"),
        "reset": ("重設", "重置", "reset"),
        "overwrite": ("覆蓋", "overwrite"),
        "format": ("格式化", "format"),
    }
    _ACTION_LABELS = {
        "query": "查詢",
        "create": "建立",
        "generate": "產生",
        "modify": "修改",
        "delete": "刪除",
        "move": "搬移",
        "copy": "複製",
        "rename": "重新命名",
        "classify": "分類",
        "analyze": "分析",
        "compare": "比較",
        "execute": "執行",
        "stop": "停止",
        "monitor": "監控",
        "read": "讀取",
        "write": "寫入",
        "export": "匯出",
        "import": "匯入",
        "backup": "備份",
        "restore": "還原",
        "schedule": "排程",
        "summarize": "摘要",
        "translate": "翻譯",
        "open": "開啟",
        "reset": "重設",
        "overwrite": "覆蓋",
        "format": "格式化",
    }
    _TAIWAN_TERM_ALIASES: dict[str, tuple[str, ...]] = {
        "資料夾": ("文件夾", "資料加", "資聊夾"),
        "設定": ("配置", "設訂"),
        "程序": ("進程", "程續"),
        "影片": ("視頻", "影篇"),
        "圖片": ("圖像", "圖篇"),
        "模型": ("模形",),
        "檔案": ("檔按",),
        "程式碼": ("程式馬",),
        "執行": ("執型", "运行"),
        "分類": ("分纇",),
        "監控": ("監空",),
        "刪除": ("删除",),
        "查詢": ("查询",),
    }
    _OBJECT_ALIASES: dict[str, tuple[str, ...]] = {
        "model": ("模型", "model", "ollama"),
        "file": ("檔案", "文件", "file"),
        "folder": ("資料夾", "目錄", "folder", "directory"),
        "image": ("圖片", "照片", "影像", "image", "photo"),
        "video": ("影片", "視訊", "video"),
        "code": ("程式碼", "原始碼", "函式", "腳本", "code", "source"),
        "text": ("文字", "文章", "內容", "text"),
        "database": ("資料庫", "database", "sqlite", "table"),
        "service": ("服務", "service", "daemon"),
        "process": ("程序", "進程", "process", "pid"),
        "config": ("設定檔", "配置檔", "設定", "config", "configuration"),
    }
    _REFERENCE_MARKERS = (
        "這個",
        "這些",
        "那個",
        "那些",
        "它",
        "它們",
        "舊的",
        "新的",
        "剛才",
        "剛剛",
        "上一個",
        "上一批",
        "照前面",
        "照剛才",
        "繼續",
        "確認執行",
        "確認刪除",
    )
    _DESTRUCTIVE_MARKERS = (
        "刪除",
        "刪掉",
        "移除",
        "砍掉",
        "清掉",
        "清空",
        "永久刪除",
        "強制刪除",
        "覆寫",
        "格式化",
        "重置",
        "drop table",
        "truncate table",
        "delete",
        "remove",
        "overwrite",
        "format",
        "reset",
    )
    _INTENT_EXAMPLES: dict[str, tuple[str, ...]] = {
        "conversation": (
            "你好，請簡短回覆",
            "確認對話是否正常並回覆指定文字",
            "針對最新訊息直接回答",
        ),
        "capabilities": (
            "列出目前可以使用的能力",
            "說明你能協助哪些工作",
            "你有哪些功能",
        ),
        "self_upgrade": ("改善自身模組", "提出系統更新方案", "讓星澄維護自己的程式"),
        "coding": ("建立資料接收端點", "實作一個服務模組", "檢查這段原始碼的問題"),
        "visual": ("辨識圖片中的內容", "整理影片畫面重點", "摘要文件掃描影像"),
        "file_management": ("依圖片內容自動分類檔案", "替影像產生標籤", "建議檔案資料夾"),
        "reading": ("找出兩份內容的共同觀點", "根據材料回答問題", "整理長篇報告的核心結論"),
        "statistics": ("描述這批樣本的分布", "求資料的離散程度", "比較兩組數據的關聯"),
        "data_organization": ("把紀錄依欄位分組", "清除重複列並排列", "將原始資料轉成表格"),
        "calculation": ("依公式求出結果", "算出投資組合報酬", "列出數值運算步驟"),
        "reasoning": ("根據前提判斷結論", "找出論述中的矛盾", "說明事件之間的因果"),
        "search": ("從公開來源取得最新資料", "幫我找到相關公告", "查證這項資訊的來源"),
        "distribution": ("確認這次收益何時發放", "是否有現金股利", "查核除息與入帳日期"),
        "quote": ("取得目前成交數值", "查基金最新淨值", "這項資產現在值多少"),
        "risk": ("檢查最壞情境與曝險", "評估可能損失", "找出組合過度集中的地方"),
        "analysis": ("評估持倉配置是否合理", "說明資產組合表現", "整合資料提出投資觀察"),
        "status": ("目前是否正常運作", "顯示系統健康資訊", "確認目前使用的版本"),
    }
    _RESULT_PIPELINE = (
        "star-tokenizer",
        "intent-encoder",
        "local-retrieval",
        "source-attributed-reading-router",
        "investment-tool-router",
        "autoregressive-probabilistic-decoder",
        "verified-self-training",
    )
    _EVIDENCE_POLICY = {
        "source_attribution_required": True,
        "unknown_values_preserved": True,
        "confidence_exposed": True,
    }
    _INTENT_GROUNDING: dict[str, str] = {
        "conversation": (
            "這是一般對話。請優先遵守使用者最新訊息的語言、格式與長度要求，"
            "直接回答，不要自行改成能力介紹，也不要加入未被要求的投資內容。"
        ),
        "self_upgrade": (
            "星澄已理解這是檢討、修正或自我維護命令，會立即執行模型維護、"
            "資料完整性與能力健康檢查。程式來源變更仍須通過範圍、語法、測試、"
            "多模型檢查、治理與可回復備份；治理規則永遠不可修改。"
        ),
        "coding": (
            "此工作已路由至星澄程式設計專家。程式碼會先建立結構化規格，"
            "再接受語法與範圍檢查；產生的內容不會在未授權時自動執行。"
        ),
        "visual": (
            "此工作只交給 MiniCPM-V 4.6 視覺檔案辨識專員，負責圖片、影片影格與"
            "文件影像的內容辨識、分類、標籤及摘要。模型只提供視覺分析結果，"
            "不直接搬移、刪除、覆寫檔案，也不參與其他任務。"
        ),
        "file_management": (
            "此工作屬於檔案管理。只有其中的視覺檔案辨識子任務可交給 MiniCPM-V 4.6；"
            "實際搬移、重新命名、刪除或覆寫仍由受治理執行層處理。"
        ),
        "reading": (
            "此工作已交給星澄閱讀理解模組。模組會分段閱讀提供的文字，"
            "保留文件雜湊、原文位置與引用；原文沒有答案時會明確拒絕補造。"
        ),
        "statistics": (
            "此工作已由主要日常模型委派給數理專家。統計結果會保留樣本數、"
            "集中趨勢與離散程度，且不使用網路資料。"
        ),
        "data_organization": (
            "此工作已由主要日常模型委派給數理專家。資料會依欄位、缺漏值與指定分組整理，"
            "原始值不會被猜測或補造。"
        ),
        "search": (
            "搜尋工作由星澄主要日常模型負責；公開來源採唯讀查詢。需要外部協作時，"
            "只由星澄經受治理 AI 通道提出申請並接收結果。"
        ),
        "calculation": (
            "此工作已路由至星澄數理專家。計算會保留輸入、公式、步驟與結果；"
            "缺少條件時會先標示未知量，不以猜測補值。"
        ),
        "reasoning": (
            "此工作已路由至星澄數理專家。推理會區分前提、推導步驟與結論，"
            "並檢查矛盾、隱含假設及證據是否足夠。"
        ),
        "distribution": (
            "星澄會依公開來源區分「無配息」與「資料不足」。已確認不配息的標的會顯示無配息，"
            "並使用每日負快取，避免持續重複查詢；需要時可由星澄直接查詢公開網站。"
        ),
        "quote": (
            "報價與基金淨值由星澄的公開來源搜尋模組取得，數值必須附來源與觀測時間；"
            "本地模型不猜測即時價格。"
        ),
        "risk": (
            "請從投資管家帶入持股後執行分析。星澄會計算集中度、幣別曝險、配息與風險參數，"
            "資料不足時會明確保留，不補造結論。"
        ),
        "analysis": (
            "請從投資管家帶入持股後執行分析。星澄會計算集中度、幣別曝險、配息與風險參數，"
            "資料不足時會明確保留，不補造結論。"
        ),
        "analysis_done": (
            "星澄已使用自己的投資參數引擎完成分析，共辨識 {count} 項風險提醒。"
            "結論來自輸入持股與星澄資料庫，不使用外部模型。"
        ),
        "status": (
            "星澄的本機生成服務已就緒；實際 Transformer、量化權重與安全回退狀態"
            "會由執行環境健康資訊如實揭露。"
            "目前保存 {instruments} 個標的識別、{observations} 筆市場觀測。"
        ),
        "_default": (
            "我是星澄，一個在本機執行並以自回歸方式生成文字的語言模型。"
            "目前提供投資資料搜尋、配息判定、參數化分析、資料品質檢查與受控自我訓練；"
            "可查詢公開網路來源，但不使用第三方生成模型，也不會猜測缺少的投資事實。"
        ),
    }

_RUNTIME_FEATURE_FLAGS: dict[str, bool] = {
    "ollama_outbound": True,
}
