# Local Model／本地模型獨立工具完整架構圖

```mermaid
flowchart TB
  ENTRY[Governed Model Request] --> RUST[Rust Lifecycle and Resource Control]
  RUST --> CPP[C++23 Native Inference]
  CPP --> MODEL[Registered Local Model]
  MODEL --> DATA[(Local Model Tool Data Scope)]
  XCSVC[星澄 Independent Service] --> CONTRACT[Typed Service Contract]
  CONTRACT --> ENTRY
  RETIRED[Python / NumPy / JAX] --> DENY[Retired: no execution, dependency, artifact or fallback]
  RETRIEVE[Rust DAG CAG RAG] --> PG[(PostgreSQL Canonical Knowledge)]
  RETRIEVE --> VD[vectord-rs Derived Index]
  RETRIEVE --> CPP
```

`local-model` 是獨立工具，預設不啟動，只能由使用者明確開關或有效單項許可啟動。它承載自身登錄的本地模型能力，不擁有、承載或控制星澄。Rust 負責工具生命週期、設定、資源協調、CLI 與 IPC；C++23 承載其登錄的模型推論、模型載入、KV Cache、sampling 與 kernels。Python、NumPy 與 JAX／XLA 已全面退役並立即生效：不得執行、相依、產生產物、控制正式模型能力或作任何回退。

星澄是與 `local-model` 分離的獨立本地原生模型服務，但不是獨立工具且不建立工具卡片。自我學習、自動編程、自動修復及自我升級是星澄服務內部能力。修復方案必須具證據、範圍、風險、回復與審計；無法確定安全時停止，不得硬修復、直接覆蓋或重置。星澄的身分、人格、記憶、對話、訓練、權重、修復知識與 runtime 記錄只能存在星澄專屬資料域；其他元件只能取得最小型別化結果或不透明參照。

星澄融合 Transformer Decoder 架構：與同時期採用 Encoder 的 BERT 不同，GPT 系列完全基於 Transformer 的解碼器（Decoder-only）堆疊而成，星澄自訓權重循同一路線——單向因果注意力（causal mask）逐 token 自回歸生成，無 encoder、無 encoder-decoder cross-attention。「融合」指混合式 Decoder 堆疊：gated linear attention（deltanet）與週期性全注意力層交錯（`full_attention_interval`），搭配 RoPE（partial rotary）、QK-norm、注意力輸出閘控、SwiGLU FFN 與含共享專家的 MoE；checkpoint 契約為 XCN4（XCN3 加 vision early-fusion 區塊）。

星澄原生多模態採「早期融合」（early fusion）：`use_vision` 啟用時，影像 patch 經線性投影 `vision.patch_proj`（hidden × patch_dim）送入與文字同一條 Decoder 主流，patch 列作為因果序列前綴、與 token embedding 共用位置與注意力；prefix cache 不承接 vision span，文本專用路徑位元不變。推論探針 `forward_vision_logits` 與 `xc_modeltool vision-smoke` 提供端到端驗證；訓練端 `vision_patches` 資料列以 -100 標籤遮蔽 patch 前綴，DPO 拒絕 vision 輸入，全部 fail-closed。

Ollama 只作登錄的本地教師或專家，不取代星澄。視窗關閉須在 5 秒內停止 `local-model` 自身後端及其擁有的模型程序，但不得停止獨立的星澄服務。
