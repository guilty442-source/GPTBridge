# Global Cleaner

Tool ID: `global-cleaner` · Version: `1.0.0`

The Global Cleaner operates only inside `E:\GPTBridge`. It cleans validated
garbage, owns centralized per-owner backups, stages governed repair extracts,
and performs read-only system health checks. Each owner (`main-system` or a
registered independent tool) retains at most one verified backup. A new backup
is published and verified before the prior generation is removed.

The cleaner contains no timer or scheduling preference. The main system sends
one governed daily maintenance request through the shared layer. The same UI
also provides a manual backup entry. Automatic repair never reads this storage
directly; System Rescue requests a narrow extraction through the governed
channel and receives non-applying staging evidence.

```powershell
python global-cleaner/src/main.py --status --json
python global-cleaner/src/main.py --cleanup-garbage --scope global --dry-run --json
python global-cleaner/src/main.py --system-check --json
python global-cleaner/src/main.py --list-managed-backups --json
```

## Test sandbox

Run repository tests through `src/test_runner.py`. Test caches and temporary
files are redirected into `global-cleaner/runtime/test-sandbox` and removed
immediately after the command finishes.
