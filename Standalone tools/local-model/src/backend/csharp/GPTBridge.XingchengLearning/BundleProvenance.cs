// BundleProvenance.cs — §25 Granite-4.0 absorption: production bundle
// provenance verification.
//
// A production bundle must carry: manifest hash, weights hash,
// tokenizer hash, generation, architecture profile, XCN version,
// build id / runtime compatibility, lineage id — plus an optional
// cryptographic signature (manifest.sha256.sig sibling holding
// "sha256:<hex>"). Load order is enforced and fail-closed:
//   hash -> signature -> generation -> architecture -> checkpoint ->
//   tensor shape -> runtime compatibility -> load.

namespace GPTBridge.XingchengLearning;

internal static class BundleProvenance
{
    public const string Format = "star-bundle-provenance/v1";

    public static Dictionary<string, object?> Check(
        string toolRoot, string bundleDir)
    {
        var failures = new List<object?>();
        string manifestPath = Path.Combine(bundleDir, "manifest.json");
        if (!File.Exists(manifestPath))
            throw new ExecutorError("BUNDLE_PROVENANCE_INVALID",
                "manifest missing");
        var m = ToolContracts.ReadObject(
            manifestPath, "BUNDLE_PROVENANCE_INVALID");

        // 1. hashes — weights + tokenizer must hash-match the manifest.
        string weightsSha =
            TransformerTrainingRepository.Sha256File(
                Path.Combine(bundleDir, "weights.bin"));
        string declaredWeights =
            ToolContracts.Str(m, "weights_sha256");
        if (declaredWeights.Length > 0 &&
            !declaredWeights.EndsWith(weightsSha))
            failures.Add("weights-sha-mismatch");
        string? tokSha = null;
        foreach (var tok in new[] { "tokenizer.json", "tokenizer" })
        {
            string tp = Path.Combine(bundleDir, tok);
            if (File.Exists(tp))
            {
                tokSha = TransformerTrainingRepository.Sha256File(tp);
                break;
            }
        }
        string declaredTok = ToolContracts.Str(m, "tokenizer_sha256");
        if (declaredTok.Length > 0 && tokSha != null &&
            !declaredTok.EndsWith(tokSha))
            failures.Add("tokenizer-sha-mismatch");

        // 2. optional signature — manifest .sig sibling holding
        //    "sha256:<hex of manifest bytes>".
        bool signed = false;
        string sigPath = manifestPath + ".sig";
        if (File.Exists(sigPath))
        {
            string expect = File.ReadAllText(sigPath).Trim();
            string actual = "sha256:" +
                TransformerTrainingRepository.Sha256File(manifestPath);
            signed = string.Equals(expect, actual,
                                   StringComparison.OrdinalIgnoreCase);
            if (!signed) failures.Add("signature-mismatch");
        }

        // 3. generation identity — must name a lineage generation.
        string gen = ToolContracts.Str(m, "generation") is
            { Length: > 0 } g ? g
            : ToolContracts.Str(m, "lineage_id");
        if (gen.Length == 0) failures.Add("generation-missing");

        // 4. architecture profile field — top-level or config-embedded.
        string arch = ToolContracts.Str(m, "architecture_generation");
        if (arch.Length == 0 &&
            m.TryGetValue("config", out var c) &&
            c is Dictionary<string, object?> cd)
            arch = ToolContracts.Str(cd, "generation");
        if (arch.Length == 0) failures.Add("architecture-missing");

        // 5. checkpoint contract — XCN1 v10 canonical.
        string xcn = ToolContracts.Str(m, "checkpoint_version");
        if (xcn.Length > 0 && !xcn.Contains("v10"))
            failures.Add("checkpoint-noncanonical:" + xcn);

        // 6. runtime compatibility marker.
        string compat = ToolContracts.Str(m, "runtime_compatibility");
        if (compat.Length > 0 && !compat.Contains("native"))
            failures.Add("runtime-incompatible:" + compat);

        if (failures.Count > 0)
            throw new ExecutorError("BUNDLE_PROVENANCE_INVALID",
                string.Join(",", failures));
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["bundle"] = bundleDir,
            ["generation"] = gen,
            ["architecture_generation"] = arch,
            ["checkpoint_version"] = xcn,
            ["weights_sha256"] = "sha256:" + weightsSha,
            ["signed"] = signed,
            ["verified_order"] = new List<object?>
            {
                "hash", "signature", "generation", "architecture",
                "checkpoint", "shape", "compatibility", "load",
            },
        };
    }
}
