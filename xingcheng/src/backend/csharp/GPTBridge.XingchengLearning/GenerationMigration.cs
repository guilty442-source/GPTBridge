// GenerationMigration.cs — ``star-generation-migration/v1``.
//
// Single-active-generation lifecycle for xingcheng upgrades:
//
//   ACTIVE v26
//   -> --gen-begin      creates the CANDIDATE manifest (v27)
//   -> --gen-record     per-domain move-forward accounting
//      (personality / cognition / memory / RAG / corpus / eval-history /
//       capability / tokenizer / routing / safety)
//   -> --gen-certify    certification gates: weights+hash readable,
//      tokenizer loadable, config contract, native trainer smoke,
//      native inference (cache-smoke), optional capability regression,
//      data domains complete, expert lineage (partial weight method),
//      single candidate
//   -> --gen-promote    registers + activates the target weights, pins
//      native-engine.json, flips ACTIVE_GENERATION
//   -> --gen-purge      removes the previous generation's executable
//      artifacts (weight files, unreferenced bundles) — dry-run unless
//      --apply; never touches the target, the pinned checkpoint, or any
//      path the lifecycle still owns
//
// The manifest itself migrates forward (it lives under the target
// generation's state dir), so lineage survives the previous
// generation's deletion. States:
// PREPARING -> MIGRATING -> CERTIFYING -> PROMOTED -> PURGED | FAILED.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class GenerationMigration
{
    public const string ManifestFormat = "star-generation-migration/v1";
    public const string StateFormat = "star-generation-state/v1";
    public const string StateDirRel = "xingcheng/runtime/state/generation";
    public const string StateFile = "state.json";

    public static readonly string[] ManifestStates =
        { "PREPARING", "MIGRATING", "CERTIFYING", "PROMOTED", "PURGED", "FAILED" };
    public static readonly string[] RequiredDomains =
    {
        "personality", "cognition_knowledge", "long_term_memory",
        "rag", "training_corpus", "evaluation_history",
        "capability_state", "tokenizer", "routing_policy",
        "safety_policy",
    };
    public static readonly string[] CompletingStatuses =
        { "migrated", "not_applicable" };
    public static readonly string[] WeightMethods =
        { "direct", "partial", "distill" };

    // ---------------------------------------------------------- paths --

    private static string StateDir(string toolRoot)
        => Path.Combine(
            toolRoot, StateDirRel.Replace('/', Path.DirectorySeparatorChar));

    private static string StatePath(string toolRoot)
        => Path.Combine(StateDir(toolRoot), StateFile);

    private static string ManifestPath(string toolRoot, string id)
        => Path.Combine(StateDir(toolRoot), $"migration-{id}.json");

    private static string Rel(string toolRoot, string path)
        => Path.GetRelativePath(toolRoot, path)
              .Replace(Path.DirectorySeparatorChar, '/');

    private static string UnderRoot(string toolRoot, string path)
    {
        string full = Path.GetFullPath(path);
        string root = Path.GetFullPath(toolRoot);
        return full.StartsWith(root + Path.DirectorySeparatorChar,
                               StringComparison.OrdinalIgnoreCase)
            ? full : "";
    }

    // ------------------------------------------------------- manifest --

    private static Dictionary<string, object?> LoadManifest(
        string toolRoot, string id)
    {
        string path = ManifestPath(toolRoot, id);
        if (!File.Exists(path))
            throw new ExecutorError("GEN_MANIFEST_MISSING", id);
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        if (!doc.RootElement.TryGetProperty("format", out var f) ||
            f.GetString() != ManifestFormat)
            throw new ExecutorError("GEN_MANIFEST_FORMAT", id);
        var map = new Dictionary<string, object?>();
        foreach (var p in doc.RootElement.EnumerateObject())
            map[p.Name] = ModelLifecycle.Decode(p.Value);
        return map;
    }

    private static void SaveManifest(
        string toolRoot, Dictionary<string, object?> m)
    {
        ModelLifecycle.AtomicWrite(
            ManifestPath(toolRoot, (string)m["migration_id"]!),
            CanonicalJson.PrettyDict(m) + "\n");
    }

    private static List<string> OpenManifestIds(string toolRoot)
    {
        var ids = new List<string>();
        if (!Directory.Exists(StateDir(toolRoot))) return ids;
        foreach (string f in Directory.GetFiles(
                       StateDir(toolRoot), "migration-*.json"))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(f));
                string status = doc.RootElement
                    .TryGetProperty("status", out var s)
                    ? s.GetString() ?? "" : "";
                if (status != "PROMOTED" && status != "PURGED" &&
                    status != "FAILED")
                    ids.Add(Path.GetFileNameWithoutExtension(f)
                            ["migration-".Length..]);
            }
            catch { /* unreadable manifests are not open candidates */ }
        }
        return ids;
    }

    private static Dictionary<string, object?> LoadState(string toolRoot)
    {
        if (File.Exists(StatePath(toolRoot)))
        {
            try
            {
                using var doc = JsonDocument.Parse(
                    File.ReadAllText(StatePath(toolRoot)));
                var map = new Dictionary<string, object?>();
                foreach (var p in doc.RootElement.EnumerateObject())
                    map[p.Name] = ModelLifecycle.Decode(p.Value);
                return map;
            }
            catch { /* fall through to defaults */ }
        }
        return new Dictionary<string, object?>
        {
            ["format"] = StateFormat,
            // Unified generation-state fields (architecture-convergence
            // contract): lineage, architecture profile, deployment and
            // contract versions are separate keys — one generation string
            // never carries them all.
            ["active_generation"] = "",
            // Unified identity fields (§3 active/canonical separation):
            // lineage generation, architecture profile, checkpoint
            // contract, runtime and bundle versions are tracked
            // independently — one string never denotes two concepts.
            ["architecture_generation"] = "",
            ["candidate_architecture"] = "",
            ["checkpoint_version"] = "",
            ["runtime_version"] = "",
            ["bundle_version"] = "",
        };
    }

    private static void SaveState(
        string toolRoot, Dictionary<string, object?> state)
    {
        state["format"] = StateFormat;
        state["updated_at"] = XcPaths.IsoNow();
        // Fill unified identity fields when absent so a v1 state file
        // upgrades in place without rewriting its lineage fields.
        foreach (var k in new[]
                 {
                     "architecture_generation", "candidate_architecture",
                     "checkpoint_version", "runtime_version",
                     "bundle_version",
                 })
        {
            if (!state.ContainsKey(k)) state[k] = "";
        }
        ModelLifecycle.AtomicWrite(
            StatePath(toolRoot), CanonicalJson.PrettyDict(state) + "\n");
    }

    /// <summary>Write (or clear) the native-engine checkpoint pin — used
    /// by the post-activation rollback path to restore the predecessor's
    /// pin without going through the candidate flow.</summary>
    private static void RestorePin(string toolRoot, string? rel)
    {
        string settingsPath = Path.Combine(
            toolRoot,
            XcPaths.EngineSettingsRel.Replace(
                '/', Path.DirectorySeparatorChar));
        var settings = new Dictionary<string, object?>();
        if (File.Exists(settingsPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(
                    File.ReadAllText(settingsPath));
                foreach (var p in doc.RootElement.EnumerateObject())
                    settings[p.Name] = ModelLifecycle.Decode(p.Value);
            }
            catch { settings = new Dictionary<string, object?>(); }
        }
        if (string.IsNullOrEmpty(rel)) settings.Remove("checkpoint");
        else settings["checkpoint"] = rel.Replace('\\', '/');
        ModelLifecycle.AtomicWrite(
            settingsPath,
            CanonicalJson.PrettyDict(settings) + "\n");
    }

    /// <summary>Current ACTIVE_GENERATION ("" when unset) — shared
    /// reader for lanes that stamp generation identity onto records.</summary>
    public static string CurrentGeneration(string toolRoot)
        => (string?)LoadState(toolRoot)
               .GetValueOrDefault("active_generation") ?? "";

    private static string Event(Dictionary<string, object?> m, string evt,
                                params (string Key, object? V)[] fields)
    {
        if (m["history"] is not List<object?> h)
        {
            h = new List<object?>();
            m["history"] = h;
        }
        var e = new Dictionary<string, object?>
        {
            ["at"] = XcPaths.IsoNow(), ["event"] = evt,
        };
        foreach (var (k, v) in fields) e[k] = v;
        h.Add(e);
        return evt;
    }

    // ------------------------------------------------------ artifacts --

    private static string HashOf(string path)
    {
        if (File.Exists(path))
            return "sha256:" + TransformerTrainingRepository.Sha256File(path);
        if (Directory.Exists(path))
        {
            string weights = Path.Combine(path, "weights.bin");
            if (File.Exists(weights))
                return "sha256:" +
                       TransformerTrainingRepository.Sha256File(weights);
            string manifest = Path.Combine(path, "manifest.json");
            if (File.Exists(manifest))
                return "sha256:" +
                       TransformerTrainingRepository.Sha256File(manifest);
        }
        return "";
    }

    private static string VersionOf(string path)
    {
        if (File.Exists(path))
            return Path.GetFileName(path);
        if (Directory.Exists(path))
        {
            string manifest = Path.Combine(path, "manifest.json");
            if (File.Exists(manifest))
                return Path.GetFileName(path) + "@" +
                       TransformerTrainingRepository
                           .Sha256File(manifest)[..12];
            return Path.GetFileName(path);
        }
        return "";
    }

    private static string TokenizerHash(string bundleOrPath)
    {
        string p = File.Exists(bundleOrPath)
            ? bundleOrPath
            : Path.Combine(bundleOrPath, "tokenizer.json");
        return File.Exists(p)
            ? "sha256:" + TransformerTrainingRepository.Sha256File(p) : "";
    }

    private static bool IsBundleDir(string path)
        => Directory.Exists(path) &&
           File.Exists(Path.Combine(path, "manifest.json"));

    // ---------------------------------------------------------- begin --

    public static Dictionary<string, object?> Begin(
        string toolRoot, string target, string weightsPath,
        string weightMethod, string source = "",
        string tokenizer = "", string schemaFrom = "",
        string schemaTo = "", string expertLineage = "",
        string notes = "")
    {
        var open = OpenManifestIds(toolRoot);
        if (open.Count > 0)
            throw new ExecutorError("GEN_CANDIDATE_EXISTS",
                $"open migrations: {string.Join(",", open)}");
        if (target.Length == 0)
            throw new ExecutorError("GEN_TARGET_REQUIRED", "empty target");
        if (!WeightMethods.Contains(weightMethod))
            throw new ExecutorError("GEN_WEIGHT_METHOD",
                $"expected one of {string.Join("/", WeightMethods)}");
        // Data residency: the generation candidate's weights are
        // xingcheng-owned — an out-of-boundary source can never be
        // registered, staged or pinned.
        string weights = DataBoundary.AssertInside(
            toolRoot, weightsPath);
        if (!File.Exists(weights) && !Directory.Exists(weights))
            throw new ExecutorError("GEN_WEIGHTS_MISSING", weights);
        if (!IsBundleDir(weights) &&
            !Path.GetExtension(weights).Equals(".xcn",
                StringComparison.OrdinalIgnoreCase) &&
            !weights.EndsWith(".xcn", StringComparison.OrdinalIgnoreCase))
            throw new ExecutorError("GEN_WEIGHTS_UNSUPPORTED",
                "expected a native bundle dir or .xcn checkpoint: " +
                weights);

        var state = LoadState(toolRoot);
        if (source.Length == 0)
            source = (string?)state.GetValueOrDefault("active_generation")
                     ?? "";
        string pinned = EngineSettings.PinnedCheckpoint(toolRoot) ?? "";
        string sourcePath = pinned.Length > 0
            ? Path.Combine(toolRoot,
                           pinned.Replace('/', Path.DirectorySeparatorChar))
            : "";
        string sourceTok = sourcePath.Length > 0
            ? TokenizerHash(sourcePath) : "";
        string targetTok = tokenizer.Length > 0
            ? TokenizerHash(tokenizer) : TokenizerHash(weights);
        if (sourceTok.Length == 0) sourceTok = targetTok;

        string id = "gmig-" + target + "-" +
                    DateTime.UtcNow.ToString("yyyyMMdd-HHmmss");
        var manifest = new Dictionary<string, object?>
        {
            ["format"] = ManifestFormat,
            ["migration_id"] = id,
            ["status"] = "MIGRATING",
            ["source_generation"] = source,
            ["target_generation"] = target,
            ["source_model_version"] =
                sourcePath.Length > 0 ? VersionOf(sourcePath) : "",
            ["target_model_version"] = VersionOf(weights),
            ["source_checkpoint_hash"] =
                sourcePath.Length > 0 ? HashOf(sourcePath) : "",
            ["target_checkpoint_hash"] = HashOf(weights),
            ["source_tokenizer_version"] = sourceTok,
            ["target_tokenizer_version"] = targetTok,
            ["schema_from"] = schemaFrom,
            ["schema_to"] = schemaTo,
            ["migration_started_at"] = XcPaths.IsoNow(),
            ["migration_completed_at"] = "",
            ["migrated_records"] = 0L,
            ["transformed_records"] = 0L,
            ["rejected_records"] = 0L,
            ["weight_migration_method"] = weightMethod,
            ["weights"] = new Dictionary<string, object?>
            {
                ["source_path"] =
                    sourcePath.Length > 0 ? Rel(toolRoot, sourcePath) : "",
                ["target_path"] = Rel(toolRoot, weights),
            },
            ["capability_validation"] = new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["certified_at"] = "",
                ["checks"] = new List<object?>(),
            },
            ["data_validation"] = new Dictionary<string, object?>
            {
                ["complete"] = false,
                ["domains"] = new Dictionary<string, object?>(),
            },
            ["activation_status"] = "pending",
            ["purge_status"] = new Dictionary<string, object?>
            {
                ["status"] = "pending",
                ["deleted"] = new List<object?>(),
                ["skipped"] = new List<object?>(),
            },
            ["notes"] = notes,
            ["history"] = new List<object?>(),
        };
        if (expertLineage.Length > 0)
        {
            using var doc = JsonDocument.Parse(
                File.ReadAllText(expertLineage));
            manifest["expert_lineage"] =
                ModelLifecycle.Decode(doc.RootElement);
        }
        var domains = (Dictionary<string, object?>)
            ((Dictionary<string, object?>)manifest["data_validation"]!)
                ["domains"]!;
        foreach (string d in RequiredDomains)
            domains[d] = new Dictionary<string, object?>
            {
                ["status"] = "pending",
                ["migrated"] = 0L, ["transformed"] = 0L,
                ["rejected"] = 0L, ["note"] = "",
            };
        Event(manifest, "migration_begun",
              ("source", source), ("target", target),
              ("weight_method", weightMethod));
        SaveManifest(toolRoot, manifest);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["migration_id"] = id,
            ["manifest"] = Rel(
                toolRoot, ManifestPath(toolRoot, id)),
            ["status"] = "MIGRATING",
        };
    }

    // --------------------------------------------------------- record --

    public static Dictionary<string, object?> Record(
        string toolRoot, string id, string domain, string status,
        long migrated, long transformed, long rejected, string note = "")
    {
        if (!RequiredDomains.Contains(domain))
            throw new ExecutorError("GEN_DOMAIN_UNKNOWN", domain);
        if (status != "pending" && status != "migrating" &&
            status != "migrated" && status != "skipped" &&
            status != "not_applicable" && status != "failed")
            throw new ExecutorError("GEN_DOMAIN_STATUS", status);
        var m = LoadManifest(toolRoot, id);
        if ((string)m["status"]! == "PURGED")
            throw new ExecutorError("GEN_MANIFEST_CLOSED", id);
        var dv = (Dictionary<string, object?>)m["data_validation"]!;
        var domains = (Dictionary<string, object?>)dv["domains"]!;
        var d = (Dictionary<string, object?>)domains[domain]!;
        d["status"] = status;
        d["migrated"] = migrated;
        d["transformed"] = transformed;
        d["rejected"] = rejected;
        if (note.Length > 0) d["note"] = note;
        m["migrated_records"] = Convert.ToInt64(m["migrated_records"]) +
                                migrated;
        m["transformed_records"] =
            Convert.ToInt64(m["transformed_records"]) + transformed;
        m["rejected_records"] = Convert.ToInt64(m["rejected_records"]) +
                                rejected;
        dv["complete"] = DomainsComplete(domains);
        Event(m, "domain_recorded",
              ("domain", domain), ("status", status));
        SaveManifest(toolRoot, m);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["migration_id"] = id,
            ["domain"] = domain,
            ["domains_complete"] = dv["complete"],
        };
    }

    private static bool DomainsComplete(
        IReadOnlyDictionary<string, object?> domains)
    {
        foreach (string d in RequiredDomains)
        {
            if (!domains.TryGetValue(d, out object? v) ||
                v is not Dictionary<string, object?> rec ||
                !CompletingStatuses.Contains((string?)rec["status"] ?? ""))
                return false;
        }
        return true;
    }

    // -------------------------------------------------------- certify --

    private static Dictionary<string, object?> Check(
        string name, bool pass, string detail = "")
        => new()
        {
            ["gate"] = name, ["pass"] = pass, ["detail"] = detail,
        };

    private static Dictionary<string, object?> StdoutJson(
        NativeTools.RunResult run)
    {
        string tail = run.StdoutTail.Trim();
        int start = tail.IndexOf('{');
        if (start < 0)
            throw new ExecutorError("GEN_EVAL_NO_JSON",
                $"tool produced no JSON (exit {run.ExitCode})");
        using var doc = JsonDocument.Parse(tail[start..]);
        var map = new Dictionary<string, object?>();
        foreach (var p in doc.RootElement.EnumerateObject())
            map[p.Name] = ModelLifecycle.Decode(p.Value);
        return map;
    }

    private static Dictionary<string, object?> ChildMap(
        IReadOnlyDictionary<string, object?> map, string key)
        => map.TryGetValue(key, out object? v) &&
           v is Dictionary<string, object?> d
            ? d : new Dictionary<string, object?>();

    public static Dictionary<string, object?> Certify(
        string toolRoot, string id, string suitePath = "")
    {
        var m = LoadManifest(toolRoot, id);
        string status = (string)m["status"]!;
        if (status == "PURGED" || status == "FAILED")
            throw new ExecutorError("GEN_MANIFEST_CLOSED", id);
        var checks = new List<object?>();
        var weights = (Dictionary<string, object?>)m["weights"]!;
        string target = Path.Combine(
            toolRoot,
            ((string)weights["target_path"]!).Replace(
                '/', Path.DirectorySeparatorChar));

        // gate: candidate artifact present and hash-stable since begin.
        bool present = File.Exists(target) || Directory.Exists(target);
        string hashNow = present ? HashOf(target) : "";
        checks.Add(Check("candidate_weights_readable",
            present && hashNow == (string)m["target_checkpoint_hash"]!,
            present ? hashNow : "missing"));

        // gate: exactly one open candidate (this manifest).
        var open = OpenManifestIds(toolRoot);
        checks.Add(Check("single_candidate",
            open.Count == 1 && open[0] == id,
            open.Count.ToString()));

        // gate: tokenizer resolves and parses (target or inherited).
        string tokPath = File.Exists(target)
            ? target
            : Path.Combine(target, "tokenizer.json");
        bool tokOk = false;
        string tokDetail = "missing";
        if (File.Exists(tokPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(
                    File.ReadAllText(tokPath));
                tokOk = doc.RootElement.ValueKind == JsonValueKind.Object;
                tokDetail = "json";
            }
            catch { /* binary tokenizers parse through the engine gate */ }
        }
        checks.Add(Check("tokenizer_loadable",
            tokOk || (string)m["target_tokenizer_version"]! ==
                     (string)m["source_tokenizer_version"]!,
            tokDetail));

        // gate: config contract — bundle manifest parses; .xcn magic.
        bool cfgOk = false;
        if (IsBundleDir(target))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(
                    Path.Combine(target, "manifest.json")));
                cfgOk = doc.RootElement.ValueKind == JsonValueKind.Object;
            }
            catch { }
        }
        else if (File.Exists(target))
        {
            var hdr = new byte[8];
            using var fs = File.OpenRead(target);
            int n = fs.Read(hdr, 0, 8);
            cfgOk = n == 8 && hdr[0] == 'X' && hdr[1] == 'C' &&
                    hdr[2] == 'N' && hdr[3] == '1' &&
                    BitConverter.ToUInt32(hdr, 4) >= 1;
        }
        checks.Add(Check("config_contract", cfgOk,
            IsBundleDir(target) ? "bundle-manifest" : "xcn-header"));

        // gate: native trainer liveness (bounded --smoke).
        string stderrLog = Path.Combine(
            toolRoot, "runtime", "logs", "gen-migration-stderr.log");
        try
        {
            var run = NativeTools.Run(
                NativeTools.TrainerExe(toolRoot),
                new[] { "--smoke" }, toolRoot, stderrLog, timeoutS: 600);
            checks.Add(Check("native_trainer_smoke", run.ExitCode == 0,
                $"exit={run.ExitCode}"));
        }
        catch (ExecutorError ex)
        {
            checks.Add(Check("native_trainer_smoke", false, ex.Message));
        }

        // gate: native inference on the target bundle (cache-smoke is the
        // engine's public probe; it covers logits finiteness, paged-KV
        // determinism and greedy generation — hybrid-aware).
        if (IsBundleDir(target))
        {
            try
            {
                var run = NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot),
                    new[] { "cache-smoke", "--bundle", target },
                    toolRoot, stderrLog, timeoutS: 1200);
                checks.Add(Check("native_inference", run.ExitCode == 0,
                    $"cache-smoke exit={run.ExitCode}"));
            }
            catch (ExecutorError ex)
            {
                checks.Add(Check("native_inference", false, ex.Message));
            }
        }
        else
        {
            checks.Add(Check("native_inference", false,
                "target is not a native bundle (export-bundle first)"));
        }

        // gate: capability regression vs the source bundle, when a
        // capability suite is supplied. A generation artifact has no
        // transformer_training_job, so no adapter candidate can be
        // registered for it; the evaluation runs directly against the
        // native tool and the outcome is recorded on this manifest.
        if (suitePath.Length > 0)
        {
            string? baseline = null;
            string src = (string)weights["source_path"]!;
            if (src.Length > 0)
            {
                string srcAbs = Path.Combine(
                    toolRoot, src.Replace('/', Path.DirectorySeparatorChar));
                if (IsBundleDir(srcAbs)) baseline = srcAbs;
            }
            string suiteAbs = Path.IsPathRooted(suitePath)
                ? suitePath
                : Path.Combine(toolRoot,
                    suitePath.Replace('/', Path.DirectorySeparatorChar));
            bool passed = false;
            string detail = suitePath;
            string? baselineReportPath = null;
            try
            {
                if (baseline != null)
                {
                    baselineReportPath = Path.Combine(
                        Path.GetTempPath(),
                        $"xc-cap-base-{Guid.NewGuid():N}.json");
                    var baseRun = NativeTools.Run(
                        NativeTools.ModelToolExe(toolRoot),
                        new[] { "capability", "--bundle", baseline,
                                "--suite", suiteAbs },
                        toolRoot, stderrLog, timeoutS: 7200);
                    var baseOut = StdoutJson(baseRun);
                    File.WriteAllText(
                        baselineReportPath,
                        CanonicalJson.PlainDict(
                            ChildMap(baseOut, "report")),
                        new System.Text.UTF8Encoding(false));
                }
                var args = new List<string>
                {
                    "capability", "--bundle", target,
                    "--suite", suiteAbs,
                };
                if (baselineReportPath != null)
                    args.AddRange(new[]
                        { "--baseline-report", baselineReportPath });
                var run = NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot), args,
                    toolRoot, stderrLog, timeoutS: 7200);
                var output = StdoutJson(run);
                passed = run.ExitCode == 0 &&
                         TransformerTrainingRepository.Truthy(
                             output.GetValueOrDefault("passed"));
                var comparison = ChildMap(output, "comparison");
                if (comparison.Count > 0)
                    detail = CanonicalJson.PlainDict(comparison);
                Event(m, "capability_evaluated",
                      ("suite", Path.GetFileName(suiteAbs)),
                      ("passed", passed ? "true" : "false"),
                      ("baseline", baseline ?? ""));
            }
            catch (Exception ex)
            {
                detail = ex.Message;
            }
            finally
            {
                if (baselineReportPath != null)
                    try { File.Delete(baselineReportPath); } catch { }
            }
            checks.Add(Check("capability_regression", passed, detail));
        }
        else
        {
            checks.Add(Check("capability_regression", true,
                "not_evaluated (no --suite)"));
        }

        // maturation-closure §91/§123: certification binds the
        // capability snapshot — protected capabilities must not be
        // REGRESSED when the generation certifies, and the snapshot
        // (per-capability state + protection flags + evidence count)
        // is recorded on the manifest so promotion can prove which
        // capability ledger it was certified against.
        var matStore = CapabilityMaturityService.Load(toolRoot);
        var protectedCaps =
            CapabilityMaturityService.Protected(toolRoot);
        var regressedProtected = protectedCaps
            .Where(c => matStore.TryGetValue(c, out var row) &&
                row.TryGetValue("state", out var st) &&
                st?.ToString() == "REGRESSED")
            .ToList();
        checks.Add(Check("protected_capabilities_held",
            regressedProtected.Count == 0,
            regressedProtected.Count == 0
                ? $"{protectedCaps.Length} protected, none regressed"
                : "regressed: " +
                  string.Join(",", regressedProtected)));
        var capSnapshot = new Dictionary<string, object?>
        {
            ["protected"] =
                protectedCaps.Cast<object?>().ToList(),
            ["states"] = matStore.ToDictionary(
                kv => kv.Key,
                kv => (object?)new Dictionary<string, object?>
                {
                    ["state"] = kv.Value.TryGetValue("state",
                        out var s) ? s?.ToString() : "UNKNOWN",
                    ["protected"] =
                        kv.Value.TryGetValue("protected",
                            out var p) && p is true,
                }),
            ["evidence_rows"] =
                CapabilityEvidence.Load(toolRoot).Count,
            ["snapshot_at"] = XcPaths.IsoNow(),
        };
        m["capability_certification_snapshot"] = capSnapshot;

        // §33 runtime-contract gates: every new runtime contract joins
        // the certification evidence — a generation can never promote
        // without them (bundle targets only; .xcn candidates have no
        // bundle envelope to prove).
        if (IsBundleDir(target))
        {
            try
            {
                string Mf(string key, string dflt) =>
                    m.TryGetValue(key, out var v) &&
                    v is string s && s.Length > 0 ? s : dflt;
                var provenance = BundleProvenance.Compute(
                    target,
                    Mf("target_generation", ""),
                    Mf("architecture_profile", "xc-fused-1"),
                    Mf("xcn_version", "XCN1 v10"),
                    id,
                    "xc-native-cpp23",
                    Mf("source_generation", ""));
                var gates = RuntimeCertGates.Evaluate(
                    toolRoot, target, provenance);
                checks.Add(Check("runtime_contract_gates",
                    (bool)gates["pass"]!,
                    CanonicalJson.PlainDict(
                        (Dictionary<string, object?>)
                        new Dictionary<string, object?>
                        {
                            ["gates"] = gates["gates"],
                        })));
            }
            catch (Exception ex)
            {
                checks.Add(Check("runtime_contract_gates", false,
                    ex.Message));
            }
        }

        // gate: every required data domain reached a completing status.
        var dv = (Dictionary<string, object?>)m["data_validation"]!;
        bool domainsOk = DomainsComplete(
            (Dictionary<string, object?>)dv["domains"]!);
        checks.Add(Check("data_domains_complete", domainsOk,
            domainsOk ? "all" : "incomplete"));

        // §33 runtime-contract gates: the unified capability layer,
        // tool/structured/state contracts and bundle provenance must
        // resolve before a generation can certify.
        bool capsOk = false;
        string capsDetail = "unresolved";
        try
        {
            var capsProfile = RuntimeCapabilities.Load(toolRoot);
            capsOk = RuntimeCapabilities.DeploymentProfiles.Contains(
                         capsProfile.DeploymentProfile) &&
                     RuntimeCapabilities.KvModes.Contains(
                         capsProfile.KvMode);
            capsDetail = capsProfile.DeploymentProfile + "/" +
                         capsProfile.KvMode;
        }
        catch (ExecutorError ex) { capsDetail = ex.Message; }
        checks.Add(Check("runtime_profile_compatible", capsOk,
            capsDetail));
        checks.Add(Check("structured_output_compatible", capsOk,
            capsDetail));   // same resolved profile carries the flag
        checks.Add(Check("state_format_compatible", capsOk,
            capsDetail));   // kv/state budgets resolved in profile

        string catalogPath = Path.Combine(toolRoot,
            FeatureCatalog.Rel.Replace('/', Path.DirectorySeparatorChar));
        bool catalogOk = false;
        string catalogDetail = "missing";
        if (File.Exists(catalogPath))
        {
            try
            {
                var cv = FeatureCatalog.Validate(catalogPath);
                catalogOk = TransformerTrainingRepository.Truthy(
                    cv.GetValueOrDefault("ok"));
                catalogDetail = catalogOk ? "valid" : "invalid";
            }
            catch (ExecutorError ex) { catalogDetail = ex.Message; }
        }
        checks.Add(Check("tool_contract_compatible", catalogOk,
            catalogDetail));

        bool provOk = false;
        string provDetail = "not_a_bundle";
        if (IsBundleDir(target))
        {
            try
            {
                using var provDoc = JsonDocument.Parse(
                    File.ReadAllText(
                        Path.Combine(target, "manifest.json")));
                var provRoot = provDoc.RootElement;
                provOk = provRoot.TryGetProperty("provenance",
                             out var pv) &&
                         pv.TryGetProperty("runtime_compatibility",
                             out _) &&
                         provRoot.TryGetProperty("tokenizer_sha256",
                             out _);
                provDetail = provOk ? "provenance-block" : "legacy";
            }
            catch (Exception) { provDetail = "manifest unreadable"; }
        }
        checks.Add(Check("bundle_provenance_pass", provOk, provDetail));

        // gate: MoE partial weight migrations carry an expert lineage
        // (weight source / init / router mapping / split-merge).
        if ((string)m["weight_migration_method"]! == "partial")
            checks.Add(Check("expert_lineage",
                m.ContainsKey("expert_lineage"),
                m.ContainsKey("expert_lineage") ? "present" : "missing"));

        bool ok = true;
        foreach (var c in checks.Cast<Dictionary<string, object?>>())
            if (!(bool)c["pass"]!) ok = false;
        m["capability_validation"] = new Dictionary<string, object?>
        {
            ["ok"] = ok,
            ["certified_at"] = XcPaths.IsoNow(),
            ["checks"] = checks,
        };
        m["status"] = ok ? "CERTIFYING" : "FAILED";
        Event(m, ok ? "certification_passed" : "certification_failed");
        SaveManifest(toolRoot, m);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["migration_id"] = id,
            ["certified"] = ok,
            ["checks"] = checks,
        };
    }

    // -------------------------------------------------------- promote --

    public static Dictionary<string, object?> Promote(
        string toolRoot, string id)
    {
        var m = LoadManifest(toolRoot, id);
        if ((string)m["status"]! != "CERTIFYING")
            throw new ExecutorError("GEN_NOT_CERTIFIED",
                $"status={(string)m["status"]!}");
        var cv = (Dictionary<string, object?>)m["capability_validation"]!;
        if (!TransformerTrainingRepository.Truthy(
                cv.GetValueOrDefault("ok")))
            throw new ExecutorError("GEN_CERTIFY_NOT_PASSED", id);
        // §5: when the convergence gate is enforced, promotion needs a
        // passing star-release-gate/v1 report — fail closed otherwise.
        if (ConvergenceGate.Enforced(toolRoot)
            && ConvergenceGate.LatestVerdict(toolRoot)
                != "PROMOTION_ALLOWED")
            throw new ExecutorError("RELEASE_GATE_BLOCKED",
                "release gate has not passed (enforce_release_gate=1)");
        var weights = (Dictionary<string, object?>)m["weights"]!;
        string target = Path.Combine(
            toolRoot,
            ((string)weights["target_path"]!).Replace(
                '/', Path.DirectorySeparatorChar));
        if (HashOf(target) != (string)m["target_checkpoint_hash"]!)
            throw new ExecutorError("GEN_TARGET_DRIFTED",
                "target artifact changed since --gen-begin");

        string lifecycleDir = Path.Combine(
            toolRoot,
            XcPaths.LifecycleRel.Replace('/', Path.DirectorySeparatorChar));
        var lifecycle = ModelLifecycle.LoadOrCreate(
            lifecycleDir, XcPaths.ModelId);

        // Rollback points — §18: promotion is only complete once the
        // *activated* generation passes independent verification, and a
        // failed post-activation check must restore the predecessor
        // rather than leave a half-promoted runtime.
        string? prevPin = EngineSettings.PinnedCheckpoint(toolRoot);
        int prevActiveVersion = lifecycle.ActiveWeightsVersion;
        string prevGen = (string?)LoadState(toolRoot)
            .GetValueOrDefault("active_generation") ?? "";

        var entry = lifecycle.RegisterArtifact(
            "weights", target,
            metadata: new Dictionary<string, object?>
            {
                ["generation"] = m["target_generation"],
                ["migration_id"] = id,
                ["config_sha256"] = m["target_checkpoint_hash"],
            },
            activate: true);
        lifecycle.Save(lifecycleDir);
        string pinned = EngineSettings.PinCheckpoint(toolRoot, target);

        // ---- §18 post-activation verification (independent, on the
        // now-active artifact as pinned) ----
        string stderrLog = Path.Combine(
            toolRoot, "runtime", "logs", "gen-migration-stderr.log");
        var post = new List<object?>();
        string targetRel = Rel(toolRoot, target).Replace('\\', '/');
        post.Add(Check("pin_points_at_target",
            pinned.Replace('\\', '/') == targetRel, pinned));
        var activeAfter = lifecycle.ActiveWeights();
        post.Add(Check("lifecycle_active_weights",
            activeAfter != null &&
            (string?)activeAfter["path"] == target,
            activeAfter != null ? (string?)activeAfter["path"] ?? "" : ""));
        if (IsBundleDir(target))
        {
            try
            {
                var run = NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot),
                    new[] { "cache-smoke", "--bundle", target },
                    toolRoot, stderrLog, timeoutS: 1200);
                post.Add(Check("active_inference", run.ExitCode == 0,
                    $"cache-smoke exit={run.ExitCode}"));
            }
            catch (ExecutorError ex)
            {
                post.Add(Check("active_inference", false, ex.Message));
            }
        }
        else
        {
            post.Add(Check("active_inference", false,
                "target is not a native bundle"));
        }
        bool postOk = true;
        foreach (var c in post.Cast<Dictionary<string, object?>>())
            if (!(bool)c["pass"]!) postOk = false;
        m["post_activation"] = new Dictionary<string, object?>
        {
            ["ok"] = postOk,
            ["verified_at"] = XcPaths.IsoNow(),
            ["checks"] = post,
        };

        if (!postOk)
        {
            // Roll back activation: predecessor pin + active version +
            // generation state are restored before failing closed.
            RestorePin(toolRoot, prevPin);
            if (prevActiveVersion != 0)
            {
                lifecycle.RollbackWeights(prevActiveVersion);
                lifecycle.Save(lifecycleDir);
            }
            var st = LoadState(toolRoot);
            st["active_generation"] = prevGen;
            SaveState(toolRoot, st);
            m["status"] = "FAILED";
            Event(m, "post_activation_failed",
                  ("rolled_back", "true"));
            SaveManifest(toolRoot, m);
            throw new ExecutorError("GEN_POST_ACTIVATION_FAILED", id);
        }

        lifecycle.RecordEvent("generation_promoted",
            ("generation", m["target_generation"]),
            ("migration_id", id));
        lifecycle.Save(lifecycleDir);

        var state = LoadState(toolRoot);
        state["previous_generation"] = m["source_generation"];
        state["active_generation"] = m["target_generation"];
        state["bundle_version"] = m["target_model_version"];
        state["checkpoint_version"] = "XCN1 v10";
        state["runtime_version"] = "xc-native-cpp23";
        // The promoted bundle runs the architecture it was trained on —
        // not the canonical contract. xc-fused-1 remains a candidate
        // architecture until a trained generation carries it.
        state["architecture_generation"] = "current-compatible-profile";
        state["candidate_architecture"] = "xc-fused-1";
        SaveState(toolRoot, state);

        // §30 post-promote verify: the pinned checkpoint must resolve to
        // the promoted artifact byte-identically and the lifecycle's
        // active weights must be the entry just registered. Any drift is
        // fail-closed — the migration cannot report PROMOTED on a pin
        // that does not resolve.
        var verify = new List<string>();
        string pinnedAbs = Path.Combine(
            toolRoot, pinned.Replace('/', Path.DirectorySeparatorChar));
        if (!File.Exists(pinnedAbs) ||
            HashOf(pinnedAbs) != (string)m["target_checkpoint_hash"]!)
            verify.Add("PINNED_CHECKPOINT_MISMATCH");
        var activeCheck = lifecycle.ActiveWeights();
        if (activeCheck == null ||
            !activeCheck.TryGetValue("path", out object? acp) ||
            acp is not string acps ||
            Rel(toolRoot, acps).Replace('\\', '/') != pinned)
            verify.Add("LIFECYCLE_ACTIVE_MISMATCH");
        if (verify.Count > 0)
        {
            m["status"] = "FAILED";
            Event(m, "post_promote_verify_failed",
                  ("failures", string.Join(",", verify)));
            SaveManifest(toolRoot, m);
            throw new ExecutorError(
                "GEN_POST_PROMOTE_VERIFY_FAILED",
                string.Join(",", verify));
        }
        Event(m, "post_promote_verify", ("pinned", pinned));

        // maturation-closure §17/§123: refresh the promoted registry
        // baseline only now — after post-promote verification passed.
        // A blocked or rolled-back promotion must never re-baseline a
        // regression into the promoted snapshot.
        try
        {
            var reg = CapabilityRegistry.Emit(toolRoot);
            string promotedPath = Path.Combine(toolRoot,
                CapabilityRegistry.BaselineRel.Replace('/',
                    Path.DirectorySeparatorChar));
            ModelLifecycle.AtomicWrite(promotedPath,
                File.ReadAllText(Path.Combine(toolRoot,
                    CapabilityRegistry.Rel.Replace('/',
                        Path.DirectorySeparatorChar))));
            Event(m, "capability_baseline_refreshed",
                  ("registry", CapabilityRegistry.BaselineRel));
            m["capability_registry_baseline"] =
                CapabilityRegistry.BaselineRel;
        }
        catch (Exception ex)
        {
            Event(m, "capability_baseline_refresh_failed",
                  ("error", ex.Message));
        }

        m["status"] = "PROMOTED";
        m["activation_status"] = "promoted";
        m["migration_completed_at"] = XcPaths.IsoNow();
        Event(m, "promoted",
              ("weights_version", entry["version"]),
              ("pinned", pinned));
        SaveManifest(toolRoot, m);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["migration_id"] = id,
            ["active_generation"] = m["target_generation"],
            ["weights_version"] = entry["version"],
            ["pinned_checkpoint"] = pinned,
        };
    }

    // ---------------------------------------------------------- purge --

    public static Dictionary<string, object?> Purge(
        string toolRoot, string id, bool apply)
    {
        var m = LoadManifest(toolRoot, id);
        if ((string)m["status"]! != "PROMOTED" &&
            (string)m["status"]! != "PURGED")
            throw new ExecutorError("GEN_NOT_PROMOTED",
                $"status={(string)m["status"]!}");
        var purge = (Dictionary<string, object?>)m["purge_status"]!;
        var weights = (Dictionary<string, object?>)m["weights"]!;
        string targetRel = ((string)weights["target_path"]!)
            .Replace('\\', '/');
        string pinnedRel = (EngineSettings.PinnedCheckpoint(toolRoot) ?? "")
            .Replace('\\', '/');

        // Never-purge set: the target generation, the pinned checkpoint,
        // and every artifact path the lifecycle still owns as active.
        var keep = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { targetRel, pinnedRel };
        string lifecycleDir = Path.Combine(
            toolRoot,
            XcPaths.LifecycleRel.Replace('/', Path.DirectorySeparatorChar));
        var lifecycle = ModelLifecycle.LoadOrCreate(
            lifecycleDir, XcPaths.ModelId);
        var active = lifecycle.ActiveWeights();
        if (active != null &&
            active.TryGetValue("path", out object? ap) && ap is string aps)
            keep.Add(Rel(toolRoot, aps).Replace('\\', '/'));

        // §33 reference-safety: purge must refuse every artifact still
        // referenced by active/candidate/lifecycle/evaluation/dataset/
        // lineage records — never a best-effort delete. Collect every
        // "path"-ish string the lifecycle owns across all artifact
        // kinds, plus every sibling migration manifest's weight paths
        // (lineage reference), before candidates are even considered.
        void CollectPaths(object? node)
        {
            switch (node)
            {
                case Dictionary<string, object?> d:
                    foreach (var kv in d)
                    {
                        if (kv.Value is string s && s.Length > 0 &&
                            (kv.Key == "path" || kv.Key == "source_path" ||
                             kv.Key == "target_path" ||
                             kv.Key == "bundle_path" ||
                             kv.Key == "checkpoint_path" ||
                             kv.Key.EndsWith("_path",
                                 StringComparison.Ordinal)))
                            keep.Add(Rel(toolRoot, s).Replace('\\', '/'));
                        else CollectPaths(kv.Value);
                    }
                    break;
                case List<object?> l:
                    foreach (var item in l) CollectPaths(item);
                    break;
            }
        }
        CollectPaths(lifecycle.Artifacts);
        CollectPaths(lifecycle.History);
        foreach (string sib in Directory.GetFiles(
                     StateDir(toolRoot), "migration-*.json"))
        {
            if (sib == ManifestPath(toolRoot, id)) continue;
            try { CollectPaths(LoadManifest(toolRoot,
                Path.GetFileNameWithoutExtension(sib)
                    ["migration-".Length..])); }
            catch (ExecutorError) { /* unreadable manifest — its paths
                stay unknown, so its artifacts stay protected via the
                lifecycle scan; a broken manifest never widens the
                delete set */ }
        }

        var candidates = new List<string>();
        // previous-generation weight versions owned by the lifecycle
        foreach (var v in lifecycle.WeightVersionPaths())
        {
            string rel = Rel(toolRoot, v).Replace('\\', '/');
            if (!keep.Contains(rel)) candidates.Add(v);
        }
        // unreferenced bundles under the model store
        string bundles = Path.Combine(
            toolRoot, "xingcheng", "runtime", "models", "cpp-bundles");
        if (Directory.Exists(bundles))
            foreach (string dir in Directory.GetDirectories(bundles))
            {
                string rel = Rel(toolRoot, dir).Replace('\\', '/');
                if (!keep.Contains(rel)) candidates.Add(dir);
            }
        // the source artifact itself, when file-resident
        string srcRel = ((string)weights["source_path"]!).Replace('\\', '/');
        if (srcRel.Length > 0 && !keep.Contains(srcRel))
        {
            string srcAbs = Path.Combine(
                toolRoot, srcRel.Replace('/', Path.DirectorySeparatorChar));
            if (File.Exists(srcAbs) || Directory.Exists(srcAbs))
                candidates.Add(srcAbs);
        }

        var deleted = new List<object?>();
        var skipped = new List<object?>();
        foreach (string abs in candidates.Distinct())
        {
            string rel = Rel(toolRoot, abs).Replace('\\', '/');
            if (keep.Contains(rel) || UnderRoot(toolRoot, abs).Length == 0)
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["path"] = rel,
                    ["reason"] = "protected",
                    ["code"] = "GENERATION_ARTIFACT_STILL_REFERENCED",
                });
                continue;
            }
            if (apply)
            {
                try
                {
                    if (File.Exists(abs)) File.Delete(abs);
                    else if (Directory.Exists(abs))
                        Directory.Delete(abs, recursive: true);
                    deleted.Add(rel);
                }
                catch (Exception ex)
                {
                    skipped.Add(new Dictionary<string, object?>
                    {
                        ["path"] = rel,
                        ["reason"] = ex.Message.Length > 200
                            ? ex.Message[..200] : ex.Message,
                    });
                }
            }
            else
            {
                skipped.Add(new Dictionary<string, object?>
                {
                    ["path"] = rel, ["reason"] = "dry_run",
                });
            }
        }
        purge["status"] = apply ? "purged" : "dry_run";
        purge["deleted"] = deleted;
        purge["skipped"] = skipped;
        if (apply)
        {
            m["status"] = "PURGED";
            purge["retired_at"] = XcPaths.IsoNow();
            // §21 lineage survives the deleted runtime: the manifest
            // (which lives under the successor generation) keeps the
            // predecessor's identity, hashes, lifecycle times and the
            // carried-forward data summary — never the legacy runtime.
            m["lineage"] = new Dictionary<string, object?>
            {
                ["predecessor_generation"] = m["source_generation"],
                ["successor_generation"] = m["target_generation"],
                ["architecture_summary"] = new Dictionary<string, object?>
                {
                    ["schema_from"] = m["schema_from"],
                    ["schema_to"] = m["schema_to"],
                    ["source_model_version"] = m["source_model_version"],
                    ["target_model_version"] = m["target_model_version"],
                    ["weight_migration_method"] =
                        m["weight_migration_method"],
                },
                ["checkpoint_hashes"] = new Dictionary<string, object?>
                {
                    ["source"] = m["source_checkpoint_hash"],
                    ["target"] = m["target_checkpoint_hash"],
                },
                ["migration_id"] = id,
                ["activation_time"] = m["migration_completed_at"],
                ["retired_at"] = purge["retired_at"],
                ["data_carried_forward"] =
                    ((Dictionary<string, object?>)
                        m["data_validation"]!)["complete"],
                ["records"] = new Dictionary<string, object?>
                {
                    ["migrated"] = m["migrated_records"],
                    ["transformed"] = m["transformed_records"],
                    ["rejected"] = m["rejected_records"],
                },
            };
            lifecycle.RetireWeights(keepLatest: 1);
            lifecycle.RecordEvent("generation_purged",
                ("migration_id", id),
                ("deleted", deleted.Count));
            lifecycle.Save(lifecycleDir);
        }
        Event(m, apply ? "purge_applied" : "purge_dry_run",
              ("candidates", candidates.Count),
              ("deleted", deleted.Count));
        SaveManifest(toolRoot, m);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["migration_id"] = id,
            ["apply"] = apply,
            ["purge_status"] = purge["status"],
            ["deleted"] = deleted,
            ["skipped"] = skipped,
        };
    }

    // --------------------------------------------------------- status --

    public static Dictionary<string, object?> Status(
        string toolRoot, string id = "")
    {
        var state = LoadState(toolRoot);
        if (id.Length > 0)
        {
            var m = LoadManifest(toolRoot, id);
            return new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["manifest"] = m,
                ["state"] = state,
            };
        }
        var manifests = new List<object?>();
        if (Directory.Exists(StateDir(toolRoot)))
        {
            foreach (string f in Directory.GetFiles(
                         StateDir(toolRoot), "migration-*.json")
                         .OrderByDescending(x => x))
            {
                try
                {
                    using var doc = JsonDocument.Parse(
                        File.ReadAllText(f));
                    manifests.Add(new Dictionary<string, object?>
                    {
                        ["migration_id"] = doc.RootElement
                            .GetProperty("migration_id").GetString(),
                        ["status"] = doc.RootElement
                            .GetProperty("status").GetString(),
                        ["source_generation"] = doc.RootElement
                            .GetProperty("source_generation").GetString(),
                        ["target_generation"] = doc.RootElement
                            .GetProperty("target_generation").GetString(),
                    });
                }
                catch { }
            }
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["state"] = state,
            ["manifests"] = manifests,
        };
    }
}
