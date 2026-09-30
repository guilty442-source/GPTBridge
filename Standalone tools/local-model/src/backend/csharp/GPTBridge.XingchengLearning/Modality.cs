// Modality.cs — §12 modality provenance + teacher lineage schemas.
//
// Llama-4 lesson: every multimodal request carries provenance —
// tokens, patch counts, sources, budgets, fusion positions,
// truncation, modality hash. TeacherLineage is schema-only: it exists
// so a future governed distillation lane has a place for
// teacher/generation/dataset/license evidence. No distillation runs.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class Modality
{
    public const string ProvenanceFormat = "star-modality-provenance/v1";
    public const string TeacherFormat = "star-teacher-lineage/v1";

    private static readonly string[] ProvenanceRequired =
        { "text_tokens", "vision_patch_count", "vision_patch_source",
          "vision_budget", "fusion_positions", "truncation",
          "modality_hash" };

    private static readonly string[] TeacherRequired =
        { "teacher_id", "teacher_generation", "dataset_id",
          "sample_hash", "verification", "license", "provenance" };

    private static Dictionary<string, object?> Check(
        JsonElement el, string format, string[] required,
        string errCode)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(errCode, "record must be object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != format)
            throw new ExecutorError(errCode,
                $"expected format {format}");
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(errCode,
                    $"missing field {k}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = format,
        };
    }

    /// <summary>Build + validate a modality provenance record for one
    /// request (called by the serve path when vision is present).</summary>
    public static Dictionary<string, object?> Provenance(
        int textTokens, int visionPatchCount, string patchSource,
        string visionBudget, int[] fusionPositions, bool truncated,
        string modalityHash)
        => new()
        {
            ["format"] = ProvenanceFormat,
            ["text_tokens"] = textTokens,
            ["vision_patch_count"] = visionPatchCount,
            ["vision_patch_source"] = patchSource,
            ["vision_budget"] = visionBudget,
            ["fusion_positions"] =
                fusionPositions.Cast<object?>().ToList(),
            ["truncation"] = truncated,
            ["modality_hash"] = modalityHash,
            ["recorded_at"] = XcPaths.IsoNow(),
        };

    public static Dictionary<string, object?> ValidateProvenance(
        JsonElement el)
        => Check(el, ProvenanceFormat, ProvenanceRequired,
                 "VISION_FALLBACK_FAILED");

    public static Dictionary<string, object?> ValidateTeacher(
        JsonElement el)
        => Check(el, TeacherFormat, TeacherRequired,
                 "BUNDLE_PROVENANCE_INVALID");
}
