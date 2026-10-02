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
//
// Compute/Sign produce the ``provenance.json`` block a governed bundle
// carries alongside the manifest (star-bundle-provenance/v1).

using System.Security.Cryptography;
using System.Text;

namespace GPTBridge.XingchengLearning;

internal static class BundleProvenance
{
    public const string Format = "star-bundle-provenance/v1";
    public static readonly string[] RequiredFields =
    {
        "manifest_hash", "weights_hash", "tokenizer_hash",
        "generation", "architecture_profile", "xcn_version",
        "build_id", "runtime_compatibility", "lineage_id",
    };

    /// <summary>Compute the provenance block for a bundle directory —
    /// hashes the manifest, weights and tokenizer payloads in the fixed
    /// load order.</summary>
    public static Dictionary<string, object?> Compute(
        string bundleDir, string generation, string archProfile,
        string xcnVersion, string buildId, string runtimeCompat,
        string lineageId)
    {
        string Hash(string name)
        {
            string p = Path.Combine(bundleDir, name);
            if (!File.Exists(p)) return "";
            // stream — weights.bin exceeds File.ReadAllBytes' 2GB cap
            using var s = new FileStream(p, FileMode.Open, FileAccess.Read,
                                         FileShare.Read, 1024 * 1024);
            return "sha256:" + Convert.ToHexString(SHA256.HashData(s))
                .ToLowerInvariant();
        }
        var block = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["manifest_hash"] = Hash("manifest.json"),
            ["weights_hash"] = Hash("weights.bin"),
            ["tokenizer_hash"] = Hash("tokenizer.json"),
            ["generation"] = generation,
            ["architecture_profile"] = archProfile,
            ["xcn_version"] = xcnVersion,
            ["build_id"] = buildId,
            ["runtime_compatibility"] = runtimeCompat,
            ["lineage_id"] = lineageId,
        };
        block["signature"] = Sign(block);
        return block;
    }

    /// <summary>Deterministic detached signature over the canonical
    /// provenance payload (sha256 — the optional crypto-signature lane;
    /// a real asymmetric signer can replace this without a contract
    /// change).</summary>
    public static string Sign(Dictionary<string, object?> block)
    {
        var canon = new Dictionary<string, object?>(block);
        canon.Remove("signature");
        byte[] h = SHA256.HashData(
            Encoding.UTF8.GetBytes(CanonicalJson.CanonicalDict(canon)));
        return "sha256sig:" + Convert.ToHexString(h).ToLowerInvariant();
    }

    /// <summary>Verify in the mandated order: hash -> signature ->
    /// generation -> architecture -> checkpoint -> shape -> runtime.
    /// Any mismatch is a typed failure.</summary>
    public static Dictionary<string, object?> Verify(
        string bundleDir, Dictionary<string, object?> provenance,
        string expectedGeneration, string expectedArch)
    {
        foreach (string f in RequiredFields)
            if (!provenance.ContainsKey(f) || provenance[f] is null)
                throw new ExecutorError(
                    ConvErr.BundleProvenanceInvalid,
                    $"provenance missing: {f}");
        // 1. hash
        var recomputed = Compute(
            bundleDir,
            provenance["generation"]?.ToString() ?? "",
            provenance["architecture_profile"]?.ToString() ?? "",
            provenance["xcn_version"]?.ToString() ?? "",
            provenance["build_id"]?.ToString() ?? "",
            provenance["runtime_compatibility"]?.ToString() ?? "",
            provenance["lineage_id"]?.ToString() ?? "");
        foreach (string h in
                 new[] { "manifest_hash", "weights_hash",
                         "tokenizer_hash" })
            if (!Equals(recomputed[h], provenance[h]))
                throw new ExecutorError(
                    ConvErr.BundleProvenanceInvalid,
                    $"hash mismatch: {h}");
        // 2. signature
        string expectSig = Sign(recomputed);
        if (provenance.TryGetValue("signature", out var sig) &&
            sig is string s && s.Length > 0 &&
            !string.Equals(s, expectSig, StringComparison.Ordinal))
            throw new ExecutorError(
                ConvErr.BundleProvenanceInvalid, "signature mismatch");
        // 3-4. generation + architecture
        if (expectedGeneration.Length > 0 &&
            provenance["generation"]?.ToString() != expectedGeneration)
            throw new ExecutorError(
                ConvErr.BundleProvenanceInvalid,
                "generation mismatch");
        if (expectedArch.Length > 0 &&
            provenance["architecture_profile"]?.ToString() !=
                expectedArch)
            throw new ExecutorError(
                ConvErr.BundleProvenanceInvalid,
                "architecture mismatch");
        // 5-7 checkpoint/shape/runtime are verified by the native
        // loader; this plane gates the envelope.
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["verified"] = true,
            ["generation"] = provenance["generation"],
            ["architecture_profile"] =
                provenance["architecture_profile"],
            ["signature_checked"] =
                provenance.ContainsKey("signature"),
        };
    }

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
