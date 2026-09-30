// ModalityContracts.cs — §12 Llama-4 absorption: modality provenance
// and future-distillation lineage schemas.
//
//   star-modality-provenance/v1 — every multimodal request records the
//   text/vision token accounting: where patches came from, which
//   positions fused, what was truncated, and a modality hash so the
//   record is verifiable later.
//
//   star-teacher-lineage/v1 — schema only this phase: the lineage a
//   distilled sample would carry (teacher identity + generation,
//   dataset, sample hash, verification, license, provenance). No
//   distillation runs; records can be lodged for future use.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ModalityContracts
{
    public const string ModalityFormat = "star-modality-provenance/v1";
    public const string TeacherFormat = "star-teacher-lineage/v1";

    private static string LogsDir(string toolRoot)
        => Path.Combine(toolRoot, XcPaths.LogsRel);

    public static Dictionary<string, object?> RecordModality(
        string toolRoot, string file)
    {
        var r = ToolContracts.ReadObject(file, "MODALITY_RECORD_INVALID");
        var missing = new List<object?>();
        foreach (var k in new[] { "format", "request_id", "text_tokens",
                                  "vision_patch_count",
                                  "vision_patch_source",
                                  "vision_budget",
                                  "fusion_positions", "truncation",
                                  "modality_hash" })
            if (!r.ContainsKey(k)) missing.Add(k);
        if (missing.Count > 0)
            throw new ExecutorError("MODALITY_RECORD_INVALID",
                "missing: " + string.Join(",", missing));
        if (ToolContracts.Str(r, "format") != ModalityFormat)
            throw new ExecutorError("MODALITY_RECORD_INVALID",
                "format must be " + ModalityFormat);
        int patches = 0;
        if (r["vision_patch_count"] is long lp) patches = (int)lp;
        else if (r["vision_patch_count"] is int ip) patches = ip;
        var p = RuntimeCapabilities.Load(toolRoot);
        if (patches > p.VisionPatchBudget)
            throw new ExecutorError("VISION_BUDGET_PARITY_FAILED",
                $"patch_count {patches} exceeds budget " +
                p.VisionPatchBudget);
        Append(toolRoot, "modality-provenance.jsonl", r);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = ModalityFormat,
            ["request_id"] = r["request_id"],
            ["vision_patch_count"] = patches,
            ["vision_budget"] = p.VisionPatchBudget,
        };
    }

    public static Dictionary<string, object?> RecordTeacher(
        string toolRoot, string file)
    {
        var r = ToolContracts.ReadObject(file, "TEACHER_LINEAGE_INVALID");
        var missing = new List<object?>();
        foreach (var k in new[] { "format", "teacher_id",
                                  "teacher_generation", "dataset_id",
                                  "sample_hash", "verification",
                                  "license", "provenance" })
            if (!r.ContainsKey(k)) missing.Add(k);
        if (missing.Count > 0)
            throw new ExecutorError("TEACHER_LINEAGE_INVALID",
                "missing: " + string.Join(",", missing));
        if (ToolContracts.Str(r, "format") != TeacherFormat)
            throw new ExecutorError("TEACHER_LINEAGE_INVALID",
                "format must be " + TeacherFormat);
        Append(toolRoot, "teacher-lineage.jsonl", r);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = TeacherFormat,
            ["teacher_id"] = r["teacher_id"],
            ["dataset_id"] = r["dataset_id"],
            ["schema_only"] = true,
            ["distillation"] = "frozen",
        };
    }

    private static void Append(
        string toolRoot, string name,
        Dictionary<string, object?> record)
    {
        Directory.CreateDirectory(LogsDir(toolRoot));
        File.AppendAllText(
            Path.Combine(LogsDir(toolRoot), name),
            JsonSerializer.Serialize(record) + "\n");
    }
}
