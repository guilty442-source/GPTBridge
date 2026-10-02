// vaultly governed executor — native port of the 16-command
// ``vaultly_*`` WS contract registered in the codex command directory
// (vaultly-<x>-request-v1/result-v1) and consumed by
// ``gptbridge-egui``'s tool_vaultly surface.
//
// What is real here: owner-private operational state
// (``runtime/state/vaultly-state.json`` — account selection, filter
// terms, removed accounts, job ledger), the destination gate as a
// genuine filesystem probe (exists / writable / free bytes),
// diagnostic report export, and the job lifecycle transitions
// (cancel / retry / record creation with the documented validation:
// pre-existing destination, non-empty media_types,
// max_items_per_account clamped to ≤200).
//
// What stays fail-closed: the platform adapter seam
// (``vaultly_open_platform`` / ``vaultly_scan_following`` /
// ``vaultly_scan_posts``) — driving the dedicated Edge profile's IG/X
// sessions has no native implementation, so those commands answer with
// a typed VAULTLY_PLATFORM_ADAPTER_PENDING result instead of
// fabricating accounts or posts. Created jobs record
// ``adapter_state: platform-adapter-pending`` and can be cancelled or
// re-queued, but never pretend to run.

using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.App;

internal sealed class VaultlyExecutor
    : IGovernedCommandExecutor, IWsCommandSurface
{
    internal const string ToolId = "vaultly";
    private const int MaxItemsPerAccountCap = 200;

    private static readonly HashSet<string> OwnedCommands = new(
        StringComparer.Ordinal)
    {
        "vaultly_get_state",
        "vaultly_add_filter_terms",
        "vaultly_remove_filter_terms",
        "vaultly_save_selection",
        "vaultly_remove_accounts",
        "vaultly_restore_accounts",
        "vaultly_check_destination",
        "vaultly_export_report",
        "vaultly_create_job",
        "vaultly_create_link_job",
        "vaultly_cancel_job",
        "vaultly_retry_job",
        "vaultly_cancel_post_scan",
        "vaultly_scan_following",
        "vaultly_scan_posts",
        "vaultly_open_platform",
    };

    private static readonly JsonArray Platforms = new()
    {
        new JsonObject
        {
            ["id"] = "instagram",
            ["name"] = "Instagram",
            ["adapter_state"] = "platform-adapter-pending",
        },
        new JsonObject
        {
            ["id"] = "x",
            ["name"] = "X",
            ["adapter_state"] = "platform-adapter-pending",
        },
    };

    private readonly GovernedEnvironment _env;
    private readonly string _statePath;
    private readonly SemaphoreSlim _stateLock = new(1, 1);

    public VaultlyExecutor(GovernedEnvironment env)
    {
        _env = env;
        _statePath = Path.Combine(
            env.ToolRoot, "runtime", "state", "vaultly-state.json");
    }

    public bool OwnsCommand(string command) =>
        OwnedCommands.Contains(command);

    public Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
        => ExecuteWsAsync(command, payload, requestId, null,
            cancellationToken);

    public async Task<(string Event, JsonObject Result)> ExecuteWsAsync(
        string command, JsonObject payload, string requestId,
        Func<JsonObject, Task>? emitProgress,
        CancellationToken cancellationToken)
    {
        if (!OwnsCommand(command))
            throw new PermissionDeniedException();
        var result = command switch
        {
            "vaultly_get_state" => await WithState(
                state => Task.FromResult(StateSnapshot(payload, state)),
                cancellationToken).ConfigureAwait(false),
            "vaultly_check_destination" => CheckDestination(payload),
            "vaultly_save_selection" => await Mutate(
                (state, _) =>
                {
                    state["selected_account_ids"] =
                        payload["account_ids"]?.DeepClone()
                        ?? new JsonArray();
                    return Ok("追蹤名單已儲存");
                }, cancellationToken).ConfigureAwait(false),
            "vaultly_add_filter_terms" => await Mutate(
                (state, _) =>
                {
                    var terms = Terms(state);
                    foreach (var term in StringList(payload["terms"]))
                        if (!terms.Contains(term, StringComparer.Ordinal))
                            terms.Add(term);
                    StoreTerms(state, terms);
                    return Ok("篩選關鍵字已更新");
                }, cancellationToken).ConfigureAwait(false),
            "vaultly_remove_filter_terms" => await Mutate(
                (state, _) =>
                {
                    var terms = Terms(state);
                    foreach (var term in StringList(payload["terms"]))
                        terms.Remove(term);
                    StoreTerms(state, terms);
                    return Ok("篩選關鍵字已更新");
                }, cancellationToken).ConfigureAwait(false),
            "vaultly_remove_accounts" => await Mutate(
                (state, _) => MoveAccounts(state, payload, remove: true),
                cancellationToken).ConfigureAwait(false),
            "vaultly_restore_accounts" => await Mutate(
                (state, _) => MoveAccounts(state, payload, remove: false),
                cancellationToken).ConfigureAwait(false),
            "vaultly_export_report" => await WithState(
                (state, ct) => ExportReport(state, ct),
                cancellationToken).ConfigureAwait(false),
            "vaultly_create_job" => await Mutate(
                (state, _) => CreateJob(
                    state, payload, linkJob: false),
                cancellationToken).ConfigureAwait(false),
            "vaultly_create_link_job" => await Mutate(
                (state, _) => CreateJob(state, payload, linkJob: true),
                cancellationToken).ConfigureAwait(false),
            "vaultly_cancel_job" => await Mutate(
                (state, _) => TransitionJob(
                    state, payload, from: ["queued", "running"],
                    to: "cancelled", verb: "任務已取消"),
                cancellationToken).ConfigureAwait(false),
            "vaultly_retry_job" => await Mutate(
                (state, _) => TransitionJob(
                    state, payload,
                    from: ["completed", "failed", "cancelled"],
                    to: "queued", verb: "任務已重新排程"),
                cancellationToken).ConfigureAwait(false),
            "vaultly_cancel_post_scan" => await Mutate(
                (state, _) => TransitionPostScan(state, payload),
                cancellationToken).ConfigureAwait(false),
            "vaultly_scan_following" or "vaultly_scan_posts"
                or "vaultly_open_platform" => AdapterPending(command),
            _ => throw new PermissionDeniedException(),
        };
        return ($"{command}_result", result);
    }

    // ------------------------------------------------------------- state --

    private async Task<JsonObject> LoadState()
    {
        if (!File.Exists(_statePath))
            return EmptyState();
        try
        {
            return JsonNode.Parse(
                    await File.ReadAllTextAsync(_statePath)
                        .ConfigureAwait(false)) as JsonObject
                ?? EmptyState();
        }
        catch (JsonException)
        {
            // Corrupt owner-private state: fail closed to an empty
            // snapshot rather than trusting partial data.
            return EmptyState();
        }
    }

    private static JsonObject EmptyState() => new()
    {
        ["destination"] = "",
        ["selected_account_ids"] = new JsonArray(),
        ["filter_terms"] = new JsonArray(),
        ["removed_accounts"] = new JsonArray(),
        ["accounts"] = new JsonArray(),
        ["jobs"] = new JsonArray(),
        ["post_scan_jobs"] = new JsonArray(),
    };

    private async Task SaveState(JsonObject state)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_statePath)!);
        var tmp = _statePath + ".tmp-" + Guid.NewGuid().ToString("N");
        await File.WriteAllTextAsync(tmp, state.ToJsonString())
            .ConfigureAwait(false);
        File.Move(tmp, _statePath, overwrite: true);
    }

    private async Task<JsonObject> WithState(
        Func<JsonObject, Task<JsonObject>> read, CancellationToken ct)
        => await WithState((state, _) => read(state), ct)
            .ConfigureAwait(false);

    private async Task<JsonObject> WithState(
        Func<JsonObject, CancellationToken, Task<JsonObject>> read,
        CancellationToken ct)
    {
        await _stateLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            var state = await LoadState().ConfigureAwait(false);
            return await read(state, ct).ConfigureAwait(false);
        }
        finally
        {
            _stateLock.Release();
        }
    }

    private async Task<JsonObject> Mutate(
        Func<JsonObject, CancellationToken, Task<JsonObject>> mutate,
        CancellationToken ct)
    {
        await _stateLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            var state = await LoadState().ConfigureAwait(false);
            var result = await mutate(state, ct).ConfigureAwait(false);
            await SaveState(state).ConfigureAwait(false);
            return result;
        }
        finally
        {
            _stateLock.Release();
        }
    }

    private Task<JsonObject> Mutate(
        Func<JsonObject, CancellationToken, JsonObject> mutate,
        CancellationToken ct)
        => Mutate(
            (state, token) => Task.FromResult(mutate(state, token)), ct);

    // ----------------------------------------------------------- snapshot --

    private JsonObject StateSnapshot(JsonObject payload, JsonObject state)
    {
        var destination = state["destination"]?.GetValue<string>() ?? "";
        var accounts = state["accounts"] as JsonArray ?? new JsonArray();
        var selected = new HashSet<string>(
            StringList(state["selected_account_ids"]),
            StringComparer.Ordinal);
        var accountView = new JsonArray();
        foreach (var accountNode in accounts)
        {
            if (accountNode is not JsonObject account)
                continue;
            var view = (JsonObject)account.DeepClone();
            view["selected"] = selected.Contains(
                account["account_id"]?.GetValue<string>() ?? "");
            accountView.Add(view);
        }
        return new JsonObject
        {
            ["ok"] = true,
            ["version"] = "1.0.0",
            ["platforms"] = Platforms.DeepClone(),
            ["accounts"] = accountView,
            ["removed_accounts"] =
                state["removed_accounts"]?.DeepClone() ?? new JsonArray(),
            ["filter_terms"] =
                state["filter_terms"]?.DeepClone() ?? new JsonArray(),
            ["selected_account_ids"] =
                state["selected_account_ids"]?.DeepClone()
                ?? new JsonArray(),
            ["auto_scan"] = new JsonObject
            {
                ["enabled"] = false,
                ["adapter_state"] = "platform-adapter-pending",
            },
            ["jobs"] = state["jobs"]?.DeepClone() ?? new JsonArray(),
            ["post_scan_jobs"] =
                state["post_scan_jobs"]?.DeepClone() ?? new JsonArray(),
            ["download_automation"] = new JsonObject
            {
                ["state"] = "idle",
                ["adapter_state"] = "platform-adapter-pending",
            },
            ["posts"] = new JsonArray(),
            ["posts_total"] = 0,
            ["destination"] = destination,
            ["destination_health"] = destination.Length > 0
                ? ProbeDestination(destination)
                : null,
            ["diagnostics"] = new JsonObject
            {
                ["state"] = "executor-native",
                ["adapter_state"] = "platform-adapter-pending",
            },
            ["workspace_path"] = _env.ToolRoot,
            ["database_path"] = "",
            ["browser_profile_path"] = Path.Combine(
                _env.ToolRoot, "runtime", "browser-profiles",
                "vaultly", "shared"),
            ["safety_notice"] = "平台介面卡（Edge profile 工作階段）"
                + "尚未移植原生實作；掃描與下載命令維持 fail-closed。",
        };
    }

    // -------------------------------------------------------------- ops --

    private static List<string> Terms(JsonObject state)
    {
        if (state["filter_terms"] is JsonArray existing)
            return existing
                .Select(n => n?.GetValue<string>() ?? "")
                .Where(t => t.Length > 0)
                .ToList();
        return [];
    }

    private static void StoreTerms(JsonObject state, List<string> terms)
        => state["filter_terms"] = new JsonArray(
            terms.Select(t => (JsonNode)t).ToArray());

    private static List<string> StringList(JsonNode? node) =>
        node is JsonArray arr
            ? arr.Select(n => n?.GetValue<string>() ?? "")
                .Where(s => s.Length > 0).ToList()
            : [];

    private JsonObject MoveAccounts(
        JsonObject state, JsonObject payload, bool remove)
    {
        var ids = StringList(payload["account_ids"]);
        if (state["accounts"] is not JsonArray accounts)
        {
            accounts = new JsonArray();
            state["accounts"] = accounts;
        }
        if (state["removed_accounts"] is not JsonArray removed)
        {
            removed = new JsonArray();
            state["removed_accounts"] = removed;
        }
        var moved = 0;
        for (var i = accounts.Count - 1; i >= 0; i--)
        {
            if (accounts[i] is not JsonObject account)
                continue;
            var id = account["account_id"]?.GetValue<string>() ?? "";
            if (!ids.Contains(id))
                continue;
            if (remove)
            {
                accounts.RemoveAt(i);
                removed.Add(account);
                moved++;
            }
        }
        if (!remove)
        {
            for (var i = removed.Count - 1; i >= 0; i--)
            {
                if (removed[i] is not JsonObject account)
                    continue;
                var id = account["account_id"]?.GetValue<string>() ?? "";
                if (!ids.Contains(id))
                    continue;
                removed.RemoveAt(i);
                accounts.Add(account);
                moved++;
            }
        }
        var result = Ok(remove ? "帳號已移除" : "帳號已還原");
        result["moved"] = moved;
        return result;
    }

    private static JsonObject ProbeDestination(string rawPath)
    {
        var path = rawPath.Trim();
        var health = new JsonObject { ["path"] = path };
        try
        {
            var full = Path.GetFullPath(path);
            if (Path.IsPathRooted(full) == false
                || !Directory.Exists(full))
            {
                health["ok"] = false;
                health["message"] = "下載資料夾不存在";
                return health;
            }
            // Writability probe: create+delete a zero-byte marker so a
            // read-only mount can never pass the gate.
            var probe = Path.Combine(
                full, ".vaultly-probe-" + Guid.NewGuid().ToString("N"));
            File.WriteAllBytes(probe, []);
            File.Delete(probe);
            var drive = new DriveInfo(Path.GetPathRoot(full)!);
            health["ok"] = true;
            health["message"] = "下載資料夾可用";
            health["free_bytes"] = drive.AvailableFreeSpace;
            return health;
        }
        catch (Exception exc) when (exc is IOException
            or UnauthorizedAccessException or ArgumentException)
        {
            health["ok"] = false;
            health["message"] = "下載資料夾不可用";
            return health;
        }
    }

    private static JsonObject CheckDestination(JsonObject payload)
    {
        var path = payload["path"]?.GetValue<string>()?.Trim() ?? "";
        var health = path.Length == 0
            ? new JsonObject
            {
                ["ok"] = false,
                ["message"] = "尚未提供下載資料夾",
            }
            : ProbeDestination(path);
        return new JsonObject
        {
            ["ok"] = true,
            ["destination_health"] = health,
        };
    }

    private async Task<JsonObject> ExportReport(
        JsonObject state, CancellationToken ct)
    {
        var reportDir = Path.Combine(
            Path.GetDirectoryName(_statePath)!, "reports");
        Directory.CreateDirectory(reportDir);
        var reportPath = Path.Combine(
            reportDir,
            "vaultly-report-"
                + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss")
                + ".json");
        var report = new JsonObject
        {
            ["generated_utc"] = DateTime.UtcNow.ToString("O"),
            ["tool_id"] = ToolId,
            ["state"] = state.DeepClone(),
            ["executor"] = new JsonObject
            {
                ["state"] = "vaultly-native",
                ["pending_seams"] = new JsonArray(
                    "platform-adapter:edge-profile-session"),
            },
        };
        await File.WriteAllTextAsync(
                reportPath, report.ToJsonString(), ct)
            .ConfigureAwait(false);
        var result = Ok("診斷報告已匯出");
        result["report_path"] = reportPath;
        return result;
    }

    private JsonObject CreateJob(
        JsonObject state, JsonObject payload, bool linkJob)
    {
        var destination =
            payload["destination"]?.GetValue<string>()?.Trim() ?? "";
        // Contract gate: the destination folder must already exist and
        // be writable — vaultly never creates it implicitly.
        var health = destination.Length == 0
            ? new JsonObject
            {
                ["ok"] = false,
                ["message"] = "尚未提供下載資料夾",
            }
            : ProbeDestination(destination);
        if (health["ok"]?.GetValue<bool>() != true)
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "DESTINATION_INVALID",
                ["message"] =
                    health["message"]?.GetValue<string>()
                    ?? "下載資料夾不可用",
                ["destination_health"] = health,
            };
        var conditions = payload["conditions"] as JsonObject;
        var mediaTypes = StringList(conditions?["media_types"]);
        if (mediaTypes.Count == 0)
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "CONDITIONS_INVALID",
                ["message"] = "請至少選擇照片或影片",
            };
        var maxPerAccount = Math.Clamp(
            conditions?["max_items_per_account"]?.GetValue<int>()
                ?? MaxItemsPerAccountCap,
            0, MaxItemsPerAccountCap);
        var accountIds = StringList(payload["account_ids"]);
        var links = StringList(payload["links"]);
        if (!linkJob && accountIds.Count == 0)
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "SELECTION_EMPTY",
                ["message"] = "請至少勾選一個追蹤帳號",
            };
        if (linkJob && links.Count == 0)
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "LINKS_EMPTY",
                ["message"] = "未提供任何連結",
            };
        var previewOnly =
            payload["preview_only"]?.GetValue<bool>() == true;
        var job = new JsonObject
        {
            ["job_id"] = "job-" + Guid.NewGuid().ToString("N")[..12],
            ["kind"] = linkJob ? "link" : "download",
            ["status"] = "queued",
            ["adapter_state"] = "platform-adapter-pending",
            ["preview_only"] = previewOnly,
            ["created_utc"] = DateTime.UtcNow.ToString("O"),
            ["destination"] = destination,
            ["account_ids"] = new JsonArray(
                accountIds.Select(id => (JsonNode)id).ToArray()),
            ["links"] = new JsonArray(
                links.Select(l => (JsonNode)l).ToArray()),
            ["progress"] = 0,
            ["progress_current"] = 0,
            ["progress_total"] = 0,
            ["matched"] = 0,
            ["downloaded"] = 0,
            ["skipped"] = 0,
            ["failed"] = 0,
            ["automation_summary"] = new JsonObject
            {
                ["label"] = "等待平台介面卡",
                ["throughput_per_minute"] = 0.0,
                ["remaining"] = "",
            },
            ["message"] = "任務已排程；平台介面卡（Edge profile 工作階段）"
                + "尚未移植原生實作，待介面卡上線後才會執行",
        };
        if (state["jobs"] is not JsonArray jobs)
        {
            jobs = new JsonArray();
            state["jobs"] = jobs;
        }
        jobs.Add(job);
        var result = Ok(previewOnly ? "條件預覽任務已建立" : "下載任務已建立");
        result["job_id"] = job["job_id"]!.GetValue<string>();
        return result;
    }

    private static JsonObject TransitionJob(
        JsonObject state, JsonObject payload,
        string[] from, string to, string verb)
    {
        var jobId = payload["job_id"]?.GetValue<string>()?.Trim() ?? "";
        var job = (state["jobs"] as JsonArray ?? [])
            .OfType<JsonObject>()
            .FirstOrDefault(
                j => j["job_id"]?.GetValue<string>() == jobId);
        if (job is null)
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "JOB_NOT_FOUND",
                ["message"] = "找不到指定的任務",
            };
        var status = job["status"]?.GetValue<string>() ?? "";
        if (!from.Contains(status))
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "JOB_STATE_INVALID",
                ["message"] = $"任務狀態 {status} 不允許此操作",
            };
        job["status"] = to;
        job["updated_utc"] = DateTime.UtcNow.ToString("O");
        var result = Ok(verb);
        result["job_id"] = jobId;
        return result;
    }

    private static JsonObject TransitionPostScan(
        JsonObject state, JsonObject payload)
    {
        var scanJobId =
            payload["scan_job_id"]?.GetValue<string>()?.Trim() ?? "";
        var job = (state["post_scan_jobs"] as JsonArray ?? [])
            .OfType<JsonObject>()
            .FirstOrDefault(
                j => j["scan_job_id"]?.GetValue<string>() == scanJobId);
        if (job is null)
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "SCAN_JOB_NOT_FOUND",
                ["message"] = "找不到指定的掃描任務",
            };
        var status = job["status"]?.GetValue<string>() ?? "";
        if (status is not ("queued" or "running"))
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "SCAN_JOB_STATE_INVALID",
                ["message"] = $"掃描任務狀態 {status} 不允許取消",
            };
        job["status"] = "cancelled";
        job["updated_utc"] = DateTime.UtcNow.ToString("O");
        return Ok("掃描任務已取消");
    }

    private static JsonObject AdapterPending(string command) => new()
    {
        ["ok"] = false,
        ["error_code"] = "VAULTLY_PLATFORM_ADAPTER_PENDING",
        ["adapter_state"] = "platform-adapter-pending",
        ["message"] = "平台介面卡（Edge profile 工作階段驅動）"
            + "尚未移植原生實作；" + command + " 維持 fail-closed",
    };

    private static JsonObject Ok(string message) => new()
    {
        ["ok"] = true,
        ["message"] = message,
    };

    public JsonObject Health() => new()
    {
        ["executor_state"] = "vaultly",
        ["surface_size"] = OwnedCommands.Count,
        ["pending_seams"] = new JsonArray(
            "platform-adapter:edge-profile-session"),
    };
}
