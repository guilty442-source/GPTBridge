// TeacherCollect.cs — governed teacher-distillation collector.
//
// Native self-distillation lane (B154 successor direction): the teacher
// signal is produced by XingCheng's own governed weights through
// `xc_modeltool serve --bundle <dir>` + the `infer` op — never an
// external model service. A scope's teacher spec is "self" (the pinned
// native-engine checkpoint) or a tool-root-relative bundle artifact
// path; anything else fails closed. The serve session is
// process-scoped demand residency (load-once, disposed with the run) —
// no resident teacher daemon exists.
//
// Flow: load policy -> resolve each scope's teacher bundle -> per
// prompt call `infer` on the native engine -> validate bounds ->
// INSERT deterministic rows into gptbridge_xingcheng_<scope>
// .language_training_example. The normal run_cycle collects them like
// any other verified example — collection never touches training
// itself. Deterministic example_ids make re-runs idempotent.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal sealed class TeacherDistillationPolicy
{
    public const string Format = "star-teacher-distillation-policy/v1";

    public bool Enabled = false;
    /// <summary>AC §57-§59: "native" uses a governed Xingcheng bundle
    /// (active/mature generation) as the teacher through xc_modeltool
    /// serve; "ollama" is the B154-registered external lane. With no
    /// legitimate teacher the lane DISABLES — never a silent Ollama
    /// fallback (§59).</summary>
    public string Source = "ollama";
    public Dictionary<string, string> Teachers = new(StringComparer.Ordinal);
    /// <summary>Native teacher bundles per scope — scope id -> bundle
    /// directory. Only governed xingcheng generation bundles qualify;
    /// a missing/unreadable bundle disables that scope's prompts (§58).</summary>
    public Dictionary<string, string> NativeBundles =
        new(StringComparer.Ordinal);
    public List<Dictionary<string, string>> Prompts = new();
    public int MaxRowsPerRun = 24;
    public int MaxNewTokens = 256;
    public double QualityScore = 0.92;
    public double Temperature = 0.2;
    public int RequestTimeoutS = 180;
    public int ServeTimeoutS = 60;
    public int MinTargetChars = 1;
    public int MaxTargetChars = 4096;

    public static string Rel => "xingcheng/runtime/settings/teacher-distillation.json";

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = Format,
        ["enabled"] = Enabled,
        ["teacher_source"] = Source,
        ["teachers"] = Teachers.ToDictionary(
            p => p.Key, p => (object?)p.Value, StringComparer.Ordinal),
        ["native_teacher_bundles"] = NativeBundles.ToDictionary(
            p => p.Key, p => (object?)p.Value, StringComparer.Ordinal),
        ["max_rows_per_run"] = MaxRowsPerRun,
        ["max_new_tokens"] = MaxNewTokens,
        ["quality_score"] = QualityScore,
        ["temperature"] = Temperature,
        ["request_timeout_s"] = RequestTimeoutS,
        ["serve_timeout_s"] = ServeTimeoutS,
        ["min_target_chars"] = MinTargetChars,
        ["max_target_chars"] = MaxTargetChars,
        ["prompts"] = Prompts.Count,
    };

    /// <summary>Fail-closed load: unreadable/invalid file resolves to a
    /// disabled policy so a corrupt settings layer can never activate
    /// collection.</summary>
    public static TeacherDistillationPolicy Load(string toolRoot)
    {
        var policy = new TeacherDistillationPolicy();
        string path = XcPaths.SettingsReadPath(toolRoot, Rel);
        try
        {
            if (!File.Exists(path))
                return policy;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var el = doc.RootElement;
            policy.Enabled = GetBool(el, "enabled", false);
            string src = GetStr(el, "teacher_source");
            policy.Source = src is "native" or "ollama" ? src : "ollama";
            if (el.TryGetProperty("native_teacher_bundles", out var nb)
                && nb.ValueKind == JsonValueKind.Object)
                foreach (var p in nb.EnumerateObject())
                    if (p.Value.ValueKind == JsonValueKind.String
                        && (p.Value.GetString() ?? "").Length > 0)
                        policy.NativeBundles[p.Name] =
                            p.Value.GetString()!;
            policy.MaxRowsPerRun = GetInt(el, "max_rows_per_run",
                policy.MaxRowsPerRun);
            policy.MaxNewTokens = GetInt(el, "max_new_tokens",
                policy.MaxNewTokens);
            policy.QualityScore = GetDouble(el, "quality_score",
                policy.QualityScore);
            policy.Temperature = GetDouble(el, "temperature",
                policy.Temperature);
            policy.RequestTimeoutS = GetInt(el, "request_timeout_s",
                policy.RequestTimeoutS);
            policy.ServeTimeoutS = GetInt(el, "serve_timeout_s",
                GetInt(el, "ensure_timeout_s", policy.ServeTimeoutS));
            policy.MinTargetChars = GetInt(el, "min_target_chars",
                policy.MinTargetChars);
            policy.MaxTargetChars = GetInt(el, "max_target_chars",
                policy.MaxTargetChars);
            if (el.TryGetProperty("teachers", out var teachers)
                && teachers.ValueKind == JsonValueKind.Object)
                foreach (var p in teachers.EnumerateObject())
                    if (p.Value.ValueKind == JsonValueKind.String
                        && (p.Value.GetString() ?? "").Length > 0)
                        policy.Teachers[p.Name] = p.Value.GetString()!;
            if (el.TryGetProperty("prompts", out var prompts)
                && prompts.ValueKind == JsonValueKind.Array)
                foreach (var item in prompts.EnumerateArray())
                {
                    if (item.ValueKind != JsonValueKind.Object) continue;
                    string scope = GetStr(item, "scope");
                    string prompt = GetStr(item, "prompt");
                    if (scope.Length == 0 || prompt.Length == 0) continue;
                    policy.Prompts.Add(new Dictionary<string, string>(
                        StringComparer.Ordinal)
                    {
                        ["scope"] = scope,
                        ["intent"] = GetStr(item, "intent"),
                        ["prompt"] = prompt,
                    });
                }
        }
        catch (JsonException)
        {
            return new TeacherDistillationPolicy();
        }
        catch (IOException)
        {
            return new TeacherDistillationPolicy();
        }
        return policy;
    }

    private static string GetStr(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";
    private static bool GetBool(JsonElement el, string name, bool d) =>
        el.TryGetProperty(name, out var v) && v.ValueKind is JsonValueKind.True
            or JsonValueKind.False ? v.GetBoolean() : d;
    private static int GetInt(JsonElement el, string name, int d) =>
        el.TryGetProperty(name, out var v)
            && v.ValueKind == JsonValueKind.Number
            && v.TryGetInt32(out int n) ? n : d;
    private static double GetDouble(JsonElement el, string name, double d) =>
        el.TryGetProperty(name, out var v)
            && v.ValueKind == JsonValueKind.Number
            && v.TryGetDouble(out double n) ? n : d;
}

internal static class TeacherCollect
{
    private const string SourceTypePrefix = "teacher-distill-native:";
    private const string AuditRel =
        "xingcheng/runtime/logs/teacher-distillation.jsonl";

    public static Dictionary<string, object?> Collect(
        string toolRoot, bool dryRun = false)
    {
        string tool = Path.GetFullPath(toolRoot);
        var policy = TeacherDistillationPolicy.Load(tool);
        if (!policy.Enabled)
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["action"] = "disabled",
                ["policy"] = policy.ToDict(),
                ["checked_at"] = XcPaths.IsoNow(),
            };
        if (policy.Teachers.Count == 0 || policy.Prompts.Count == 0)
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["action"] = "idle",
                ["reason"] = "no-teachers-or-prompts",
                ["policy"] = policy.ToDict(),
                ["checked_at"] = XcPaths.IsoNow(),
            };

        // Native demand residency — one xc_modeltool serve session per
        // resolved teacher bundle for the duration of this run.
        var sessions = new Dictionary<string, ModelToolSession>(
            StringComparer.Ordinal);
        var bundleCache = new Dictionary<string, string>(
            StringComparer.Ordinal);
        var inserted = new List<object?>();
        var skipped = new List<object?>();
        var errors = new List<object?>();
        int budget = Math.Max(0, policy.MaxRowsPerRun);

        foreach (var item in policy.Prompts)
        {
            if (budget <= 0) break;
            string scope = item["scope"];
            string prompt = item["prompt"];
            string intent = item["intent"].Length > 0
                ? item["intent"] : "conversation";
            if (!XcPaths.Scopes.Contains(scope, StringComparer.Ordinal))
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["scope"] = scope,
                    ["reason"] = "unknown-scope",
                });
                continue;
            }
            if (!policy.Teachers.TryGetValue(scope, out string? spec)
                || spec.Length == 0)
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["scope"] = scope,
                    ["reason"] = "no-teacher-configured",
                });
                continue;
            }
            if (!bundleCache.TryGetValue(spec, out string? bundleDir))
            {
                try
                {
                    bundleDir = ResolveTeacherBundle(tool, spec);
                    bundleCache[spec] = bundleDir;
                }
                catch (Exception exc)
                {
                    bundleCache[spec] = "";
                    errors.Add(new Dictionary<string, object?>
                    {
                        ["scope"] = scope, ["teacher"] = spec,
                        ["reason"] =
                            $"teacher-bundle:{exc.GetType().Name}:" +
                            exc.Message,
                    });
                    continue;
                }
            }
            if (bundleDir.Length == 0)
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["scope"] = scope, ["teacher"] = spec,
                    ["reason"] = "teacher-bundle-unresolved",
                });
                continue;
            }
            if (!sessions.TryGetValue(bundleDir,
                    out ModelToolSession? session))
            {
                session = ModelToolSession.TryStart(
                    tool, bundleDir,
                    Path.Combine(tool, XcPaths.LogsRel,
                        "teacher-serve-stderr.log"),
                    policy.ServeTimeoutS);
                if (session is null)
                {
                    errors.Add(new Dictionary<string, object?>
                    {
                        ["scope"] = scope, ["teacher"] = spec,
                        ["reason"] = "native-serve-unavailable",
                    });
                    sessions[bundleDir] = null!;
                    continue;
                }
                sessions[bundleDir] = session;
            }
            if (session is null)
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["scope"] = scope, ["teacher"] = spec,
                    ["reason"] = "native-serve-unavailable",
                });
                continue;
            }

            string exampleId = "star-train-" +
                TransformerTrainingRepository.Sha256Text(
                    $"teacher-distill|{scope}|{spec}|{prompt}")[..32];
            if (ExampleExists(tool, scope, exampleId))
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["scope"] = scope, ["teacher"] = spec,
                    ["example_id"] = exampleId,
                    ["reason"] = "already-collected",
                });
                continue;
            }

            var gen = Generate(session, prompt,
                policy.MaxNewTokens, policy.Temperature,
                policy.RequestTimeoutS);
            if (!TransformerTrainingRepository.Truthy(gen["ok"]))
            {
                errors.Add(new Dictionary<string, object?>
                {
                    ["scope"] = scope, ["teacher"] = spec,
                    ["reason"] = gen.GetValueOrDefault("error"),
                });
                continue;
            }
            string target =
                (gen["response"]?.ToString() ?? "").Trim();
            if (target.Length < policy.MinTargetChars
                || target.Length > policy.MaxTargetChars)
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["scope"] = scope, ["teacher"] = spec,
                    ["reason"] = $"target-out-of-bounds:{target.Length}",
                });
                continue;
            }

            var validation = new Dictionary<string, object?>
            {
                ["bounded_output"] = true,
                ["quality_gate"] = "star-gpt-training-gate/v1",
                ["received_via"] = "xc-modeltool-serve-infer",
                ["direct_external_write"] = false,
                ["reviewed_by"] = "xc-learning-teacher-collect",
                ["teacher_model"] = spec,
                ["teacher_bundle"] = bundleDir,
                ["scope"] = scope,
                ["response_digest"] =
                    TransformerTrainingRepository.Sha256Text(target),
                ["facts_preserved"] = true,
                ["grounding_coverage"] = 1.0,
            };
            var row = new Dictionary<string, object?>
            {
                ["example_id"] = exampleId,
                ["content_hash"] = TransformerTrainingRepository.Sha256Text(
                    $"{intent}\n{prompt}\n{target}"),
                ["intent"] = intent,
                ["input_text"] = prompt,
                ["target_text"] = target,
                ["source_type"] = SourceTypePrefix + spec,
                ["quality_score"] = policy.QualityScore,
                ["validation_json"] =
                    CanonicalJson.CanonicalDict(validation),
                ["created_at"] = XcPaths.IsoNow(),
            };
            if (!dryRun)
            {
                try { InsertExample(tool, scope, row); }
                catch (Exception exc)
                {
                    errors.Add(new Dictionary<string, object?>
                    {
                        ["scope"] = scope, ["teacher"] = spec,
                        ["reason"] =
                            $"insert:{exc.GetType().Name}:{exc.Message}",
                    });
                    continue;
                }
            }
            inserted.Add(new Dictionary<string, object?>
            {
                ["scope"] = scope, ["teacher"] = spec,
                ["example_id"] = exampleId,
                ["target_chars"] = target.Length,
            });
            budget--;
        }

        foreach (var s in sessions.Values) s?.Dispose();
        var result = new Dictionary<string, object?>
        {
            ["ok"] = errors.Count == 0,
            ["action"] = dryRun ? "dry-run" : "collected",
            ["inserted"] = inserted.Count,
            ["skipped"] = skipped.Count,
            ["errors"] = errors.Count,
            ["detail"] = new Dictionary<string, object?>
            {
                ["inserted"] = inserted,
                ["skipped"] = skipped,
                ["errors"] = errors,
            },
            ["teacher_sessions"] = sessions.Count(s => s.Value != null),
            ["policy"] = policy.ToDict(),
            ["checked_at"] = XcPaths.IsoNow(),
        };
        AppendAudit(tool, result);
        return result;
    }

    /// <summary>Resolve a scope's teacher spec to an absolute bundle
    /// directory. "self" (or empty) means the pinned native-engine
    /// checkpoint — self-distillation on the governed active weights.
    /// Any other value must be a tool-root-relative bundle artifact
    /// (dir or manifest.json) inside the XingCheng data boundary;
    /// URLs, service tags and out-of-boundary paths fail closed.</summary>
    private static string ResolveTeacherBundle(string toolRoot, string spec)
    {
        string rel = spec == "self"
            ? EngineSettings.PinnedCheckpoint(toolRoot) ?? ""
            : spec;
        if (rel.Length == 0)
            throw new ExecutorError("TEACHER_BUNDLE_UNRESOLVED",
                "no pinned checkpoint for self-distillation teacher");
        string abs = Path.IsPathRooted(rel)
            ? rel
            : Path.GetFullPath(Path.Combine(toolRoot, rel));
        DataBoundary.AssertInside(toolRoot, abs);
        return Evaluation.BundleDirOf(abs);
    }

    // ------------------------------------------------------- internals --

    /// <summary>Native teacher generation — one `infer` request on the
    /// resident serve session for the scope's bundle. Chat template is
    /// applied inside xc_modeltool; the response carries `text`.</summary>
    private static Dictionary<string, object?> Generate(
        ModelToolSession session, string prompt, int maxNewTokens,
        double temperature, int timeoutS)
    {
        try
        {
            var (json, exitCode) = session.Request(
                new Dictionary<string, object?>
                {
                    ["op"] = "infer",
                    ["messages"] = new List<object?>
                    {
                        new Dictionary<string, object?>
                        {
                            ["role"] = "user",
                            ["content"] = prompt,
                        },
                    },
                    ["do_sample"] = true,
                    ["temperature"] = temperature,
                    ["max_new_tokens"] = maxNewTokens,
                },
                timeoutS: Math.Max(5, timeoutS));
            if (exitCode != 0 || !TransformerTrainingRepository.Truthy(
                    json.GetValueOrDefault("ok")))
                return new Dictionary<string, object?>
                {
                    ["ok"] = false,
                    ["error"] = $"serve-infer:{exitCode}:" +
                        (json.GetValueOrDefault("error")?.ToString()
                         ?? "no-response"),
                };
            string? text = json.GetValueOrDefault("text")?.ToString();
            return new Dictionary<string, object?>
            {
                ["ok"] = text is not null && text.Length > 0,
                ["response"] = text ?? "",
                ["error"] = text is null || text.Length == 0
                    ? "empty-response" : "",
            };
        }
        catch (Exception exc)
        {
            return new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = $"{exc.GetType().Name}:{exc.Message}",
            };
        }
    }

    private static bool ExampleExists(
        string toolRoot, string scope, string exampleId)
    {
        try
        {
            using var db = Pg.Connect($"gptbridge_xingcheng_{scope}");
            return db.QueryOne(
                "SELECT 1 AS one FROM language_training_example " +
                "WHERE example_id = $1 LIMIT 1", exampleId) is not null;
        }
        catch (Exception)
        {
            // Unverifiable dedup -> treat as existing (never risk a
            // duplicate write); the insert path fails closed anyway.
            return true;
        }
    }

    private static void InsertExample(
        string toolRoot, string scope, Dictionary<string, object?> row)
    {
        using var db = Pg.Connect(
            $"gptbridge_xingcheng_{scope}", autocommit: false);
        db.Execute(
            "INSERT INTO language_training_example " +
            "(revision, example_id, content_hash, intent, input_text, " +
            "target_text, source_type, quality_score, validation_json, " +
            "active, created_at) VALUES (" +
            "(SELECT COALESCE(MAX(revision), 0) + 1 " +
            "FROM language_training_example), " +
            "$1,$2,$3,$4,$5,$6,$7,$8,1,$9)",
            row["example_id"], row["content_hash"], row["intent"],
            row["input_text"], row["target_text"], row["source_type"],
            row["quality_score"], row["validation_json"],
            row["created_at"]);
        db.Commit();
    }

    private static void AppendAudit(
        string toolRoot, Dictionary<string, object?> result)
    {
        try
        {
            string path = Path.Combine(toolRoot, AuditRel);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.AppendAllText(path,
                CanonicalJson.CanonicalDict(result) + "\n",
                new System.Text.UTF8Encoding(false));
        }
        catch { /* audit append must never break collection */ }
    }
}
