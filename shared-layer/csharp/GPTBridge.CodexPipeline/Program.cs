using System.Text.Json.Nodes;
using GPTBridge.CodexPipeline;

/// <summary>
/// GPTBridge Codex amendment pipeline CLI — the C# entry point that
/// replaces ``python -m governance_rule.execution.codex_amendment_driver``
/// and ``codex_amendment_executor``.
///
/// Driver mode:
///   --scan                     discover staged request artifacts
///   --request &lt;path&gt; [--source &lt;sql&gt;] [--auto-execute]
///   --all [--source &lt;sql&gt;] [--auto-execute]
///
/// Executor mode (governor-invoked):
///   --execute --request &lt;path&gt; [--prepared &lt;sql&gt;]
///       [--staging &lt;dir&gt;] [--audit-result &lt;json&gt;] [--apply]
///
/// Parity probes (read-only, used by the oracle-parity harness):
///   --canonical &lt;json&gt;         canonical JSON text
///   --hash-content &lt;json&gt;      content_hash of a JSON payload
///   --hash-revision &lt;json&gt;     revision_entry_hash of a field map
///   --hash-doc &lt;text-file&gt;     search_document_hash of raw text
///   --seal-preview &lt;sql&gt;       compute_seal_preview for an artifact
///   --artifact-tables &lt;sql&gt;    artifact_table_names
///   --artifact-version &lt;sql&gt;   artifact_version
///   --roundtrip &lt;sql&gt; &lt;out&gt;    materialize + dump (byte parity)
///   --export &lt;target&gt;          export_postgresql_codex
///   --authority-state          codex_authority_state row
///   --verify-parity &lt;sql&gt;      verify_sql_parity
///
/// Global:
///   --root &lt;dir&gt;             repository root override
/// </summary>
internal static class Program
{
    private static void Usage()
    {
        Console.Error.WriteLine(
            "usage: GPTBridge.CodexPipeline --scan | --request <path> |"
            + " --all [--source <sql>] [--auto-execute]\n"
            + "       GPTBridge.CodexPipeline --execute --request <path>"
            + " [--prepared <sql>] [--staging <dir>]"
            + " [--audit-result <json>] [--apply]\n"
            + "       [--root <dir>]");
    }

    private static int Emit(object? payload)
    {
        Console.WriteLine(PrettyJson.Serialize(payload));
        return 0;
    }

    private static string FileHash(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(
            System.Security.Cryptography.SHA256.HashData(stream))
            .ToLowerInvariant();
    }

    public static async Task<int> Main(string[] args)
    {
        var scan = false;
        var all = false;
        var execute = false;
        var apply = false;
        var autoExecute = false;
        string? request = null;
        string? source = null;
        string? prepared = null;
        string? staging = null;
        string? auditResultPath = null;
        string? root = null;
        string? canonicalPath = null;
        string? hashContent = null;
        string? hashRevision = null;
        string? hashDoc = null;
        string? sealPreview = null;
        string? artifactTables = null;
        string? artifactVersion = null;
        string? roundtrip = null;
        string? roundtripOut = null;
        string? exportTarget = null;
        string? verifyParity = null;
        string? buildRequest = null;
        string? buildSource = null;
        string? buildOutput = null;
        string? ledgerRoot = null;
        string? successorVersion = null;
        var authorityState = false;
        for (var i = 0; i < args.Length; i++)
        {
            string Value()
            {
                if (i + 1 >= args.Length)
                    throw new ArgumentException(
                        $"missing value for {args[i]}");
                return args[++i];
            }
            switch (args[i])
            {
                case "--scan": scan = true; break;
                case "--all": all = true; break;
                case "--execute": execute = true; break;
                case "--apply": apply = true; break;
                case "--auto-execute": autoExecute = true; break;
                case "--authority-state": authorityState = true; break;
                case "--request": request = Value(); break;
                case "--source": source = Value(); break;
                case "--prepared": prepared = Value(); break;
                case "--staging": staging = Value(); break;
                case "--audit-result": auditResultPath = Value(); break;
                case "--root": root = Value(); break;
                case "--canonical": canonicalPath = Value(); break;
                case "--hash-content": hashContent = Value(); break;
                case "--hash-revision": hashRevision = Value(); break;
                case "--hash-doc": hashDoc = Value(); break;
                case "--seal-preview": sealPreview = Value(); break;
                case "--artifact-tables": artifactTables = Value(); break;
                case "--artifact-version": artifactVersion = Value(); break;
                case "--roundtrip":
                    roundtrip = Value();
                    roundtripOut = Value();
                    break;
                case "--export": exportTarget = Value(); break;
                case "--verify-parity": verifyParity = Value(); break;
                case "--build-successor": buildRequest = Value();
                    buildSource = Value(); buildOutput = Value(); break;
                case "--ledger-root": ledgerRoot = Value(); break;
                case "--successor-version": successorVersion = Value(); break;
                default:
                    Console.Error.WriteLine(
                        $"unrecognized argument: {args[i]}");
                    Usage();
                    return 2;
            }
        }
        if (root is not null)
            Repo.SetRoot(root);
        try
        {
            if (canonicalPath is not null)
            {
                var payload = Repo.ToPlain(
                    JsonNode.Parse(File.ReadAllText(canonicalPath)));
                Console.WriteLine(CanonJson.Serialize(payload));
                return 0;
            }
            if (hashContent is not null)
            {
                var payload = Repo.ToPlain(
                    JsonNode.Parse(File.ReadAllText(hashContent)));
                return Emit(new Dictionary<string, object?>
                {
                    ["content_hash"] =
                        AmendmentContract.ContentHash(payload),
                });
            }
            if (hashRevision is not null)
            {
                var payload = (Dictionary<string, object?>)Repo.ToPlain(
                    JsonNode.Parse(File.ReadAllText(hashRevision)))!;
                return Emit(new Dictionary<string, object?>
                {
                    ["revision_entry_hash"] =
                        AmendmentContract.RevisionEntryHash(payload),
                });
            }
            if (hashDoc is not null)
                // Python open().read() normalizes CRLF→LF; hash the
                // logical text, not the raw file bytes.
                return Emit(new Dictionary<string, object?>
                {
                    ["search_document_hash"] =
                        AmendmentContract.SearchDocumentHash(
                            File.ReadAllText(hashDoc)
                                .Replace("\r\n", "\n")),
                });
            if (sealPreview is not null)
                return Emit(
                    AmendmentContract.ComputeSealPreview(sealPreview));
            if (artifactTables is not null)
                return Emit(StageCodec.ArtifactTableNames(artifactTables)
                    ?.OrderBy(t => t, StringComparer.Ordinal).ToList()
                    ?? (object)new Dictionary<string, object?>
                    { ["error"] = "ARTIFACT_UNREADABLE" });
            if (artifactVersion is not null)
                return Emit(new Dictionary<string, object?>
                {
                    ["codex_version"] =
                        StageCodec.ArtifactVersion(artifactVersion),
                });
            if (roundtrip is not null)
            {
                var target = roundtripOut
                    ?? throw new ArgumentException(
                        "--roundtrip requires <sql> <out>");
                var schema = StageCodec.StageName("parity"
                    + Guid.NewGuid().ToString("N")[..12]);
                try
                {
                    StageCodec.MaterializeArtifact(roundtrip, schema);
                    StageCodec.DumpSchema(schema, target);
                }
                finally
                {
                    StageCodec.DropSchema(schema);
                }
                return Emit(new Dictionary<string, object?>
                {
                    ["roundtrip"] = target,
                    ["source_sha256"] = FileHash(roundtrip),
                    ["target_sha256"] = FileHash(target),
                });
            }
            if (exportTarget is not null)
                return Emit(new Dictionary<string, object?>
                {
                    ["exported"] =
                        PgExport.ExportPostgresqlCodex(exportTarget),
                });
            if (authorityState)
                return Emit(PgExport.AuthorityState());
            if (verifyParity is not null)
                return Emit(PgExport.VerifySqlParity(verifyParity));
            if (buildRequest is not null)
            {
                if (buildSource is null || buildOutput is null)
                    throw new ArgumentException(
                        "--build-successor requires"
                        + " <request> <source> <output>");
                var ledger = new CodexAmendmentRequestLedger(ledgerRoot);
                var built = SuccessorBuilder.BuildSuccessor(
                    buildRequest, buildSource, buildOutput, ledger,
                    successorVersion: successorVersion);
                return Emit(built.AsDict());
            }
            if (execute)
            {
                if (request is null)
                {
                    Console.Error.WriteLine(
                        "--execute requires --request");
                    Usage();
                    return 2;
                }
                Dictionary<string, object?>? auditResult = null;
                if (!string.IsNullOrEmpty(auditResultPath))
                    auditResult = (Dictionary<string, object?>)
                        Repo.ToPlain(
                            JsonNode.Parse(File.ReadAllText(
                                auditResultPath)))!;
                var result = Executor.ExecuteAmendment(
                    requestPath: request,
                    preparedDatabase:
                        string.IsNullOrEmpty(prepared) ? null : prepared,
                    auditResult: auditResult,
                    apply: apply,
                    stagingRoot:
                        string.IsNullOrEmpty(staging) ? null : staging);
                Emit(result.ToDict());
                return result.Ok ? 0 : 1;
            }
            if (scan)
                return Emit(Driver.ScanRequests());
            if (request is not null)
            {
                var outcome = await Driver.AdvanceRequest(request,
                    sourceDatabase:
                        string.IsNullOrEmpty(source) ? null : source,
                    autoExecute: autoExecute);
                Emit(outcome);
                return outcome.TryGetValue("ok", out var ok)
                    && ok is true ? 0 : 1;
            }
            if (all)
            {
                var outcomes = await Driver.AdvanceAll(
                    sourceDatabase:
                        string.IsNullOrEmpty(source) ? null : source,
                    autoExecute: autoExecute);
                Emit(outcomes);
                return outcomes.All(item =>
                    item.TryGetValue("ok", out var ok)
                    && ok is true) ? 0 : 1;
            }
            Usage();
            return 2;
        }
        catch (ExecutorDenied error)
        {
            Emit(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["error_code"] = ExecutorDenied.FailureCode,
                ["reason"] = error.Message,
            });
            return 1;
        }
        catch (ArgumentException error)
        {
            Console.Error.WriteLine(error.Message);
            Usage();
            return 2;
        }
        catch (Exception error)
        {
            Emit(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = false,
                ["error"] = $"{error.GetType().Name}:{error.Message}",
            });
            return 1;
        }
    }
}
