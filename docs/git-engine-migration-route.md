# Git 引擎退役遷移路線（外部 Git 工具全退役）

> **現行文件**。指令：使用者 2026-10-03「外部 Git 工具 = 全退役；
> 零 shell Git、零第三方 Git library」。
> 治理依據：修正案 `codex-amendment-request-git-engine-native-20261003-r1`
> （待五主權一致通過）——註冊 `git-engine-native` 組件
> （C++23 執行＋C 穩定 ABI，`native/git_engine` +
> `native/include/git_engine.h`）並宣告 M0–M4 成熟階梯；
> M3 前 C# `GPTBridge.GitAutomation` host 為唯一 committer，
> 引擎全程 read-only shadow；M4 引擎為唯一擁有者、C# host 退居相容層。

**盤點日期**：2026-10-03

---

## 一、消費者盤點結論

**全專案的 git 子行程集中在單一收口**：
`shared-layer/csharp/GPTBridge.GitAutomation/Git.cs`
（`Git.Run`/`Git.Exec` → `ProcessStartInfo` → 解析 git.exe）。
14 個檔案 ~35 個呼叫點全部經此邊界，無任何繞道者。

| 層面 | 現況 |
| --- | --- |
| 第三方 Git library（libgit2/LibGit2Sharp/go-git/git2-rs） | **零**——無需退役 |
| C# 直接 `ProcessStartInfo("git*")` | 僅 `Git.cs` 內部（`ResolveExecutable` 已做原生 git.exe 定位＋hook lane 排除） |
| Rust/Go/C++ spawn git | 零（`native/git_engine` 自身除外，其為目標引擎） |
| shell 腳本呼叫 git（.ps1/.bat/.vbs） | 零 |
| `.git/hooks/*` sh wrapper | git 內部機制：hook 僅 `exec GitAutomation.exe --hook`；git.exe 退役後由引擎接管 hook 語義（M4 課題） |
| `Git.Exec`（通用 bounded subprocess） | 兼跑 dotnet/cargo 等非 git 二進位——退役範圍僅 git 類呼叫 |
| `AuditGate` `cmd /c build.bat` | 審計引擎編譯步驟，非 git——不屬本退役面 |

### Git.Run 動詞清單（現行消費）

- **唯讀**：`rev-parse`、`diff (--cached/--check/--name-only/--numstat/--quiet)`、
  `ls-files --others`、`rev-list`、`merge-base/--is-ancestor`、`config --get`、
  `status`、`log`、`branch`、`worktree list`
- **寫入**：`add`、`commit`、`merge`、`fetch`、`push`、`worktree` 操作、
  `update-ref`（治理清單內的高危動詞另有禁制表，`Governance.cs` 已分類）

## 二、目標引擎現況（`native/git_engine`）

`git_engine.h` 已宣告並實作（工作區進行中）：

| ABI verb | milestone | 語義 | 狀態 |
| --- | --- | --- | --- |
| `ge_probe_worktree` | M0 | porcelain 狀態探針（bounded、不取 index lock——`GIT_OPTIONAL_LOCKS=0`） | landed `ce415435b` |
| `ge_diff_summary` / `ge_log_latest` | M1 | numstat 摘要 / tip 歷史探針 | 實作中（工作區） |
| `ge_sweep_plan` | M2 | sweep 影子決策（verify-only，不 stage/commit） | 實作中（opencode 認領） |
| `ge_sync_plan` | M3 | ahead/behind＋`merge-tree` 唯讀衝突預檢 | 實作中（工作區） |

內部方法：M0–M3 皆為「governed git subprocess」——
`CreateProcessW` 直啟 git.exe（無 shell、bounded output、逾時殺停）。
**這是修正案認可的過渡方法**；使用者指令的終態要求（M4 起）
是引擎內部亦不再依賴 git.exe——見 §四。

## 三、階梯對應的消費者遷移

### M1 — 讀面 parity（引擎動詞已備）
- C# host 對 `Git.Run` 的唯讀呼叫可逐類以 ABI 取代或並行驗證：
  `diff --cached --check`、`diff --name-only`、`ls-files --others`、
  `rev-parse HEAD/--abbrev-ref`、`rev-list`、`merge-base`。
- 所需前置：**C# 綁定**（P/Invoke over `git_engine.h`，比照
  `xstore_sql`/`NativeCodex` 模式——project-owned binding，非第三方庫）
  ＋引擎 DLL 產物（`native/git_engine` build 目前只產測試 exe，
  DLL 產線屬該目錄，須與認領者協調）。

### M2 — sweep 影子（opencode 進行中）
- `ge_sweep_plan` 對拍 `SelfCommit.RunOnce` 的 guard 決策
  （staged-index-present / merge-state / not-repo / clean）。

### M3 — sync 影子＋切換修正案
- `ge_sync_plan` 對拍 `Sync.cs` 的 ahead/behind＋merge pre-check；
- 通過後提切換修正案：唯讀面改由引擎供給。

### M4 — 引擎唯一擁有者（終態：零 git.exe）
- 需新增**寫入動詞**（目前 ABI 無 mutating verb）：
  `add`（index 寫）、`commit`（tree/commit object＋ref update）、
  `merge`/ff、`fetch`/`push`（網路協定——最大塊）、worktree 管理、
  `update-ref`。
- 兩條路線（治理裁決點，文件 §五 Q1）：
  a. **原生 plumbing**：C++ 直接讀寫 `.git`（index/objects/refs/pack）——
     符合「零 shell Git」終態但工程量最大，push/fetch 需協定實作；
  b. **受管子行程保留於引擎內**：git.exe 僅存在於引擎邊界內——
     不滿足「全退役」字面，僅作過渡。
- hook 語義：git.exe 移除後 pre-commit/pre-push 由引擎自行執行
  （`Hooks.Commit`/`Hooks.Push` 已是 C# 實作，可內嵌或經 ABI 呼叫）。

## 四、指令符合度評估

- 「零第三方 Git library」：**已達成**（無任何第三方庫，且引擎為
  project-owned C++23/C）。
- 「零 shell Git」：**外部消費者面已達成**——除引擎外全專案僅
  `Git.cs` 一處 ProcessStartInfo（直啟非 shell）；引擎 M0–M3 的
  `CreateProcessW` 同為直啟非 shell。**剩餘問題是 git.exe 二進位本身**，
  屬 M4 原生 plumbing 課題。
- 「外部 Git 工具全退役」：依階梯推進中；
  M3 切換修正案前不得抽換 `Git.Run` 實作（C# host 仍為唯一 committer，
  抽換會造成並行權威）。

## 五、開放問題

0. **認領/ABI 重疊警示（2026-10-03 觀測）**：兩個活躍認領同時涵蓋
   `native/git_engine/`——`opencode-git-engine-native`（M2 sweep
   shadow，延伸 `git_engine.h` ABI）與 `devin-git-native-engine`
   （720min TTL，範圍含 `native/git_engine/`、`native/include/xgit.h`、
   `Git.cs`、`NativeGit.cs`、build.ps1）。`xgit.h` 目前不存在，
   若落地將形成第二條 ABI——修正案只註冊單一 `git-engine-native`
   組件單一 C ABI，並行 ABI 會構成 authority drift，需協調收斂為一；
1. **M4 寫面路線**：原生 plumbing vs 受管子行程過渡期長度——
   需治理裁決（push/fetch 協定是最重成本項）；
2. 引擎 DLL 產物與 C# 綁定專案的落點（`native/git_engine` 目錄
   目前有 opencode 活躍認領，綁定屬協調後工作）；
3. `Git.Exec` 通用 subprocess 的非 git 用途（dotnet/cargo 編譯呼叫）
   與本退役無涉，保留但建議改名/分層避免誤判；
4. hook wrapper（sh）在 M4 的替代——引擎自執行 hook 語義；
5. `ResolveGitDirs` 已做 `.git` 檔案系統解析（worktree/commondir），
   為 M4 原生 plumbing 的既有基礎。

## 六、驗證要求（每 milestone）

- 引擎動詞 vs `Git.Run` 同輸入同輸出 parity（影子期強制）；
- `GIT_OPTIONAL_LOCKS=0` 類護欄維持（引擎不取 watcher 持有的鎖）；
- bounded output/逾時殺停契約不可回歸；
- 寫入動詞上線前須有對拍證據＋切換修正案；
- 全程 C# host 唯一 committer 直到 M3 修正案執行。
