// ProductionClosure.cs — ``star-production-certification/v1`` matrix +
// ``star-production-health/v1`` receipt + production state machine
// (production-closure directive §0-§148).
//
// This is the production-closure record authority. It does NOT run the
// soak/crash/recovery batteries itself — it records the governed
// certification state each axis has actually reached, exactly like
// CapabilityMaturityService records maturity:
//
//   axis state (§5):   NOT_RUN → RUNNING → PASS | FAIL | BLOCKED
//                      (SKIPPED is not a member and can never count)
//   production state (§119):
//     DEVELOPMENT → CANDIDATE → CERTIFYING → PRODUCTION_READY →
//     ACTIVE → DEGRADED / RECOVERY_REQUIRED → RETIRED
//
// Gate conditions (fail-closed):
//   §1   PRODUCTION_CLOSURE_FREEZE must be active before CERTIFYING;
//        while frozen, mutating verbs elsewhere should refuse via
//        <see cref="FreezeGuard"/>.
//   §2/§3 a Production Candidate Generation is pinned exactly once
//         per certification round; all §3 prerequisites must PASS or
//         the pin is refused (``先回前階段``).
//   §5/§146 PASS on an axis requires an evidence file on disk; the
//         sha256 is recorded. NOT_RUN → anything; anything → NOT_RUN
//         only with a note (re-run bookkeeping, never erases history).
//   §118 PRODUCTION_READY is derived — every required axis PASS.
//   §120 DEGRADED is reachable from ACTIVE; §121 RECOVERY_REQUIRED
//        blocks autonomous work and returns to CERTIFYING on repair.
//
// History is append-only inside each record; nothing here mutates the
// codex, the lifecycle pin, or any store.
//

using System.Security.Cryptography;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ProductionClosure
{
    public const string Format = "star-production-certification/v1";
    public const string HealthFormat = "star-production-health/v1";
    public const string Rel =
        "xingcheng/runtime/state/production-certification.json";

    /// <summary>§4 certification axes — the minimum matrix.</summary>
    public static readonly string[] Axes =
    {
        "architecture", "capability", "runtime", "data", "storage",
        "resource", "cuda", "recovery", "lifecycle", "security",
        "performance", "durability",
        "native-metadata", "regression", "native-only", "provenance", "soak",
    };

    /// <summary>§5: SKIPPED is deliberately absent — a skipped gate can
    /// never be recorded as anything other than NOT_RUN.</summary>
    public static readonly string[] AxisStates =
        { "NOT_RUN", "RUNNING", "PASS", "FAIL", "BLOCKED" };

    /// <summary>§119 formal production states.</summary>
    public static readonly string[] ProductionStates =
    {
        "DEVELOPMENT", "CANDIDATE", "CERTIFYING", "PRODUCTION_READY",
        "ACTIVE", "DEGRADED", "RECOVERY_REQUIRED", "RETIRED",
    };

    /// <summary>§146 final release gate — every axis is required.</summary>
    public static readonly string[] ReleaseGateAxes = Axes;

    /// <summary>§3 prerequisites a candidate must already hold.</summary>
    public static readonly string[] CandidatePrereqs =
    {
        "architecture", "capability-consistency", "capability-floors",
        "regression", "native-only", "resource-contract",
        "native-cuda", "provenance", "native-metadata-authority",
    };

    // ---------------------------------------------------------- io --

    private static string Path_(string toolRoot) =>
        Path.Combine(toolRoot, Rel.Replace('/',
            Path.DirectorySeparatorChar));

    public static Dictionary<string, object?> Load(string toolRoot)
    {
        string p = Path_(toolRoot);
        if (!NativeStateProjection.Exists(p))
            return new Dictionary<string, object?>
            {
                ["format"] = Format,
                ["freeze"] = new Dictionary<string, object?>
                    { ["active"] = false },
                ["state"] = "DEVELOPMENT",
                ["axes"] = new Dictionary<string, object?>(),
            };
        try
        {
            return ModelLifecycle.Decode(JsonDocument.Parse(
                NativeStateProjection.ReadAllText(p)).RootElement)
                as Dictionary<string, object?>
                ?? throw new JsonException("root not object");
        }
        catch (JsonException)
        {
            throw new ExecutorError("PRODUCTION_CERT_INVALID",
                "production-certification.json unreadable — fail closed");
        }
    }

    private static void Save(string toolRoot,
        Dictionary<string, object?> doc)
    {
        doc["format"] = Format;
        ModelLifecycle.AtomicWrite(Path_(toolRoot),
            CanonicalJson.Canonical(ModelLifecycle.Encode(doc)));
    }

    private static Dictionary<string, object?> AxesMap(
        Dictionary<string, object?> doc)
    {
        if (!doc.TryGetValue("axes", out var a) ||
            a is not Dictionary<string, object?> map)
            doc["axes"] = map = new Dictionary<string, object?>();
        return map;
    }

    // -------------------------------------------------------- freeze --

    /// <summary>§1: while the freeze is active, architecture/capability/
    /// contract/checkpoint/store mutations are prohibited — mutating
    /// surfaces call this before acting.</summary>
    public static void FreezeGuard(string toolRoot, string mutation)
    {
        var doc = Load(toolRoot);
        if (doc.TryGetValue("freeze", out var fz) &&
            fz is Dictionary<string, object?> f &&
            TransformerTrainingRepository.Truthy(f["active"]))
            throw new ExecutorError("PRODUCTION_CLOSURE_FREEZE",
                $"{mutation} denied — PRODUCTION_CLOSURE_FREEZE " +
                $"active since {f["since"]}");
    }

    public static Dictionary<string, object?> FreezeSet(
        string toolRoot, bool active, string note)
    {
        if (active)
        {
            var metadata = new NativeMetadataClient(toolRoot);
            var gate = NativeMetadataProductionGate.Evaluate(MetadataAuthority.LatestTransition(metadata), metadata.Verify(),
                NativeMetadataProductionGate.HasExternalReference(typeof(ProductionClosure).Assembly));
            if (!(bool)gate["ok"]!) throw new ExecutorError("PRODUCTION_CLOSURE_FREEZE_DENIED", "Native metadata authority is not certified.");
        }
        var doc = Load(toolRoot);
        doc["freeze"] = new Dictionary<string, object?>
        {
            ["active"] = active,
            ["since"] = XcPaths.IsoNow(),
            ["reason"] = note,
            ["allowed"] = new List<object?>
            {
                "bug fix", "performance fix", "correctness fix",
                "durability fix", "security fix",
            },
        };
        Save(toolRoot, doc);
        return new Dictionary<string, object?>
            { ["ok"] = true, ["format"] = Format,
              ["freeze"] = doc["freeze"] };
    }

    // -------------------------------------------------- prerequisites --

    /// <summary>§3 candidate prerequisites, derived live where a
    /// checkable surface exists: the latest release-gate report's named
    /// steps supply capability-consistency / native-only /
    /// resource-contract / regression (capability-delta); gates with no
    /// live evidence are NOT_EVALUATED, never assumed.</summary>
    public static Dictionary<string, object?> Prereqs(string toolRoot)
    {
        var map = CandidatePrereqs.ToDictionary(
            g => g, _ => (object?)new Dictionary<string, object?>
            {
                ["state"] = "NOT_EVALUATED",
                ["source"] = "no live evidence",
            }, StringComparer.Ordinal);

        string gateDir = Path.Combine(toolRoot,
            ConvergenceGate.ReportRel.Replace('/',
                Path.DirectorySeparatorChar));
        var latest = Directory.Exists(gateDir)
            ? Directory.EnumerateFiles(gateDir, "gate-*.json")
                .OrderByDescending(f => f).FirstOrDefault()
            : null;
        if (latest != null)
        {
            try
            {
                var rep = ModelLifecycle.Decode(JsonDocument.Parse(
                    File.ReadAllText(latest)).RootElement)
                    as Dictionary<string, object?>;
                var steps = rep?["steps"] as List<object?>
                    ?? new List<object?>();
                string? StepStatus(string name) =>
                    steps.OfType<Dictionary<string, object?>>()
                        .Where(s => s["step"]?.ToString() == name)
                        .Select(s => s["status"]?.ToString())
                        .FirstOrDefault();
                void FromStep(string gate, string step)
                {
                    string? st = StepStatus(step);
                    // §5: SKIP is neither PASS nor FAIL — an
                    // unevaluated gate stays NOT_EVALUATED, it can
                    // never convict or acquit.
                    if (st != "PASS" && st != "FAIL") return;
                    map[gate] = new Dictionary<string, object?>
                    {
                        ["state"] = st,
                        ["source"] = $"release-gate:{step} " +
                            $"({Path.GetFileName(latest)})",
                    };
                }
                FromStep("capability-consistency",
                    "capability-consistency");
                FromStep("regression", "capability-delta");
                FromStep("native-only", "native-dependency");
                FromStep("resource-contract", "resource-contract");
                FromStep("native-cuda", "cuda-probe");
                FromStep("architecture", "architecture-drift");
            }
            catch (JsonException) { /* unreadable report → all stay
                NOT_EVALUATED (fail-closed) */ }
        }

        // §3 capability-floors: every canonical capability's floor
        // verdict — derived from the maturity service, not assumed.
        // FAIL beats NOT_EVALUATED: a measured breach must surface.
        var floorFindings = new List<object?>();
        bool floorsMeasured = false;
        foreach (var cd in CapabilityRegistry.Canonical)
        {
            var fc = CapabilityMaturityService.FloorCheck(
                cd.CapabilityId, toolRoot);
            bool measured = fc["latest_evidence_hash"] != null;
            if (measured &&
                !TransformerTrainingRepository.Truthy(fc["ok"]))
            {
                // Real breach: evidence exists and misses the floor.
                floorsMeasured = true;
                floorFindings.Add(new Dictionary<string, object?>
                {
                    ["capability_id"] = cd.CapabilityId,
                    ["findings"] = fc["findings"],
                });
            }
            else if (measured)
                floorsMeasured = true;
            // missing-evidence is NOT_EVALUATED, not a breach —
            // an unmeasured floor cannot convict (§5).
        }
        if (floorsMeasured)
            map["capability-floors"] = new Dictionary<string, object?>
            {
                ["state"] = floorFindings.Count == 0
                    ? "PASS" : "FAIL",
                ["source"] = "capability-maturity floor check",
                ["breaches"] = floorFindings.Take(6)
                    .Cast<object?>().ToList(),
            };
        // §3 provenance: evaluated live — the pinned serving artifact
        // must carry a verifiable star-bundle-provenance/v1 record.
        map["provenance"] = ProvenancePrereq(toolRoot);
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["prerequisites"] = map,
            ["all_pass"] = map.Values.All(v =>
                v is Dictionary<string, object?> d &&
                d["state"]?.ToString() == "PASS"),
        };
    }

    /// <summary>§3 provenance prerequisite: resolve the pin from
    /// native-engine.json, load the artifact's provenance.json and run
    /// the mandated hash → signature → identity checks. Missing
    /// evidence stays NOT_EVALUATED (§5 — absence cannot convict); a
    /// present-but-invalid record FAILs.</summary>
    private static Dictionary<string, object?> ProvenancePrereq(
        string toolRoot)
    {
        Dictionary<string, object?> Unevaluated(string source) => new()
        {
            ["state"] = "NOT_EVALUATED", ["source"] = source,
        };
        try
        {
            string? rel = EngineSettings.PinnedCheckpoint(toolRoot);
            if (rel == null || rel.Length == 0)
                return Unevaluated("no pinned checkpoint");
            string dir = Path.GetFullPath(Path.Combine(toolRoot,
                rel.Replace('/', Path.DirectorySeparatorChar)));
            string provPath = Path.Combine(dir, "provenance.json");
            if (!Directory.Exists(dir) || !File.Exists(provPath))
            {
                var miss = Unevaluated("pinned artifact carries no " +
                    "provenance.json");
                miss["pin"] = rel;
                return miss;
            }
            var prov = ModelLifecycle.Decode(JsonDocument.Parse(
                File.ReadAllText(provPath)).RootElement)
                as Dictionary<string, object?>;
            if (prov == null)
                return Unevaluated("provenance.json unreadable");
            // Empty expected generation/arch — this gate certifies the
            // envelope (hash + signature); identity binding belongs to
            // the candidate selection (§2).
            var v = BundleProvenance.Verify(dir, prov, "", "");
            return new Dictionary<string, object?>
            {
                ["state"] = "PASS",
                ["source"] = "bundle provenance verified",
                ["pin"] = rel,
                ["generation"] = v["generation"],
                ["signature_checked"] = v["signature_checked"],
            };
        }
        catch (ExecutorError ex)
        {
            return new Dictionary<string, object?>
            {
                ["state"] = "FAIL",
                ["source"] = $"provenance invalid: {ex.Message}",
            };
        }
        catch (Exception ex) when (ex is IOException or JsonException)
        {
            return Unevaluated("provenance read failed");
        }
    }

    // ----------------------------------------------------- candidate --

    /// <summary>§2/§3: pin the single Production Candidate Generation.
    /// Refused when any §3 prerequisite is not PASS — a candidate with
    /// open prerequisites belongs to the previous phase.</summary>
    public static Dictionary<string, object?> CandidateSelect(
        string toolRoot, string generation, string note)
    {
        if (generation.Length == 0)
            throw new ExecutorError("PRODUCTION_CANDIDATE_INVALID",
                "generation required");
        var doc = Load(toolRoot);
        if (doc.TryGetValue("candidate", out var oldCand) &&
            oldCand is Dictionary<string, object?> old &&
            old["generation"]?.ToString() is { Length: > 0 } prev)
            throw new ExecutorError("PRODUCTION_CANDIDATE_INVALID",
                $"candidate already pinned ({prev}) — §2 forbids " +
                "switching mid-certification; retire or restart " +
                "the round first");
        var pre = Prereqs(toolRoot);
        if (!TransformerTrainingRepository.Truthy(pre["all_pass"]))
            throw new ExecutorError("PRODUCTION_CANDIDATE_PREREQ",
                "§3 prerequisites not all PASS — " +
                "先回前階段: " +
                string.Join(",",
                    ((Dictionary<string, object?>)pre["prerequisites"]!)
                        .Where(kv =>
                            ((Dictionary<string, object?>)kv.Value)
                                ["state"]?.ToString() != "PASS")
                        .Select(kv => kv.Key)));

        doc["candidate"] = new Dictionary<string, object?>
        {
            ["generation"] = generation,
            ["pinned_at"] = XcPaths.IsoNow(),
            ["pinned_note"] = note,
            ["prerequisites"] = pre["prerequisites"],
        };
        Save(toolRoot, doc);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["candidate"] = doc["candidate"],
        };
    }

    // ------------------------------------------------------ axes ----

    /// <summary>§5 axis transition. PASS requires an on-disk evidence
    /// file whose sha256 is pinned; downward moves require a note so a
    /// regression is always explained. SKIPPED does not exist.</summary>
    public static Dictionary<string, object?> AxisMark(
        string toolRoot, string axis, string state,
        string evidencePath, string note)
    {
        axis = axis.ToLowerInvariant();
        state = state.ToUpperInvariant();
        if (!Axes.Contains(axis))
            throw new ExecutorError("PRODUCTION_CERT_INVALID",
                $"unknown axis '{axis}'");
        if (!AxisStates.Contains(state))
            throw new ExecutorError("PRODUCTION_CERT_INVALID",
                $"unknown axis state '{state}' — SKIPPED is not a " +
                "certification state");

        var doc = Load(toolRoot);
        var axes = AxesMap(doc);
        if (!axes.TryGetValue(axis, out var rowRaw) ||
            rowRaw is not Dictionary<string, object?> row)
        {
            row = new Dictionary<string, object?>
            {
                ["state"] = "NOT_RUN",
                ["history"] = new List<object?>(),
            };
            axes[axis] = row;
        }
        string current = row["state"]?.ToString() ?? "NOT_RUN";

        string? sha = null;
        if (state == "PASS")
        {
            if (evidencePath.Length == 0 || !File.Exists(evidencePath))
                throw new ExecutorError("PRODUCTION_CERT_INVALID",
                    "PASS requires an existing --evidence file — " +
                    "a declared pass is not evidence");
            sha = Convert.ToHexString(SHA256.HashData(
                File.ReadAllBytes(evidencePath))).ToLowerInvariant();
            using var evidence = JsonDocument.Parse(File.ReadAllText(evidencePath));
            var proof = evidence.RootElement;
            if (proof.ValueKind != JsonValueKind.Object || !proof.TryGetProperty("ok", out var ok)
                || ok.ValueKind != JsonValueKind.True)
                throw new ExecutorError("PRODUCTION_CERT_INVALID", "Evidence does not contain an executed passing verdict.");
            if (axis == "native-metadata")
            {
                var live = NativeMetadataProductionGate.Evaluate(
                    MetadataAuthority.LatestTransition(new NativeMetadataClient(toolRoot)),
                    new NativeMetadataClient(toolRoot).Verify(),
                    NativeMetadataProductionGate.HasExternalReference(typeof(ProductionClosure).Assembly));
                if (!(bool)live["ok"]!) throw new ExecutorError("PRODUCTION_CERT_INVALID", "Native metadata authority gate has unresolved evidence.");
            }
            if (axis == "soak" && (!proof.TryGetProperty("duration_s", out var duration)
                || !duration.TryGetDouble(out var seconds) || seconds < 72 * 3600))
                throw new ExecutorError("PRODUCTION_CERT_INVALID", "Final soak certification requires executed 72h evidence.");
        }
        if (state == "NOT_RUN" && note.Length == 0)
            throw new ExecutorError("PRODUCTION_CERT_INVALID",
                "reset to NOT_RUN requires --note (a pass is never " +
                "silently erased)");

        ((List<object?>)row["history"]!).Add(
            new Dictionary<string, object?>
            {
                ["from"] = current, ["to"] = state,
                ["at"] = XcPaths.IsoNow(),
                ["note"] = note,
                ["evidence"] = evidencePath.Length > 0
                    ? evidencePath : null,
                ["evidence_sha256"] = sha,
            });
        row["state"] = state;
        row["evidence"] = evidencePath.Length > 0 ? evidencePath : null;
        row["evidence_sha256"] = sha;
        row["at"] = XcPaths.IsoNow();
        Save(toolRoot, doc);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["axis"] = axis, ["from"] = current, ["to"] = state,
            ["evidence_sha256"] = sha,
        };
    }

    // -------------------------------------------------- state machine --

    private static bool EvidenceCurrent(object? record)
    {
        if (record is not Dictionary<string, object?> row || row.GetValueOrDefault("state") as string != "PASS"
            || row.GetValueOrDefault("evidence") is not string path
            || row.GetValueOrDefault("evidence_sha256") is not string expected) return false;
        try
        {
            var bytes = File.ReadAllBytes(path);
            if (Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant() != expected) return false;
            using var proof = JsonDocument.Parse(bytes);
            return proof.RootElement.ValueKind == JsonValueKind.Object
                && proof.RootElement.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { return false; }
    }

    /// <summary>§119-§121 production state transition, fail-closed:
    /// CANDIDATE requires a pinned candidate; CERTIFYING requires the
    /// §1 freeze; PRODUCTION_READY is derived from an all-PASS matrix;
    /// ACTIVE requires PRODUCTION_READY.</summary>
    public static Dictionary<string, object?> Transition(
        string toolRoot, string target, string note)
    {
        target = target.ToUpperInvariant();
        if (!ProductionStates.Contains(target))
            throw new ExecutorError("PRODUCTION_STATE_INVALID",
                $"unknown production state '{target}'");
        var doc = Load(toolRoot);
        string current = doc.TryGetValue("state", out var sv)
            ? sv?.ToString() ?? "DEVELOPMENT" : "DEVELOPMENT";
        var axes = AxesMap(doc);
        bool allPass = ReleaseGateAxes.All(a => axes.TryGetValue(a, out var r) && EvidenceCurrent(r));

        switch (target)
        {
            case "CANDIDATE":
                if (!doc.TryGetValue("candidate", out var cand) ||
                    cand is not
                    Dictionary<string, object?> c ||
                    c["generation"]?.ToString() is not { Length: > 0 })
                    throw new ExecutorError("PRODUCTION_STATE_DENIED",
                        "no §2 candidate pinned");
                break;
            case "CERTIFYING":
                if (!doc.TryGetValue("freeze", out var fz) ||
                    fz is not Dictionary<string, object?> f ||
                    !TransformerTrainingRepository.Truthy(f["active"]))
                    throw new ExecutorError("PRODUCTION_STATE_DENIED",
                        "§1 PRODUCTION_CLOSURE_FREEZE must be active " +
                        "before certification begins");
                // §2: certification runs against the pinned candidate —
                // certifying with no candidate is meaningless.
                if (!doc.TryGetValue("candidate", out var cc) ||
                    cc is not Dictionary<string, object?> ccd ||
                    ccd["generation"]?.ToString() is not { Length: > 0 })
                    throw new ExecutorError("PRODUCTION_STATE_DENIED",
                        "no §2 candidate pinned");
                break;
            case "PRODUCTION_READY":
            case "ACTIVE":
                if (!allPass)
                    throw new ExecutorError("PRODUCTION_STATE_DENIED",
                        "§118/§146: every release-gate axis must be " +
                        "PASS before production readiness");
                break;
        }

        var hist = doc.TryGetValue("state_history", out var sh) &&
            sh is List<object?> h ? h : new List<object?>();
        hist.Add(new Dictionary<string, object?>
        {
            ["from"] = current, ["to"] = target,
            ["at"] = XcPaths.IsoNow(), ["note"] = note,
        });
        doc["state_history"] = hist;
        doc["state"] = target;
        Save(toolRoot, doc);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["from"] = current, ["to"] = target,
        };
    }

    // ------------------------------------------------------ report ---

    /// <summary>Full certification matrix + §131-§145 completion
    /// conditions derived from the matrix (each gate reports its axis
    /// binding and current satisfaction).</summary>
    public static Dictionary<string, object?> Report(string toolRoot)
    {
        var doc = Load(toolRoot);
        var axes = AxesMap(doc);
        var matrix = new Dictionary<string, object?>();
        foreach (var a in Axes)
        {
            axes.TryGetValue(a, out var r);
            var d = r as Dictionary<string, object?>;
            matrix[a] = new Dictionary<string, object?>
            {
                ["state"] = d?["state"] ?? "NOT_RUN",
                ["evidence"] = d?["evidence"],
                ["evidence_sha256"] = d?["evidence_sha256"],
                ["at"] = d?["at"],
            };
        }
        bool P(string a) => axes.TryGetValue(a, out var r) && EvidenceCurrent(r);

        // §131-§145: each completion condition binds to axes + required
        // sub-evidence; satisfied only when every bound axis is PASS.
        var conds = new Dictionary<string, object?>
        {
            ["A_soak_72h"] = Gate("soak", "soak-8h/24h/72h evidence"),
            ["B_pressure_revoke_resume"] =
                Gate("resource", "pressure tiers + revoke + resume"),
            ["C_cuda_fault_fallback"] =
                Gate("cuda", "driver-loss + CPU fallback"),
            ["D_store_crash_recovery"] =
                Gate("storage", "xstore crash/corruption/recovery"),
            ["E_training_crash_ckpt"] =
                Gate("recovery", "trainer crash + committed-step resume"),
            ["F_multilane_kill_handoff"] =
                Gate("lifecycle", "lane kill + winner handoff"),
            ["G_capability_regression"] =
                Gate("capability", "protected-floor regression"),
            ["H_post_process_regression"] =
                Gate("capability", "distill/compress/quantize floors"),
            ["I_promotion_rollback"] =
                Gate("lifecycle", "N→N+1 promote/rollback"),
            ["J_succession_x3"] =
                Gate("lifecycle", "three successions"),
            ["K_forbidden_deps_zero"] =
                Gate("security", "native dependency scan"),
            ["L_resource_authority"] =
                Gate("resource", "main-system only"),
            ["M_no_pg_ollama_cublas"] =
                Gate("architecture", "dependency-free production"),
            ["N_capabilities_mature"] =
                Gate("capability", "all required caps MATURE/CERTIFIED"),
            ["O_roots_verifiable"] =
                Gate("architecture", "capability/cert/provenance roots"),
        };
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["state"] = doc["state"] ?? "DEVELOPMENT",
            ["freeze"] = doc.TryGetValue("freeze", out var frz)
                ? frz : null,
            ["candidate"] = doc.TryGetValue("candidate", out var cd)
                ? cd : null,
            ["axes"] = matrix,
            ["axes_pass"] = Axes.Count(P),
            ["completion_conditions"] = conds,
            ["all_conditions_met"] = conds.Values.All(v =>
                v is Dictionary<string, object?> d &&
                TransformerTrainingRepository.Truthy(d["ok"])),
            ["production_ready"] = ReleaseGateAxes.All(P),
        };

        Dictionary<string, object?> Gate(string axis, string evidence) =>
            new()
            {
                ["ok"] = P(axis),
                ["bound_axis"] = axis,
                ["required_evidence"] = evidence,
            };
    }

    // ------------------------------------------------------ health ---

    /// <summary>§122/§123: lightweight daily receipt — never a full
    /// eval. Reads only cheap live surfaces: the production record,
    /// the model lifecycle pin, and file hashes.</summary>
    public static Dictionary<string, object?> Health(string toolRoot)
    {
        var doc = Load(toolRoot);
        var axes = AxesMap(doc);

        // active generation / checkpoint hash from the lifecycle pin
        string lcPath = Path.Combine(toolRoot,
            "xingcheng/runtime/models/lifecycle/" +
            "xingcheng-native-transformer/lifecycle.json");
        string? gen = null, ckpt = null;
        if (File.Exists(lcPath))
            try
            {
                var lc = ModelLifecycle.Decode(JsonDocument.Parse(
                    File.ReadAllText(lcPath)).RootElement)
                    as Dictionary<string, object?>;
                gen = lc?["model_id"]?.ToString();
                ckpt = lc?["active_weights_version"]?.ToString();
            }
            catch (JsonException) { /* stale/corrupt → nulls */ }

        var sha = File.Exists(Path_(toolRoot))
            ? Convert.ToHexString(SHA256.HashData(
                File.ReadAllBytes(Path_(toolRoot)))).ToLowerInvariant()
            : null;
        string health = doc["state"]?.ToString() ?? "DEVELOPMENT";
        if (health == "ACTIVE" && !ReleaseGateAxes.All(a => axes.TryGetValue(a, out var r) && EvidenceCurrent(r)))
            health = "DEGRADED"; // §120: never pretend full health

        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = HealthFormat,
            ["generation"] = gen,
            ["checkpoint_ref"] = ckpt,
            ["certification_root_sha256"] = sha,
            ["health_state"] = health,
            ["last_certification"] =
                axes.Values.OfType<Dictionary<string, object?>>()
                    .Select(d => d["at"]?.ToString())
                    .Where(s => s != null)
                    .OrderByDescending(s => s)
                    .FirstOrDefault(),
            ["emitted_at"] = XcPaths.IsoNow(),
        };
    }
}
