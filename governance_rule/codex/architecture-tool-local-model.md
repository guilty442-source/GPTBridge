# Local Model／星澄完整架構圖

```mermaid
flowchart TB
  ENTRY[Model Dialogue Entry] --> UI[Dialogue UI]
  UI --> SERVICE[Local Model Dialogue Service]
  SERVICE --> ROUTER[Model and Task Router]
  ROUTER --> OLLAMA[Local Ollama Runtime]
  ROUTER --> RAG[RAG Pipeline]
  RAG --> PG[(PostgreSQL Metadata and Chunks)]
  RAG --> QD[(Qdrant Semantic Candidates)]
  SERVICE --> XC[星澄 Workflow]
  XC --> PRIVATE[(星澄專屬狀態)]
  SERVICE --> INFO[Information Layer]
  SERVICE --> PROC[Independent Process Tree]
  PROC --> SUP[Supervisor and Watchdog]
  PROC --> FAULT[Isolated Failure Boundary]
```

模型核心與網路功能保持分離；RAG、模型輸出及候選資料均不得自行取得治理權威。

同步基線：A528、A537、A538、A540；獨立工具啟動與關閉各自上限 5 秒，逾時 fail-closed。

本地模型預設為關閉，不參與系統預設啟動。前端提供獨立啟動／關閉開關；啟動必須由使用者明確觸發或有效單項許可，關閉後禁止自動重啟。

星澄的神經網路核心為第一方原生實作：自有 tokenizer、自有 Transformer 權重、自有訓練引擎（語料、預訓練、監督微調、偏好訓練、checkpoint）與原生推論引擎（context、KV cache、sampling、量化）。原生模型必須能在不依賴外部模型產生答案的前提下獨立完成基本語言模型推理。

Ollama 僅為本地專家與教師模型，用以產生受治理的訓練資料與專家諮詢，不得取代星澄本身的神經網路核心。權重替換一律經明確流程與審計，不得自動生效。

原生推論引擎的正式啟用開關為工具自有設定 runtime/settings/native-engine.json（工具行程環境受 allowlist 限制，不倚賴環境變數）。啟用後所有生成改走自有權重並 fail-closed，不靜默回退第三方模型；每次原生推論寫入可驗證執行帳本，內含 checkpoint 雜湊、prompt 與輸出雜湊及時間戳，供事後稽核。

原生引擎具備受控面：可啟用／停用（enable/disable）、可指定 checkpoint、可設定生成預設（temperature、top_k、top_p、repetition_penalty、max_new_tokens、seed），並有兩道輸出護欄——品質護欄（過短、亂碼、高度重複時改回受控訊息）與範圍閘門（未命中允許主題清單時直接回覆受控訊息，不進行生成）。控制指令：`python -m xingcheng.infrastructure.native_engine --status|--enable|--disable|--set-checkpoint <path>`。

資源負擔受控：GPU 可用時一律使用 GPU 推論；CPU 路徑以 cpu_threads 限制 torch 執行緒（預設核心數 1/4、上限 4）並以 cpu_max_new_tokens 限制單次生成（預設 64），訓練 CLI 於 CPU 訓練時同樣限制執行緒（--cpu-threads，預設核心數一半、上限 8），避免與主系統爭用全部核心。

KV Cache 已完備：prefill、decode、cache update、position tracking、causal consistency、GQA KV layout 六項皆有測試鎖定；儲存精度支援 FP32／FP16／BF16 與 INT8（INT8 採 per-token、per-head 對稱量化，尺度逐 token 保存，絕不以單一尺度覆蓋序列）。量化僅降低記憶體，不得犧牲生成品質：greedy 生成必須與 FP 完全一致，長上下文困惑度差異需在容忍度內（實測 384 tokens：BF16 +0.007%、INT8 +0.025%，KV 記憶體 624KB→312KB→166KB）。

Triton 自研 kernel（RMSNorm／SwiGLU／RoPE）預設關閉（品質優先）：這些 kernel 不帶 autograd，需要梯度時一律退回 PyTorch 實作，否則訓練梯度會在正規化／激活／位置編碼處斷裂而不收斂；且目前即使在推論下，模型層級困惑度與延遲仍不及 PyTorch 參考實作，必須以 set_triton_kernels(True) 顯式開啟並完成逐層驗證後才可用於正式推論。GQA 下 RoPE kernel 的 launch 參數必須逐張量計算（k 的 head 數少於 q）。

人格以對話為唯一使用者入口：對話中輸入「設定人格：…」「把人格設為：…」即寫入受治理的 identity 儲存（版本歷程與稽核），「你的人格是什麼？」顯示現值，「清除人格」重設；人格由伺服器端於每次推論前以「星澄人格設定：／使用者最新訊息：」標記套用，用戶端不保存、不傳送人格，範圍閘門僅判定標記後的使用者訊息。對話視窗不提供人格編輯器。
