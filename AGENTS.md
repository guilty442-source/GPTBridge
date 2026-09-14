# AGENTS.md — Project Guidelines for AI Workers

## Shell Environment

This project runs on **Windows PowerShell**. Bash-specific syntax does NOT work.

### Git Commits

**Do NOT use heredoc syntax** for commit messages. PowerShell does not support
`$(cat <<'EOF'...EOF)`.

**Correct method**: write the commit message to a temporary file, then use `-F`:

```powershell
# 1. Write message to a temp file
Set-Content -Path _commit_msg.txt -Value @"
Commit title here

Body text here.

Generated with [Devin](https://devin.ai)

Co-Authored-By: Devin <158243242+devin-ai-integration[bot]@users.noreply.github.com>
"@

# 2. Commit using -F
git commit -F _commit_msg.txt

# 3. Clean up
Remove-Item _commit_msg.txt -Force
```

**Alternative**: use a single-line message:

```powershell
git commit -m "Single-line commit message"
```

### Other PowerShell Notes

- `ls -la` → use `Get-ChildItem` or `dir`
- `rm -rf` → use `Remove-Item -Recurse -Force`
- `2>&1` works differently; pipe errors with `2>&1` at the end of the command
- `find` → use `Get-ChildItem -Recurse -Filter`
- `grep` → use `Select-String`

## Git Hooks

- **pre-commit**: runs `git diff --cached --check` (whitespace check) + audit log
- **pre-push**: blocks force-push / ref deletion / non-fast-forward unless `GOVERNANCE_AUTHORITY_APPROVAL=1`
- **pre-merge-commit**: same as pre-commit

Hooks are installed in `.git/hooks/` and shared across all worktrees.

## Worktrees

| Worktree | Path | Branch |
| --- | --- | --- |
| Main | `E:\GPTBridge` | `main` |
| Git | `E:\GPTBridge-worktrees\git` | `git` |
| Local Model | `E:\GPTBridge-worktrees\local-model` | `local-model` |
| RAG | `E:\GPTBridge-worktrees\rag` | `rag` |
| UI | `E:\GPTBridge-worktrees\ui` | `ui` |

Worktrees share the same `.git` directory. Hooks, config, and objects are common.

## Automatic Self-Commit (per worktree)

Each worktree can automatically commit the changes made inside its own checkout.
The service only commits — it **never pushes**.

```powershell
# One-shot (scheduler / on-demand), act on every worktree including main
& main-system\.venv\Scripts\python.exe scripts\git-auto-commit.py --all --once

# One-shot, single worktree
& main-system\.venv\Scripts\python.exe scripts\git-auto-commit.py --worktree E:\GPTBridge-worktrees\ui --once

# Long-running watcher for one worktree (interval + stability debounce in seconds)
& main-system\.venv\Scripts\python.exe scripts\git-auto-commit.py --worktree E:\GPTBridge-worktrees\ui --watch --interval 30 --debounce 60

# Spawn one background watcher per worktree (no console window)
& main-system\.venv\Scripts\python.exe scripts\git-auto-commit.py --all --watch
```

Guards: skipped while merge/rebase/cherry-pick/revert is in progress, when the
worktree is clean, and when git identity is missing. Honours `.gitignore`
(ignored paths are never staged). Commits are recorded in the audit ledger with
operation `auto-commit`. Implementation:
`governance_rule/execution/git_tiers/self_commit.py`.

## Automatic Worktree Synchronization

Commit each checkout, merge worker branches into `main`, audit the integrated
result, then fast-forward all clean worktrees. Conflicts stop the cycle. Only
the coordinator may push `main`; it never force-pushes, deletes refs, resets,
or chooses a conflict resolution.

```powershell
& main-system\.venv\Scripts\python.exe scripts\git-worktree-sync.py --root E:\GPTBridge
& main-system\.venv\Scripts\python.exe scripts\git-worktree-sync.py --root E:\GPTBridge --watch --interval 60 --no-commit --push
```

Use `--no-commit` when the per-worktree auto-commit watchers are active, so the
sync coordinator never competes with them for the Git index.

Only the synchronization coordinator may push. It pushes `main` only after all
worktrees are clean, governance audits pass, integration succeeds, and
`origin/main` is an ancestor of local `main`. Workers and self-commit watchers
must never push directly.

## Supervised Full-System Automation

The automation supervisor owns the entire parallel pipeline as one persistent,
self-restarting service: it spawns one self-commit watcher per registered
worktree, periodically runs the conflict-safe synchronizer, and records its
state in `.git/gptbridge-automation/`. Use the wrapper from any checkout:

```powershell
# status / start / stop
& main-system\.venv\Scripts\python.exe scripts\git-supervisor.py --root E:\GPTBridge --status
& main-system\.venv\Scripts\python.exe scripts\git-supervisor.py --root E:\GPTBridge --start
& main-system\.venv\Scripts\python.exe scripts\git-supervisor.py --root E:\GPTBridge --stop

# cross-reboot persistence (per-user Run key: no elevation needed)
& main-system\.venv\Scripts\python.exe scripts\git-supervisor.py --root E:\GPTBridge --install-logon
& main-system\.venv\Scripts\python.exe scripts\git-supervisor.py --root E:\GPTBridge --uninstall-logon

# optional: Task Scheduler logon job (requires an elevated shell)
& main-system\.venv\Scripts\python.exe scripts\git-supervisor.py --root E:\GPTBridge --install-task
& main-system\.venv\Scripts\python.exe scripts\git-supervisor.py --root E:\GPTBridge --uninstall-task
```

Design notes:

- Double-start is safe: a second supervisor exits when the lock is busy
  (`LockBusyError`), delete the lock file only to force yourself to replace a
  hung instance (`Stop-Process` its pid first).
- Watchers auto-commit their own worktree after a stability debounce; the
  supervisor never pushes unless `--push` was used at start time.
- If a sync cycle reports `error:dirty-worktree:<path>` it means a worktree
  (usually the main checkout while an external worker is mid-edit) is still
  uncommitted; the next cycle absorbs it once the tree settles and the
  governance audit passes. This is the intended parallel-update behaviour.
- Local-worker branches (`git`, `local-model`, `rag`, `ui` and the `kilo`
  worktrees) are merged into `main` by the coordinator; conflicts stop the
  cycle until a human resolves them.

Implementation: `governance_rule/execution/git_tiers/automation_supervisor.py`.

## Governance

- Codex files (`governance_rule/codex/*.py`) are **read-only** — do not modify without explicit user approval.
- Governance audit must pass before commits: `python -m governance_rule.execution.audit`
- Model core must remain separate from network functionality.
- All external network access must go through governed tool paths.

## Verification Commands

```powershell
# Python syntax check
python -c "import ast; ast.parse(open('file.py', encoding='utf-8').read())"

# Full test suite (bounded workers from pytest.ini keep local load low;
# cache is written to the managed global-cleaner temp directory)
main-system\.venv\Scripts\python.exe -m pytest -q

# Governance audit
python -m governance_rule.execution.audit
```

## Build Commands

- Python venv: `main-system\.venv\Scripts\python.exe`
- TypeScript: `npx tsc --noEmit` (in `main-system/`)
- Electron: `npm run build` (in `main-system/`)
