# GPTBridge Git 架構圖

```mermaid
flowchart LR
  O[(Shared Git Object Store)]
  MAIN[main worktree] --- O
  WG[git worktree] --- O
  WL[local-model worktree] --- O
  WR[rag worktree] --- O
  WU[ui worktree] --- O
  WG --> SWEEP[GitAutomationService commit sweep]
  WL --> SWEEP
  WR --> SWEEP
  WU --> SWEEP
  MAIN --> SWEEP
  SWEEP --> GUARD{staged index present?}
  GUARD -->|yes| SKIP[skip: human/agent mid-commit]
  GUARD -->|no| COMMIT[Self-commit（never push）]
  COMMIT --> SYNC[Sync cycle 300s]
  MAIN --> SYNC
  SYNC --> AUDIT[Governance Audit]
  AUDIT --> MQ[Merge queue]
  MQ -->|conflict| STOP[Stop cycle until human resolves]
  MQ -->|clean| MERGE[Merge to main]
  MERGE --> FF[Fast-forward clean worktrees]
  MERGE --> PUSH[Coordinator-only push]
```

提交與同步由單一行程內任務 `GitAutomationService` 承接（60 秒提交掃描＋60 秒指紋穩定去抖；300 秒同步循環），取代舊 supervisor 程序艦隊；工作區索引已有他人 staged 變更時跳過（`staged-index-present`），人類或代理提交中絕不被掃入。自我提交永不推送，只有同步協調者可推 `main`；提交一律路徑限定（path-scoped）以免夾帶外部已暫存檔案。衝突停止同步；禁止自動 force-push、刪除 ref 或替人決定衝突解法。

同步基線：A528、A537、A538、A53／E39、A46；啟動 10 秒、強制測試套件 20 秒、獨立審計流程 30 秒，逾時 fail-closed。
