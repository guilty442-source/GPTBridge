using System.Text.Json.Nodes;
using Npgsql;

namespace GPTBridge.CodexPipeline;

internal static partial class SuccessorBuilder
{
    public static SuccessorBuildResult BuildSuccessor(
        string requestPath,
        string sourceDatabase,
        string outputDatabase,
        CodexAmendmentRequestLedger ledger,
        string? successorVersion = null,
        string? expectedCurrentVersion = null,
        long? expectedRevisionSequence = null)
    {
        var requestId = "";
        var outputCreated = false;
        var manifestPath = Path.ChangeExtension(
            Path.GetFullPath(outputDatabase),
            ".candidate-manifest.json");
        try
        {
            var request = Lifecycle.LoadAmendmentRequest(requestPath);
            requestId = request.RequestId;
            var source = Path.GetFullPath(sourceDatabase);
            var output = Path.GetFullPath(outputDatabase);
            if (!File.Exists(source))
                throw new SuccessorBuildError(
                    "SOURCE_DATABASE_MISSING", source);
            if (string.Equals(source, output, StringComparison.Ordinal))
                throw new SuccessorBuildError(
                    "CANDIDATE_MUST_NOT_OVERWRITE_SOURCE");
            if (File.Exists(output))
                throw new SuccessorBuildError(
                    "CANDIDATE_OUTPUT_EXISTS", output);
            if (File.Exists(manifestPath))
                throw new SuccessorBuildError(
                    "CANDIDATE_MANIFEST_EXISTS", manifestPath);
            Directory.CreateDirectory(Path.GetDirectoryName(output)!);
            List<string[]> baselineViolations;
            using (var store =
                AmendmentContract.OpenCodexStore(source))
                baselineViolations =
                    UpdateValidation.ForeignKeyViolations(store.Connection);
            var record = ledger.Begin(requestPath,
                currentVersion: expectedCurrentVersion,
                expectedRevisionSequence: expectedRevisionSequence);
            if (record.State == Lifecycle.StateSubmitted)
                ledger.Transition(requestId, Lifecycle.StateUnderReview);
            else if (record.State != Lifecycle.StateUnderReview)
                throw new SuccessorBuildError("REQUEST_NOT_UNDER_REVIEW",
                    $"{requestId}:{record.State}");
            File.Copy(source, output, overwrite: false);
            outputCreated = true;
            var errors = new List<string>();
            List<Dictionary<string, object?>> applied;
            List<Dictionary<string, object?>> deferred;
            // Parity: open_artifact(write_back=True) always dumps the
            // mutated schema back over the candidate artifact on exit;
            // a rejected candidate is then removed by unlink below.
            using (var store = AmendmentContract.OpenCodexStore(output,
                writeBack: true))
            {
                var connection = store.Connection;
                SetCandidateVersion(connection, successorVersion);
                (applied, deferred) = ApplyChanges(connection,
                    request.Payload, successorVersion);
                errors.AddRange(FormalRuleErrors(connection));
                connection.Commit();
            }
            errors.AddRange(UpdateValidation.StagedGenerationErrors(
                output, version: successorVersion,
                baselineViolations: baselineViolations));
            if (errors.Count > 0)
            {
                try { File.Delete(output); outputCreated = false; }
                catch (IOException) { }
                ledger.Transition(requestId, Lifecycle.StateRejected,
                    new Dictionary<string, object?>
                    { ["errors"] = errors.Cast<object?>().ToList() });
                return new SuccessorBuildResult(false, requestId,
                    output, manifestPath, Errors: errors);
            }
            var sealPreview =
                AmendmentContract.ComputeSealPreview(output);
            var candidateSha256 = FileSha256(output);
            var manifest = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["schema"] = CandidateManifestSchema,
                ["request_id"] = requestId,
                ["request_hash"] = request.RequestHash,
                ["lineage_key"] = request.LineageKey,
                ["source_database_sha256"] = FileSha256(source),
                ["candidate_sha256"] = candidateSha256,
                ["successor_version"] = successorVersion ?? "",
                ["predecessor"] = request.Predecessor,
                ["scope"] = request.Scope.ToList(),
                ["applied"] = applied,
                ["deferred"] = deferred,
                ["seal_preview_schema"] =
                    AmendmentContract.SealPreviewSchema,
                ["seal_preview"] = sealPreview,
                ["governor_only"] = new List<object?>
                {
                    "seal_manifest", "epoch_seal_manifest",
                    "revision_history", "atomic-publication",
                    "authority-reanchor",
                },
                ["created_at"] = Repo.UtcNow(),
                ["manifest_hash_algorithm"] =
                    AmendmentContract.ContentHashAlgorithm,
                ["manifest_hash_excludes"] = new List<object?>
                { "manifest_hash" },
            };
            manifest["manifest_hash"] = AmendmentContract.ContentHash(
                manifest.Where(pair => pair.Key != "manifest_hash")
                    .ToDictionary(pair => pair.Key, pair => pair.Value,
                        StringComparer.Ordinal));
            Repo.AtomicJson(manifestPath, manifest);
            ledger.Transition(requestId, Lifecycle.StateSuccessorBuilt,
                new Dictionary<string, object?>
                {
                    ["candidate_sha256"] = candidateSha256,
                    ["manifest_path"] = manifestPath,
                    ["seal_preview"] = sealPreview,
                });
            return new SuccessorBuildResult(true, requestId, output,
                manifestPath, candidateSha256, applied, deferred,
                SealPreview: sealPreview);
        }
        catch (Exception error) when (error is AmendmentLifecycleError
            or SuccessorBuildError or IOException or PostgresException
            or InvalidOperationException)
        {
            if (outputCreated)
            {
                try { File.Delete(Path.GetFullPath(outputDatabase)); }
                catch (IOException) { }
            }
            if (requestId.Length > 0)
            {
                try
                {
                    var record = ledger.LoadRecord(requestId);
                    var state = record?.TryGetValue("state", out var s)
                        == true ? s?.ToString() ?? "" : "";
                    if (record is not null
                        && !new HashSet<string>(StringComparer.Ordinal)
                        {
                            Lifecycle.StateRejected,
                            Lifecycle.StateExecuted,
                            "withdrawn",
                        }.Contains(state))
                        ledger.Transition(requestId,
                            Lifecycle.StateRejected,
                            new Dictionary<string, object?>
                            { ["error"] = error.Message });
                }
                catch (AmendmentLifecycleError) { }
            }
            return new SuccessorBuildResult(false, requestId,
                Path.GetFullPath(outputDatabase), manifestPath,
                Errors: new List<string> { error.Message });
        }
    }

    internal static string FileSha256(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(
            System.Security.Cryptography.SHA256.HashData(stream))
            .ToLowerInvariant();
    }
}
